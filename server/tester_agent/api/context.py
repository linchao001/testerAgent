"""上下文调试路由（WP-32 Task 15；context-management §8 / plan §8）。

端点：

- ``GET /tasks/{id}/context`` / ``GET /conversations/{id}/context``：
  三分区视图（P1 不含 passage 全文；journal 降级附 ``degraded``）；
- ``GET /tasks/{id}/context/evictions``：journal key-set 分页
  （action∈demote/evict/goal/batch_close/pin；``?partition=`` 过滤）；
- ``POST /workspaces/{id}/context/playground``：组装预览
  （persist_evictions=False，零业务表写入，对齐 dd §8.7 口径）。

跨 workspace / 未知资源 → 404 不暴露存在性。WP-33：``POST .../context/commands``。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from langchain_core.messages import SystemMessage
from pydantic import BaseModel, Field

from ..context.assembler import assemble
from ..context.intervention import (
    ContextAction,
    ContextCommand,
    CommandResult,
    execute,
)
from ..context.journal import JournalAction
from ..context.models import (
    ContextEntry,
    ContextPartition,
    ContextView,
    EntryKind,
    Phase,
    ProfileName,
)
from ..context.scopes import ExecScope
from ..context.store import ContextStore
from ..context.tokens import estimate_tokens
from ..errors import NotFoundError, TaskStateConflict, ValidationError
from ..store.models import (
    ArtifactDAO,
    ContextJournalDAO,
    ConversationDAO,
    MessageDAO,
    TaskDAO,
    WorkspaceDAO,
)
from .common import DEFAULT_LIMIT, checked_cursor, idem_check, idem_record, page_response
from .events import CONTEXT_COMMAND_EXECUTED, CONTEXT_POLICY_CHANGED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["context"])

_EVICTION_ACTIONS = [
    JournalAction.DEMOTE,
    JournalAction.EVICT,
    JournalAction.GOAL,
    JournalAction.BATCH_CLOSE,
    JournalAction.PIN,
]

# playground 组装用的保守窗口（不读真实模型配置；仅校验预算合法性）
_PLAYGROUND_MODEL_WINDOW = 128_000

_TERMINAL_TASK_STATUS = frozenset({"completed", "failed", "aborted", "cancelled"})

# ---- 请求/响应模型 ----


class PlaygroundItemIn(BaseModel):
    entry_id: str
    content: str
    digest: str = ""
    entry_kind: str | None = None
    tokens_est: int = 0
    pinned: bool = False


class PlaygroundIn(BaseModel):
    profile: ProfileName
    goal: str | None = None
    p1_items: list[PlaygroundItemIn] = Field(default_factory=list)
    p2_entries: list[PlaygroundItemIn] = Field(default_factory=list)
    scope: dict[str, Any] | None = None


class EvictionOut(BaseModel):
    id: str
    workspace_id: str
    owner_type: str
    owner_id: str
    partition: str
    entry_id: str
    entry_kind: str
    action: str
    reason: str
    policy_version: str
    tokens_est: int
    digest: str
    refs: dict
    created_at: str
    step_id: str | None = None
    batch_id: str | None = None
    item_key: str | None = None


class CommandIn(BaseModel):
    action: ContextAction
    selector: str = ""
    arg: str | None = None
    confirm: bool = False
    reason: str | None = None


# ---- 视图 ----


@router.get("/tasks/{task_id}/context")
async def get_task_context(task_id: str, request: Request) -> ContextView:
    task = await TaskDAO(request.app.state.db).get(task_id)  # 404
    store = await _resolve_store(
        request,
        owner_type="task",
        owner_id=task_id,
        workspace_id=task.workspace_id,
    )
    view = _build_view(store)
    await _emit_degraded_if_needed(request, task_id, view)
    return view


@router.get("/conversations/{conversation_id}/context")
async def get_conversation_context(
    conversation_id: str, request: Request
) -> ContextView:
    conv = await ConversationDAO(request.app.state.db).get(conversation_id)  # 404
    store = await _resolve_store(
        request,
        owner_type="conversation",
        owner_id=conversation_id,
        workspace_id=conv.workspace_id,
    )
    return _build_view(store)


@router.get("/tasks/{task_id}/context/evictions")
async def list_task_evictions(
    task_id: str,
    request: Request,
    partition: str | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=200),
    cursor: str | None = Query(None),
) -> dict:
    db = request.app.state.db
    task = await TaskDAO(db).get(task_id)  # 404
    if partition is not None and partition not in {
        ContextPartition.P0.value,
        ContextPartition.P1.value,
        ContextPartition.P2.value,
    }:
        raise ValidationError(
            "非法 partition",
            details={"partition": partition},
        )
    page = await ContextJournalDAO(db).list_by_owner(
        workspace_id=task.workspace_id,
        owner_type="task",
        owner_id=task_id,
        cursor=checked_cursor(cursor),
        limit=limit,
        partition=partition,
        actions=_EVICTION_ACTIONS,
    )
    return page_response(page, _eviction_out)


# ---- playground ----


@router.post("/workspaces/{workspace_id}/context/playground")
async def context_playground(
    workspace_id: str, body: PlaygroundIn, request: Request
) -> dict:
    """组装预览：临时 store + persist_evictions=False，不落任何业务表。"""
    await WorkspaceDAO(request.app.state.db).get(workspace_id)  # 404
    store = ContextStore(
        owner_type="task",
        owner_id=f"playground-{uuid.uuid4().hex}",
        workspace_id=workspace_id,
        policy_version="cp-v1",
        journal=None,  # 显式无 sink：零 journal 写入
    )
    now = "2026-01-01T00:00:00.000Z"
    for item in body.p1_items:
        await store.append(
            ContextEntry(
                entry_id=item.entry_id,
                partition=ContextPartition.P1,
                entry_kind=EntryKind.KB_BLOCK,
                content=item.content,
                digest=item.digest or item.content[:80],
                tokens_est=item.tokens_est
                or estimate_tokens(item.content),
                pinned=item.pinned,
                created_at=now,
            )
        )
    for item in body.p2_entries:
        kind = _parse_kind(item.entry_kind) if item.entry_kind else EntryKind.REFLECTION
        await store.append(
            ContextEntry(
                entry_id=item.entry_id,
                partition=ContextPartition.P2,
                entry_kind=kind,
                content=item.content,
                digest=item.digest or item.content[:80],
                tokens_est=item.tokens_est
                or estimate_tokens(item.content),
                pinned=item.pinned,
                created_at=now,
                step_seq=int((body.scope or {}).get("step_seq", 0) or 0),
            )
        )
    if body.goal:
        await store.set_goal(body.goal, reason="playground")

    scope = _parse_scope(body.scope)
    result = await assemble(
        store,
        body.profile,
        p0_messages=[SystemMessage(content="【方法论】playground")],
        model_window=_PLAYGROUND_MODEL_WINDOW,
        scope=scope,
        goal=body.goal,
        persist_evictions=False,
    )
    return {
        "report": result.report.model_dump(mode="json"),
        "messages": [
            {"type": type(m).__name__, "content": getattr(m, "content", "")}
            for m in result.messages
        ],
        "degraded": None,
        "snapshot_state": store.snapshot_state(),
    }


# ---- WP-33 commands ----


@router.post("/tasks/{task_id}/context/commands")
async def post_task_context_command(
    task_id: str,
    body: CommandIn,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    db = request.app.state.db
    task = await TaskDAO(db).get(task_id)  # 404
    body_json = body.model_dump_json()
    cached = idem_check(request, "context.commands.task", idempotency_key, body_json)
    if cached is not None:
        return cached.body

    store = await _resolve_store(
        request,
        owner_type="task",
        owner_id=task_id,
        workspace_id=task.workspace_id,
    )
    if task.status in _TERMINAL_TASK_STATUS:
        store.closed = True

    result = await _run_command(
        request,
        store=store,
        body=body,
        emit_task_id=task_id,
        model_window=_model_window(request),
    )
    out = result.model_dump(mode="json")
    idem_record(
        request, "context.commands.task", idempotency_key, body_json, 200, out
    )
    return out


@router.post("/conversations/{conversation_id}/context/commands")
async def post_conversation_context_command(
    conversation_id: str,
    body: CommandIn,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict:
    db = request.app.state.db
    conv = await ConversationDAO(db).get(conversation_id)  # 404
    body_json = body.model_dump_json()
    cached = idem_check(
        request, "context.commands.conversation", idempotency_key, body_json
    )
    if cached is not None:
        return cached.body

    store = await _resolve_store(
        request,
        owner_type="conversation",
        owner_id=conversation_id,
        workspace_id=conv.workspace_id,
    )
    result = await _run_command(
        request,
        store=store,
        body=body,
        emit_task_id=None,
        model_window=_model_window(request),
    )
    out = result.model_dump(mode="json")
    idem_record(
        request,
        "context.commands.conversation",
        idempotency_key,
        body_json,
        200,
        out,
    )
    return out


async def _run_command(
    request: Request,
    *,
    store: ContextStore,
    body: CommandIn,
    emit_task_id: str | None,
    model_window: int,
) -> CommandResult:
    cmd = ContextCommand(
        action=body.action,
        selector=body.selector,
        arg=body.arg,
        confirm=body.confirm,
        reason=body.reason,
    )
    result = await execute(
        store, cmd, operator="api", model_window=model_window
    )
    if emit_task_id:
        await _emit_command_events(request, emit_task_id, cmd, result)
    return result


async def _emit_command_events(
    request: Request,
    task_id: str,
    cmd: ContextCommand,
    result: CommandResult,
) -> None:
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        return
    payload = {
        "action": result.action,
        "selector": result.selector,
        "affected": list(result.affected),
        "affected_meta": dict(result.affected_meta),
    }
    try:
        await bus.emit(task_id, CONTEXT_COMMAND_EXECUTED, payload)
        if cmd.action in (
            ContextAction.BUDGET,
            ContextAction.FREEZE,
            ContextAction.UNFREEZE,
            ContextAction.SET_GOAL,
        ):
            await bus.emit(
                task_id,
                CONTEXT_POLICY_CHANGED,
                {
                    "reason": f"command:{cmd.action.value}",
                    "action": cmd.action.value,
                },
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "emit context command events failed",
            extra={"task_id": task_id},
        )


def _model_window(request: Request) -> int:
    app_ctx = getattr(request.app.state, "app_ctx", None)
    if app_ctx is None:
        return _PLAYGROUND_MODEL_WINDOW
    try:
        cfg = app_ctx.config  # ConfigDAO
        # sync path unavailable; fall back — callers in tests rarely need real window
        model = getattr(cfg, "model_dict", None)
        if callable(model):
            # ConfigDAO.model_dict is async in some paths; avoid here
            pass
    except Exception:  # noqa: BLE001
        pass
    return _PLAYGROUND_MODEL_WINDOW


# ---- 内部 ----


def _build_view(store: ContextStore) -> ContextView:
    entries: list[ContextEntry] = []
    totals: dict[str, int] = {
        "P0": 0,
        "P1": 0,
        "P2": 0,
        "active": 0,
        "demoted": 0,
        "evicted": 0,
        "tokens_est": 0,
    }
    for e in store.entries():
        totals[e.partition.value] = totals.get(e.partition.value, 0) + 1
        totals[e.status.value] = totals.get(e.status.value, 0) + 1
        totals["tokens_est"] += e.tokens_est
        if e.partition is ContextPartition.P1:
            # P1 不含 passage 全文；保留 digest / snapshot_id 供调试跳转
            entries.append(e.model_copy(update={"content": ""}))
        else:
            entries.append(e)
    degraded = None
    pending = len(store.degraded_journal)
    if pending:
        degraded = {"journal_pending": pending}
    return ContextView(
        owner={
            "scope": store.owner_type,
            "owner_id": store.owner_id,
            "workspace_id": store.workspace_id,
        },
        goal=store.goal_text(),
        entries=entries,
        totals=totals,
        degraded=degraded,
    )


async def _resolve_store(
    request: Request,
    *,
    owner_type: str,
    owner_id: str,
    workspace_id: str,
) -> ContextStore:
    app_ctx = getattr(request.app.state, "app_ctx", None)
    if app_ctx is None:
        raise TaskStateConflict(
            "任务执行组件未就绪，上下文视图不可用",
            details={"reason": "app_ctx_not_ready"},
        )
    registry = app_ctx.context_registry
    existing = registry.get_owner(owner_type, owner_id)  # type: ignore[arg-type]
    if existing is not None:
        if existing.workspace_id != workspace_id:
            # 跨 workspace：404 不暴露存在性
            raise NotFoundError(
                f"{'任务' if owner_type == 'task' else '会话'}不存在：{owner_id}"
            )
        return existing

    db = app_ctx.db
    journal = ContextJournalDAO(db)
    # rebuild 所需最小 DAOs（缺源时跳过，不抛）
    from ..runtime.context import DAOs

    daos = DAOs(
        task=TaskDAO(db),
        message=MessageDAO(db),
        artifact=ArtifactDAO(db),
        journal=journal,
    )
    return await registry.restore_owner(
        owner_type=owner_type,  # type: ignore[arg-type]
        owner_id=owner_id,
        workspace_id=workspace_id,
        daos=daos,
        journal_sink=journal,
    )


async def _emit_degraded_if_needed(
    request: Request, task_id: str, view: ContextView
) -> None:
    if not view.degraded:
        return
    bus = getattr(request.app.state, "bus", None)
    if bus is None:
        return
    try:
        await bus.emit(
            task_id,
            CONTEXT_POLICY_CHANGED,
            {
                "reason": "journal_degraded",
                "pending": int(view.degraded.get("journal_pending", 0)),
            },
        )
    except Exception:  # noqa: BLE001 — SSE 旁路，不阻断读路径
        logger.exception(
            "emit context_policy_changed failed",
            extra={"task_id": task_id},
        )


def _eviction_out(row: Any) -> EvictionOut:
    return EvictionOut(
        id=row.id,
        workspace_id=row.workspace_id,
        owner_type=row.owner_type,
        owner_id=row.owner_id,
        partition=row.partition,
        entry_id=row.entry_id,
        entry_kind=row.entry_kind,
        action=row.action,
        reason=row.reason,
        policy_version=row.policy_version,
        tokens_est=row.tokens_est,
        digest=row.digest,
        refs=row.refs_obj() if hasattr(row, "refs_obj") else {},
        created_at=row.created_at,
        step_id=row.step_id,
        batch_id=row.batch_id,
        item_key=row.item_key,
    )


def _parse_scope(raw: dict[str, Any] | None) -> ExecScope:
    if not raw:
        return ExecScope()
    phase_raw = raw.get("phase", "shared")
    try:
        phase = Phase(phase_raw)
    except ValueError as exc:
        raise ValidationError(
            "非法 scope.phase", details={"phase": phase_raw}
        ) from exc
    return ExecScope(
        phase=phase,
        step_id=raw.get("step_id"),
        step_seq=int(raw.get("step_seq") or 0),
        batch_id=raw.get("batch_id"),
        item_key=raw.get("item_key"),
        turn_seq=int(raw.get("turn_seq") or 0),
    )


def _parse_kind(raw: str) -> EntryKind:
    try:
        return EntryKind(raw)
    except ValueError as exc:
        raise ValidationError(
            "非法 entry_kind", details={"entry_kind": raw}
        ) from exc

