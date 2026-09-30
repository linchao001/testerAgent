"""从审计数据 + journal 重放重建 ContextStore（spec §7 / plan §5.3）。

本模块为叶子层：不 import store/runtime；DAO 经 duck-type ``daos`` 注入。
重建期间 store 不挂 journal（避免重放动作二次落库）；结束写一行
``action=rebuild`` 标记。item 过程条目不复活（spec §15.3）。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Literal

from ._time import utcnow_iso
from .journal import JournalAction, JournalRecord, JournalSink
from .models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryRefs,
    EntryStatus,
    Phase,
    ScopeLevel,
)
from .policy import programmatic_digest
from .store import ContextStore
from .tokens import estimate_tokens

logger = logging.getLogger(__name__)

OwnerType = Literal["task", "conversation"]

_CHAT_ROLES = frozenset({"user", "assistant"})


async def rebuild_store(
    *,
    owner_type: OwnerType,
    owner_id: str,
    workspace_id: str,
    daos: Any,
    journal_sink: JournalSink | None = None,
    current_step_seq: int = 0,
    policy_version: str = "cp-v1",
    step_window: int = 1,
) -> ContextStore:
    """按审计数据 + journal 重放构造 store；缺源不抛。"""
    del current_step_seq, step_window  # P1 一期统一 DEMOTED 占位，参数预留
    store = ContextStore(
        owner_type=owner_type,
        owner_id=owner_id,
        workspace_id=workspace_id,
        policy_version=policy_version,
        journal=None,
    )
    missing: list[str] = []

    if owner_type == "conversation":
        await _hydrate_messages(store, daos, owner_id)
    else:
        await _hydrate_artifacts(store, daos, owner_id, missing)
        await _hydrate_traces_p1(store, daos, owner_id)

    actions = await _load_actions(daos, workspace_id, owner_type, owner_id)
    await _replay_actions(store, actions, missing)

    if journal_sink is not None:
        now = utcnow_iso()
        records = [
            JournalRecord(
                id=uuid.uuid4().hex,
                workspace_id=workspace_id,
                owner_type=owner_type,
                owner_id=owner_id,
                task_id=owner_id if owner_type == "task" else None,
                conversation_id=owner_id if owner_type == "conversation" else None,
                partition=ContextPartition.P2.value,
                entry_id="*",
                entry_kind="rebuild",
                action=JournalAction.REBUILD,
                reason="rebuild",
                policy_version=policy_version,
                digest=f"entries={len(store.entries())}",
                refs={},
                created_at=now,
            )
        ]
        for eid in missing:
            records.append(
                JournalRecord(
                    id=uuid.uuid4().hex,
                    workspace_id=workspace_id,
                    owner_type=owner_type,
                    owner_id=owner_id,
                    task_id=owner_id if owner_type == "task" else None,
                    conversation_id=(
                        owner_id if owner_type == "conversation" else None
                    ),
                    partition=ContextPartition.P2.value,
                    entry_id=eid,
                    entry_kind="unknown",
                    action=JournalAction.REBUILD,
                    reason="source_missing",
                    policy_version=policy_version,
                    digest="",
                    refs={},
                    created_at=now,
                )
            )
        try:
            await journal_sink.record(records)
        except Exception:  # noqa: BLE001
            logger.exception("rebuild journal marker failed; store still valid")
            store.degraded_journal.extend(records)

    store._journal = journal_sink  # noqa: SLF001 — 重建完成后挂回 sink
    return store


# ---------- hydrate ----------


async def _hydrate_messages(store: ContextStore, daos: Any, conversation_id: str) -> None:
    msg_dao = getattr(daos, "message", None)
    if msg_dao is None:
        return
    page = await msg_dao.list_by_conversation(
        conversation_id, cursor=None, limit=500
    )
    rows = []
    for m in reversed(getattr(page, "items", []) or []):
        kind = getattr(m, "kind", "chat")
        kind_s = kind.value if hasattr(kind, "value") else str(kind)
        role = getattr(m, "role", None)
        role_s = role.value if hasattr(role, "value") else role
        if kind_s == "chat" and role_s in _CHAT_ROLES:
            rows.append(m)
    turn = 0
    expect_assistant = False
    for m in rows:
        role = getattr(m, "role", None)
        role_s = role.value if hasattr(role, "value") else str(role)
        if role_s == "user":
            turn += 1
            expect_assistant = True
        elif not expect_assistant:
            turn += 1
        content = m.content or ""
        await store.append(
            ContextEntry(
                entry_id=f"chat:{m.id}",
                partition=ContextPartition.P2,
                entry_kind=EntryKind.CHAT_TURN,
                role=role_s,
                content=content,
                digest=programmatic_digest(content),
                tokens_est=estimate_tokens(content),
                turn_seq=turn,
                refs=EntryRefs(message_id=m.id),
                created_at=m.created_at or utcnow_iso(),
                source="rebuild",
            )
        )
        if role_s == "assistant":
            expect_assistant = False


async def _hydrate_artifacts(
    store: ContextStore, daos: Any, task_id: str, missing: list[str]
) -> None:
    art_dao = getattr(daos, "artifact", None)
    if art_dao is None:
        return
    try:
        chain = await art_dao.list_active_chain(task_id)
    except Exception:  # noqa: BLE001
        logger.exception("rebuild list_active_chain failed")
        return
    for art in chain or []:
        kind = (getattr(art, "kind", None) or getattr(art, "stage", "") or "").lower()
        payload_raw = getattr(art, "payload", "{}") or "{}"
        try:
            payload = (
                art.payload_obj()
                if hasattr(art, "payload_obj")
                else json.loads(payload_raw)
            )
        except Exception:  # noqa: BLE001
            payload = {}
        summary = _artifact_digest_text(kind, payload, art.id)
        is_plan = kind in {"agent_plan", "plan"} or "plan_id" in payload
        confirmed = getattr(art, "confirmed_by", None) == "user"
        entry_kind = EntryKind.PLAN if is_plan else EntryKind.ARTIFACT_DIGEST
        phase = Phase.SHARED if (is_plan or confirmed) else Phase.DESIGN
        await store.append(
            ContextEntry(
                entry_id=f"artifact:{art.id}",
                partition=ContextPartition.P2,
                entry_kind=entry_kind,
                content=summary,
                digest=programmatic_digest(summary),
                tokens_est=estimate_tokens(summary),
                pinned=bool(is_plan or confirmed),
                refs=EntryRefs(payload_ref=art.id),
                phase=phase,
                created_at=getattr(art, "created_at", None) or utcnow_iso(),
                source="rebuild",
            )
        )


async def _hydrate_traces_p1(store: ContextStore, daos: Any, task_id: str) -> None:
    """P1：injected_ids → DEMOTED 占位（正文不重建；active 由后续 step 重挂）。"""
    trace_dao = getattr(daos, "trace", None)
    if trace_dao is None:
        return
    cursor = None
    seen: set[str] = set()
    while True:
        page = await trace_dao.list_by_task(
            task_id, stage=None, version=None, cursor=cursor, limit=100
        )
        for tr in getattr(page, "items", []) or []:
            ids = (
                tr.injected_ids_obj()
                if callable(getattr(tr, "injected_ids_obj", None))
                else getattr(tr, "injected_ids", None) or []
            )
            if isinstance(ids, str):
                try:
                    ids = json.loads(ids)
                except Exception:  # noqa: BLE001
                    ids = []
            for eid in ids:
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                entry = ContextEntry(
                    entry_id=f"kb:{eid}",
                    partition=ContextPartition.P1,
                    entry_kind=EntryKind.KB_BLOCK,
                    content="",
                    digest=f"kb {eid}",
                    refs=EntryRefs(trace_id=getattr(tr, "id", None)),
                    created_at=getattr(tr, "created_at", None) or utcnow_iso(),
                    source="rebuild",
                )
                await store.append(entry)
                await store.demote(entry.entry_id, "rebuild_p1_placeholder")
        cursor = getattr(page, "next_cursor", None)
        if not cursor:
            break


def _artifact_digest_text(kind: str, payload: dict, art_id: str) -> str:
    if isinstance(payload, dict):
        goal = payload.get("goal")
        if goal:
            return str(goal)
        title = payload.get("title")
        if title:
            return str(title)
    return f"{kind or 'artifact'} #{art_id}"


# ---------- journal replay ----------


async def _load_actions(
    daos: Any, workspace_id: str, owner_type: str, owner_id: str
) -> list[JournalRecord]:
    jdao = getattr(daos, "journal", None)
    if jdao is None or not hasattr(jdao, "list_actions"):
        return []
    rows = await jdao.list_actions(
        workspace_id=workspace_id, owner_type=owner_type, owner_id=owner_id
    )
    out: list[JournalRecord] = []
    for r in rows or []:
        if isinstance(r, JournalRecord):
            out.append(r)
        else:
            # ContextJournalRow → JournalRecord
            refs = r.refs_obj() if hasattr(r, "refs_obj") else {}
            out.append(
                JournalRecord(
                    id=r.id,
                    workspace_id=r.workspace_id,
                    owner_type=r.owner_type,
                    owner_id=r.owner_id,
                    task_id=r.task_id,
                    conversation_id=r.conversation_id,
                    partition=r.partition,
                    entry_id=r.entry_id,
                    entry_kind=r.entry_kind,
                    action=r.action,
                    reason=r.reason,
                    policy_version=r.policy_version,
                    tokens_est=r.tokens_est,
                    digest=r.digest,
                    refs=refs if isinstance(refs, dict) else {},
                    scope_level=r.scope_level,
                    phase=r.phase,
                    step_id=r.step_id,
                    batch_id=r.batch_id,
                    item_key=r.item_key,
                    created_at=r.created_at,
                )
            )
    return out


async def _replay_actions(
    store: ContextStore, actions: list[JournalRecord], missing: list[str]
) -> None:
    for rec in actions:
        if rec.action == JournalAction.REBUILD:
            continue
        if rec.action == JournalAction.BATCH_CLOSE:
            if rec.batch_id:
                await store.close_batch(rec.batch_id)
            continue
        if rec.action == JournalAction.GOAL:
            # set_goal 已由 APPEND+GOAL 成对记录；仅 GOAL 行时补写
            if rec.entry_id not in {e.entry_id for e in store.entries()}:
                await store.set_goal(rec.digest or rec.reason, reason=rec.reason)
            continue
        if rec.action == JournalAction.APPEND:
            await _replay_append(store, rec)
            continue
        # demote / evict / pin / unpin — 条目须已存在
        if rec.entry_id not in {e.entry_id for e in store.entries()}:
            # 尝试从 digest 补建再迁移；item 永不复活
            if rec.scope_level == ScopeLevel.ITEM.value or rec.item_key:
                continue
            if rec.action == JournalAction.APPEND:
                continue
            missing.append(rec.entry_id)
            continue
        try:
            if rec.action == JournalAction.DEMOTE:
                e = store.get(rec.entry_id)
                if e.status is EntryStatus.ACTIVE:
                    await store.demote(rec.entry_id, rec.reason)
            elif rec.action == JournalAction.EVICT:
                e = store.get(rec.entry_id)
                if e.status is not EntryStatus.EVICTED:
                    await store.evict(rec.entry_id, rec.reason)
            elif rec.action == JournalAction.PIN:
                e = store.get(rec.entry_id)
                if e.status is EntryStatus.ACTIVE and not e.pinned:
                    await store.pin(rec.entry_id, reason=rec.reason)
            elif rec.action == JournalAction.UNPIN:
                e = store.get(rec.entry_id)
                if e.pinned:
                    await store.unpin(rec.entry_id, reason=rec.reason)
            elif rec.action == JournalAction.REFRESH:
                e = store.get(rec.entry_id)
                if e.status is not EntryStatus.ACTIVE:
                    # 无正文源时用 digest 占位（完整回填依赖审计源）
                    await store.reactivate(
                        rec.entry_id,
                        content=rec.digest or e.digest or "",
                        reason=rec.reason,
                    )
        except Exception:  # noqa: BLE001
            missing.append(rec.entry_id)


async def _replay_append(store: ContextStore, rec: JournalRecord) -> None:
    if rec.scope_level == ScopeLevel.ITEM.value or rec.item_key:
        return  # item 过程不复活
    if any(e.entry_id == rec.entry_id for e in store.entries()):
        return
    try:
        partition = ContextPartition(rec.partition)
    except Exception:  # noqa: BLE001
        partition = ContextPartition.P2
    try:
        kind = EntryKind(rec.entry_kind)
    except Exception:  # noqa: BLE001
        kind = EntryKind.REFLECTION
    try:
        phase = Phase(rec.phase)
    except Exception:  # noqa: BLE001
        phase = Phase.SHARED
    try:
        scope_level = ScopeLevel(rec.scope_level)
    except Exception:  # noqa: BLE001
        scope_level = ScopeLevel.TASK
    content = rec.digest or f"〔重建 {rec.entry_kind} #{rec.entry_id}〕"
    # 非 ACTIVE 审计过程：以 tombstone 正文挂入后保持 ACTIVE，后续 demote 行裁决
    entry = ContextEntry(
        entry_id=rec.entry_id,
        partition=partition,
        entry_kind=kind,
        content=content,
        digest=rec.digest or programmatic_digest(content),
        tokens_est=rec.tokens_est or estimate_tokens(content),
        refs=EntryRefs(**(rec.refs or {})),
        scope_level=scope_level,
        phase=phase,
        step_id=rec.step_id,
        batch_id=rec.batch_id,
        item_key=rec.item_key,
        created_at=rec.created_at,
        source="rebuild",
    )
    await store.append(entry)
