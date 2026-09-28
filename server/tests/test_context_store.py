"""ContextStore：三分区存放、状态机、幂等 append、pin、journal 协议、并发。"""

from __future__ import annotations

import asyncio

import pytest

from tester_agent.context._time import utcnow_iso
from tester_agent.context.journal import JournalAction, JournalRecord, JournalSink
from tester_agent.context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryStatus,
    Phase,
    ScopeLevel,
)
from tester_agent.context.store import ContextStore
from tester_agent.errors import ValidationError


class FakeSink(JournalSink):
    def __init__(self, *, fail: bool = False):
        self.rows: list[JournalRecord] = []
        self.fail = fail
        self.batches = 0

    async def record(self, records: list[JournalRecord]) -> None:
        if self.fail:
            raise RuntimeError("journal down")
        self.batches += 1
        self.rows.extend(records)


def _entry(eid: str = "plan:p1:v1", **kw) -> ContextEntry:
    base = dict(
        entry_id=eid,
        partition=ContextPartition.P2,
        entry_kind=EntryKind.PLAN,
        content="计划正文",
        digest="计划 v1",
        created_at="2026-09-28T00:00:00.000Z",
    )
    base.update(kw)
    return ContextEntry(**base)


def _store(**kw) -> tuple[ContextStore, FakeSink]:
    sink = FakeSink()
    store = ContextStore(
        owner_type="task", owner_id="t1", workspace_id="ws1", journal=sink, **kw
    )
    return store, sink


async def test_append_and_get_active_by_default():
    store, sink = _store()
    ok = await store.append(_entry())
    assert ok is True
    assert store.get("plan:p1:v1").status is EntryStatus.ACTIVE
    assert sink.rows[-1].action == JournalAction.APPEND


async def test_append_is_idempotent_same_entry_id():
    store, sink = _store()
    await store.append(_entry(content="第一版"))
    ok2 = await store.append(_entry(content="第二版"))
    assert ok2 is False
    assert store.get("plan:p1:v1").content == "第一版"  # 不覆盖
    assert [r.action for r in sink.rows] == [JournalAction.APPEND]


async def test_entries_filters_and_sorted():
    store, _ = _store()
    await store.append(_entry("b", created_at="2026-09-28T00:00:02.000Z"))
    await store.append(_entry("a", created_at="2026-09-28T00:00:01.000Z"))
    ids = [e.entry_id for e in store.entries()]
    assert ids == ["a", "b"]
    assert all(e.partition is ContextPartition.P2 for e in store.entries(ContextPartition.P2))
    assert store.entries(ContextPartition.P0) == []


async def test_demote_evict_legal_path():
    store, sink = _store()
    await store.append(_entry())
    await store.demote("plan:p1:v1", "step_window")
    assert store.get("plan:p1:v1").status is EntryStatus.DEMOTED
    assert "已归档" in store.get("plan:p1:v1").content
    await store.evict("plan:p1:v1", "budget_cut")
    assert store.get("plan:p1:v1").status is EntryStatus.EVICTED
    reasons = [(r.action, r.reason) for r in sink.rows]
    assert ("demote", "step_window") in reasons
    assert ("evict", "budget_cut") in reasons


async def test_illegal_transitions_rejected():
    store, _ = _store()
    await store.append(_entry())
    await store.evict("plan:p1:v1", "x")
    with pytest.raises(ValidationError):
        await store.pin("plan:p1:v1", reason="x")
    with pytest.raises(ValidationError):
        await store.demote("plan:p1:v1", "x")
    with pytest.raises(ValidationError):
        await store.evict("plan:p1:v1", "x")


async def test_pin_requires_active_unpin_idempotent():
    store, sink = _store()
    await store.append(_entry())
    await store.pin("plan:p1:v1", reason="manual:pin")
    assert store.get("plan:p1:v1").pinned is True
    await store.unpin("plan:p1:v1", reason="manual:unpin")
    assert store.get("plan:p1:v1").pinned is False
    # 再次 unpin 为幂等 no-op（不写 journal）
    n = len(sink.rows)
    await store.unpin("plan:p1:v1", reason="again")
    assert len(sink.rows) == n
    # demoted 不可直接 pin（须先 refresh 回 active）
    await store.demote("plan:p1:v1", "superseded")
    with pytest.raises(ValidationError):
        await store.pin("plan:p1:v1", reason="x")


def test_get_missing_raises_key_error():
    store, _ = _store()
    with pytest.raises(KeyError):
        store.get("nope")


async def test_set_goal_supersedes_previous():
    store, sink = _store()
    await store.set_goal("覆盖登录", reason="init")
    await store.set_goal("只覆盖支付", reason="manual:set_goal")
    assert store.goal_text() == "只覆盖支付"
    goals = [e for e in store.entries() if e.entry_kind is EntryKind.GOAL]
    assert {e.status for e in goals} == {EntryStatus.ACTIVE, EntryStatus.DEMOTED}
    active = [e for e in goals if e.status is EntryStatus.ACTIVE][0]
    assert active.content == "只覆盖支付"
    assert active.supersedes is not None
    assert any(r.action == JournalAction.GOAL for r in sink.rows)


async def test_close_batch_evicts_batch_and_item_entries():
    store, sink = _store()
    await store.append(
        _entry(
            "item:b1:p1",
            scope_level=ScopeLevel.ITEM,
            phase=Phase.WRITE,
            batch_id="b1",
            item_key="b1:p1",
        )
    )
    await store.append(
        _entry(
            "kb:1",
            partition=ContextPartition.P1,
            entry_kind=EntryKind.KB_BLOCK,
            scope_level=ScopeLevel.BATCH,
            batch_id="b1",
        )
    )
    await store.append(_entry("task:shared", phase=Phase.SHARED))
    n = await store.close_batch("b1")
    assert n == 2
    assert store.get("item:b1:p1").status is EntryStatus.EVICTED
    assert store.get("kb:1").status is EntryStatus.EVICTED
    assert store.get("task:shared").status is EntryStatus.ACTIVE  # task 级不动
    assert any(r.action == JournalAction.BATCH_CLOSE for r in sink.rows)
    assert all(r.reason == "batch_closed" for r in sink.rows if r.action == JournalAction.EVICT)


async def test_bulk_demote_only_matches_active():
    store, sink = _store()
    await store.append(
        _entry("kb1", partition=ContextPartition.P1, entry_kind=EntryKind.KB_BLOCK,
               step_id="s1", step_seq=1)
    )
    await store.append(
        _entry("kb2", partition=ContextPartition.P1, entry_kind=EntryKind.KB_BLOCK,
               step_id="s2", step_seq=2)
    )
    ids = await store.bulk_demote(lambda e: e.step_id == "s1", "step_window")
    assert ids == ["kb1"]
    assert store.get("kb1").status is EntryStatus.DEMOTED
    assert store.get("kb2").status is EntryStatus.ACTIVE


async def test_concurrent_appends_no_loss():
    store, _ = _store()
    await asyncio.gather(*[
        store.append(_entry(f"e{i}", created_at=f"2026-09-28T00:00:{i:02d}.000Z"))
        for i in range(100)
    ])
    assert len(store.entries()) == 100


async def test_journal_failure_does_not_block_and_degraded_replays():
    sink = FakeSink(fail=True)
    store = ContextStore(owner_type="task", owner_id="t1", workspace_id="ws1", journal=sink)
    await store.append(_entry())
    assert store.get("plan:p1:v1").status is EntryStatus.ACTIVE  # 内存生效
    assert len(store.degraded_journal) == 1
    sink.fail = False
    flushed = await store.flush_degraded()
    assert flushed == 1
    assert store.degraded_journal == []
    assert len(sink.rows) == 1


async def test_snapshot_state_sorted_and_stable():
    store, _ = _store()
    await store.append(_entry("a"))
    await store.append(_entry("b"))
    state = store.snapshot_state()
    assert list(state) == ["a", "b"]
    assert state == {"a": "active", "b": "active"}


async def test_append_with_empty_created_at_autofills():
    store, _ = _store()
    before = utcnow_iso()
    await store.append(_entry("auto", created_at=""))
    ts = store.get("auto").created_at
    assert ts.endswith("Z") and ts >= before


async def test_append_inherits_scope_fields_from_scope_obj():
    store, _ = _store()

    class _S:  # duck-type ExecScope（Task 5 前避免前置依赖）
        phase = Phase.WRITE
        step_id = "s9"
        step_seq = 3
        batch_id = "b9"
        item_key = "b9:p2"
        turn_seq = 7

    await store.append(_entry("scoped"), scope=_S())
    e = store.get("scoped")
    assert e.phase is Phase.WRITE
    assert e.step_id == "s9"
    assert e.step_seq == 3
    assert e.batch_id == "b9"
    assert e.item_key == "b9:p2"
    assert e.turn_seq == 7
