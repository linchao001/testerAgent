"""WP-32 Task 13：rebuild_store（审计数据 + journal 重放）。"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.context.journal import JournalAction, JournalRecord
from tester_agent.context.models import (
    ContextPartition,
    EntryKind,
    EntryStatus,
    Phase,
    ScopeLevel,
)
from tester_agent.context.rebuild import rebuild_store
from tester_agent.context.registry import ContextRegistry


TS0 = "2026-09-29T00:00:00.000Z"
TS1 = "2026-09-29T00:00:01.000Z"
TS2 = "2026-09-29T00:00:02.000Z"


@dataclass
class FakeJournal:
    rows: list[JournalRecord] = field(default_factory=list)

    async def record(self, records: list[JournalRecord]) -> None:
        self.rows.extend(records)

    async def list_actions(
        self, *, workspace_id: str, owner_type: str, owner_id: str
    ) -> list[JournalRecord]:
        return [
            r
            for r in self.rows
            if r.workspace_id == workspace_id
            and r.owner_type == owner_type
            and r.owner_id == owner_id
        ]


def _jrec(
    id_: str,
    *,
    action: str,
    entry_id: str,
    reason: str = "",
    partition: str = "P2",
    entry_kind: str = "reflection",
    created_at: str = TS1,
    scope_level: str = "task",
    phase: str = "shared",
    batch_id: str | None = None,
    item_key: str | None = None,
    digest: str = "d",
) -> JournalRecord:
    return JournalRecord(
        id=id_,
        workspace_id="ws1",
        owner_type="task",
        owner_id="t1",
        task_id="t1",
        conversation_id=None,
        partition=partition,
        entry_id=entry_id,
        entry_kind=entry_kind,
        action=action,
        reason=reason or action,
        policy_version="cp-v1",
        digest=digest,
        refs={},
        scope_level=scope_level,
        phase=phase,
        batch_id=batch_id,
        item_key=item_key,
        created_at=created_at,
    )


@dataclass
class FakeArt:
    id: str
    task_id: str = "t1"
    stage: str = "link_identify"
    kind: str | None = None
    status: str = "active"
    confirmed_by: str | None = None
    payload: str = "{}"
    created_at: str = TS0
    stage_version: int = 1

    def payload_obj(self) -> dict:
        import json

        return json.loads(self.payload) if self.payload else {}


class FakeArtifactDAO:
    def __init__(self, rows: list[FakeArt]):
        self.rows = rows

    async def list_active_chain(self, task_id: str) -> list[FakeArt]:
        return [r for r in self.rows if r.task_id == task_id and r.status == "active"]

    async def get(self, artifact_id: str) -> FakeArt:
        for r in self.rows:
            if r.id == artifact_id:
                return r
        raise KeyError(artifact_id)


@dataclass
class FakeMsg:
    id: str
    role: str
    kind: str = "chat"
    content: str = "hi"
    created_at: str = TS0


class FakeMessageDAO:
    def __init__(self, rows: list[FakeMsg]):
        self.rows = rows

    async def list_by_conversation(
        self, conversation_id: str, *, cursor: str | None, limit: int
    ):
        # 倒序分页惯例：最新在前
        items = list(reversed(self.rows))[:limit]
        return SimpleNamespace(items=items, next_cursor=None)


class FakeTraceDAO:
    def __init__(self, injected_by_trace: dict[str, list[str]] | None = None):
        self._injected = injected_by_trace or {}

    async def list_by_task(self, task_id: str, *, stage, version, cursor, limit):
        items = []
        for tid, ids in self._injected.items():
            items.append(
                SimpleNamespace(
                    id=tid,
                    injected_ids_obj=lambda ids=ids: ids,
                    created_at=TS0,
                )
            )
        return SimpleNamespace(items=items, next_cursor=None)


async def test_rebuild_plan_pinned_and_confirmed_digest():
    arts = [
        FakeArt(
            id="plan-1",
            kind="agent_plan",
            payload='{"plan_id":"p1","goal":"g"}',
            created_at=TS0,
        ),
        FakeArt(
            id="art-link",
            kind="coverage_design",
            confirmed_by="user",
            payload='{"links":[]}',
            created_at=TS1,
        ),
    ]
    journal = FakeJournal()
    daos = SimpleNamespace(
        artifact=FakeArtifactDAO(arts),
        message=None,
        trace=FakeTraceDAO(),
        journal=journal,
    )
    store = await rebuild_store(
        owner_type="task",
        owner_id="t1",
        workspace_id="ws1",
        daos=daos,
        journal_sink=journal,
    )
    plan = store.get("artifact:plan-1")
    assert plan.entry_kind is EntryKind.PLAN
    assert plan.pinned is True
    assert plan.status is EntryStatus.ACTIVE
    dig = store.get("artifact:art-link")
    assert dig.entry_kind is EntryKind.ARTIFACT_DIGEST
    assert dig.pinned is True
    assert dig.phase is Phase.SHARED


async def test_rebuild_messages_to_chat_turns():
    msgs = [
        FakeMsg(id="m1", role="user", content="u1", created_at=TS0),
        FakeMsg(id="m2", role="assistant", content="a1", created_at=TS1),
        FakeMsg(id="m3", role="user", content="u2", created_at=TS2),
    ]
    journal = FakeJournal()
    daos = SimpleNamespace(
        artifact=None,
        message=FakeMessageDAO(msgs),
        trace=None,
        journal=journal,
    )
    store = await rebuild_store(
        owner_type="conversation",
        owner_id="c1",
        workspace_id="ws1",
        daos=daos,
        journal_sink=journal,
    )
    turns = [
        e for e in store.entries() if e.entry_kind is EntryKind.CHAT_TURN
    ]
    assert [e.refs.message_id for e in turns] == ["m1", "m2", "m3"]
    assert [e.turn_seq for e in turns] == [1, 1, 2]


async def test_rebuild_journal_replay_demote_evict_pin():
    journal = FakeJournal(
        rows=[
            _jrec("j0", action=JournalAction.APPEND, entry_id="r1", created_at=TS0),
            _jrec(
                "j1",
                action=JournalAction.DEMOTE,
                entry_id="r1",
                reason="step_window",
                created_at=TS1,
            ),
            _jrec(
                "j2",
                action=JournalAction.APPEND,
                entry_id="r2",
                created_at=TS1,
                digest="keep",
            ),
            _jrec(
                "j3",
                action=JournalAction.PIN,
                entry_id="r2",
                reason="manual",
                created_at=TS2,
            ),
            _jrec(
                "j4",
                action=JournalAction.EVICT,
                entry_id="r1",
                reason="budget_cut",
                created_at=TS2,
            ),
        ]
    )
    daos = SimpleNamespace(
        artifact=FakeArtifactDAO([]),
        message=None,
        trace=FakeTraceDAO(),
        journal=journal,
    )
    store = await rebuild_store(
        owner_type="task",
        owner_id="t1",
        workspace_id="ws1",
        daos=daos,
        journal_sink=journal,
    )
    assert store.get("r1").status is EntryStatus.EVICTED
    assert store.get("r2").pinned is True
    assert store.get("r2").status is EntryStatus.ACTIVE


async def test_rebuild_item_entries_not_resurrected():
    journal = FakeJournal(
        rows=[
            _jrec(
                "j0",
                action=JournalAction.APPEND,
                entry_id="item-1",
                scope_level="item",
                phase="write",
                batch_id="b1",
                item_key="b1:p1",
                created_at=TS0,
            ),
        ]
    )
    daos = SimpleNamespace(
        artifact=FakeArtifactDAO([]),
        message=None,
        trace=None,
        journal=journal,
    )
    store = await rebuild_store(
        owner_type="task",
        owner_id="t1",
        workspace_id="ws1",
        daos=daos,
        journal_sink=journal,
    )
    assert store.entries() == [] or all(
        e.scope_level is not ScopeLevel.ITEM for e in store.entries()
    )
    # item append 被跳过；不应出现 item-1
    with pytest.raises(KeyError):
        store.get("item-1")


async def test_rebuild_source_missing_does_not_raise():
    journal = FakeJournal(
        rows=[
            _jrec(
                "j0",
                action=JournalAction.DEMOTE,
                entry_id="ghost",
                reason="step_window",
                created_at=TS0,
            ),
        ]
    )
    daos = SimpleNamespace(
        artifact=FakeArtifactDAO([]),
        message=None,
        trace=None,
        journal=journal,
    )
    store = await rebuild_store(
        owner_type="task",
        owner_id="t1",
        workspace_id="ws1",
        daos=daos,
        journal_sink=journal,
    )
    # 缺源仅记 rebuild/source_missing，不抛
    assert any(
        r.action == JournalAction.REBUILD and r.reason == "source_missing"
        for r in journal.rows
    )
    assert store is not None


async def test_rebuild_idempotent_snapshot_state():
    arts = [
        FakeArt(id="plan-1", kind="agent_plan", payload='{"goal":"g"}'),
    ]
    journal = FakeJournal(
        rows=[
            _jrec("j0", action=JournalAction.APPEND, entry_id="r1", created_at=TS0),
            _jrec(
                "j1",
                action=JournalAction.DEMOTE,
                entry_id="r1",
                reason="step_window",
                created_at=TS1,
            ),
        ]
    )
    daos = SimpleNamespace(
        artifact=FakeArtifactDAO(arts),
        message=None,
        trace=FakeTraceDAO(),
        journal=journal,
    )
    s1 = await rebuild_store(
        owner_type="task",
        owner_id="t1",
        workspace_id="ws1",
        daos=daos,
        journal_sink=journal,
    )
    # 第二次：journal 已含 rebuild 标记行，仍应状态等价（忽略 rebuild 标记本身）
    s2 = await rebuild_store(
        owner_type="task",
        owner_id="t1",
        workspace_id="ws1",
        daos=daos,
        journal_sink=journal,
    )
    assert s1.snapshot_state() == s2.snapshot_state()


async def test_registry_restore_owner_uses_rebuild():
    msgs = [FakeMsg(id="m1", role="user", content="hello")]
    journal = FakeJournal()
    daos = SimpleNamespace(
        artifact=None,
        message=FakeMessageDAO(msgs),
        trace=None,
        journal=journal,
    )
    reg = ContextRegistry()
    store = await reg.restore_owner(
        owner_type="conversation",
        owner_id="c1",
        workspace_id="ws1",
        daos=daos,
        journal_sink=journal,
    )
    assert store.get("chat:m1").content == "hello"
    # 幂等：再 restore 返回同一实例
    again = await reg.restore_owner(
        owner_type="conversation",
        owner_id="c1",
        workspace_id="ws1",
        daos=daos,
        journal_sink=journal,
    )
    assert again is store
