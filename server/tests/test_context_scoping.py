"""context.scopes：ExecScope contextvars 栈 + 窗口可见性谓词（spec §15.5/§15.6）。

WP-31 Task 11 追加：ITEM 条目经 assemble 的 O(1) token 口径、close_batch
evict + journal batch_closed、repair 同 item_key 幂等保留。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import SystemMessage

from tester_agent.context.assembler import assemble
from tester_agent.context.journal import JournalAction, JournalRecord
from tester_agent.context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryStatus,
    Phase,
    ProfileName,
    ScopeLevel,
)
from tester_agent.context.scopes import ExecScope, current_scope, scope, visible_in_window
from tester_agent.context.store import ContextStore
from tester_agent.context.tokens import estimate_tokens


def _entry(eid, **kw) -> ContextEntry:
    base = dict(
        entry_id=eid,
        partition=ContextPartition.P2,
        entry_kind=EntryKind.REFLECTION,
        content="x",
        digest="x",
        created_at="2026-09-28T00:00:00.000Z",
    )
    base.update(kw)
    return ContextEntry(**base)


def test_default_scope_when_no_stack():
    s = current_scope()
    assert s.phase is Phase.SHARED
    assert s.step_id is None
    assert s.batch_id is None


async def test_nested_scope_inherits_parent_fields():
    async with scope(phase=Phase.WRITE, batch_id="b1") as outer:
        assert outer.batch_id == "b1"
        async with scope(item_key="b1:p2") as inner:
            # 未显式给的字段从父栈继承
            assert inner.batch_id == "b1"
            assert inner.phase is Phase.WRITE
            assert inner.item_key == "b1:p2"
        assert current_scope().batch_id == "b1"  # 弹栈恢复
    assert current_scope().batch_id is None


async def test_explicit_child_overrides_parent():
    async with scope(phase=Phase.DESIGN, step_id="s1"):
        async with scope(phase=Phase.WRITE, step_id="s9"):
            s = current_scope()
            assert s.phase is Phase.WRITE
            assert s.step_id == "s9"


async def test_contextvars_isolated_between_concurrent_tasks():
    order: list[str] = []

    async def worker(name: str, batch_id: str):
        async with scope(phase=Phase.WRITE, batch_id=batch_id):
            order.append(f"{name}:{current_scope().batch_id}")
            await asyncio.sleep(0)
            # 让出后不被另一任务污染
            assert current_scope().batch_id == batch_id
            order.append(f"{name}:ok")

    await asyncio.gather(worker("A", "bA"), worker("B", "bB"))
    assert "A:ok" in order and "B:ok" in order


def _write_item_scope() -> ExecScope:
    return ExecScope(phase=Phase.WRITE, batch_id="b1", item_key="b1:p3")


def test_case_item_previous_item_invisible_same_item_visible():
    s = _write_item_scope()
    prev = _entry("item:b1:p2", scope_level=ScopeLevel.ITEM, phase=Phase.WRITE,
                  batch_id="b1", item_key="b1:p2")
    cur = _entry("item:b1:p3", scope_level=ScopeLevel.ITEM, phase=Phase.WRITE,
                 batch_id="b1", item_key="b1:p3")
    assert not visible_in_window(prev, profile=ProfileName.CASE_ITEM, scope_=s)
    assert visible_in_window(cur, profile=ProfileName.CASE_ITEM, scope_=s)


def test_case_item_task_shared_visible_design_invisible():
    s = _write_item_scope()
    shared = _entry("goal-1", entry_kind=EntryKind.GOAL, phase=Phase.SHARED)
    design = _entry("design-draft", phase=Phase.DESIGN, step_id="s1")
    write_task = _entry("write-task", phase=Phase.WRITE)
    assert visible_in_window(shared, profile=ProfileName.CASE_ITEM, scope_=s)
    assert not visible_in_window(design, profile=ProfileName.CASE_ITEM, scope_=s)
    assert visible_in_window(write_task, profile=ProfileName.CASE_ITEM, scope_=s)


def test_batch_entries_cross_batch_invisible():
    s = ExecScope(phase=Phase.WRITE, batch_id="b1", item_key="b1:p1")
    other = _entry("kb:b2", partition=ContextPartition.P1, entry_kind=EntryKind.KB_BLOCK,
                   scope_level=ScopeLevel.BATCH, phase=Phase.WRITE, batch_id="b2")
    mine = _entry("kb:b1", partition=ContextPartition.P1, entry_kind=EntryKind.KB_BLOCK,
                  scope_level=ScopeLevel.BATCH, phase=Phase.WRITE, batch_id="b1")
    assert not visible_in_window(other, profile=ProfileName.CASE_ITEM, scope_=s)
    assert visible_in_window(mine, profile=ProfileName.CASE_ITEM, scope_=s)


def test_design_profile_sees_design_not_write():
    s = ExecScope(phase=Phase.DESIGN, step_id="s1")
    design = _entry("design-draft", phase=Phase.DESIGN, step_id="s1")
    write = _entry("write-task", phase=Phase.WRITE)
    shared = _entry("goal-1", entry_kind=EntryKind.GOAL, phase=Phase.SHARED)
    assert visible_in_window(design, profile=ProfileName.PLAN, scope_=s)
    assert not visible_in_window(write, profile=ProfileName.PLAN, scope_=s)
    assert visible_in_window(shared, profile=ProfileName.PLAN, scope_=s)
    # plan 调用无 item 上下文 → item 条目不可见
    item = _entry("it", scope_level=ScopeLevel.ITEM, phase=Phase.WRITE,
                  batch_id="b1", item_key="b1:p1")
    assert not visible_in_window(item, profile=ProfileName.PLAN, scope_=s)


def test_p0_always_visible_evicted_never():
    s = ExecScope()
    p0 = _entry("m1", partition=ContextPartition.P0, entry_kind=EntryKind.METHODOLOGY)
    gone = _entry("gone", status=EntryStatus.EVICTED)
    assert visible_in_window(p0, profile=ProfileName.CHAT, scope_=s)
    assert not visible_in_window(gone, profile=ProfileName.CHAT, scope_=s)


def test_chat_sees_shared_only_not_private_phases():
    s = ExecScope(phase=Phase.SHARED)
    assert not visible_in_window(
        _entry("d", phase=Phase.DESIGN), profile=ProfileName.CHAT, scope_=s
    )
    assert not visible_in_window(
        _entry("w", phase=Phase.WRITE), profile=ProfileName.CHAT, scope_=s
    )
    assert visible_in_window(
        _entry("sh", phase=Phase.SHARED), profile=ProfileName.CHAT, scope_=s
    )


# ---------- WP-31 Task 11：ITEM 装配 O(1) / close_batch / repair ----------


class _RecordingSink:
    def __init__(self) -> None:
        self.records: list[JournalRecord] = []

    async def record(self, records: list[JournalRecord]) -> None:
        self.records.extend(records)


def _large_item_entry(i: int, *, body_len: int = 400) -> ContextEntry:
    return _entry(
        f"item:b1:p{i}",
        scope_level=ScopeLevel.ITEM,
        phase=Phase.WRITE,
        batch_id="b1",
        item_key=f"b1:p{i}",
        content=f"item{i}-marker " + "内" * body_len,
        created_at=f"2026-09-28T01:{i:02d}:00.000Z",
    )


async def test_item_assembly_constant_tokens_across_100_items():
    sink = _RecordingSink()
    store = ContextStore(
        owner_type="task", owner_id="t1", workspace_id="ws1", journal=sink
    )
    for i in range(1, 101):
        await store.append(_large_item_entry(i))

    async def assemble_item(i: int):
        async with scope(
            phase=Phase.WRITE, batch_id="b1", item_key=f"b1:p{i}"
        ):
            return await assemble(
                store,
                ProfileName.CASE_ITEM,
                p0_messages=[SystemMessage(content="p0-static")],
                model_window=128_000,
            )

    first = await assemble_item(1)
    last = await assemble_item(100)
    t1 = sum(estimate_tokens(str(m.content)) for m in first.messages)
    t100 = sum(estimate_tokens(str(m.content)) for m in last.messages)
    # O(1)：第 1 条与第 100 条窗口 token 差 <10%
    assert abs(t100 - t1) / max(t1, 1) < 0.1

    last_text = "\n".join(str(m.content) for m in last.messages)
    assert "item100-marker" in last_text
    assert "item99-marker" not in last_text
    first_text = "\n".join(str(m.content) for m in first.messages)
    assert "item1-marker" in first_text
    assert "item2-marker" not in first_text


async def test_close_batch_evicts_items_and_batch_entries():
    sink = _RecordingSink()
    store = ContextStore(
        owner_type="task", owner_id="t1", workspace_id="ws1", journal=sink
    )
    await store.append(_large_item_entry(1))
    await store.append(
        _entry(
            "batch:note",
            scope_level=ScopeLevel.BATCH,
            phase=Phase.WRITE,
            batch_id="b1",
            content="本批批注",
            created_at="2026-09-28T00:00:01.000Z",
        )
    )
    await store.append(
        _entry(
            "batch:other",
            scope_level=ScopeLevel.BATCH,
            phase=Phase.WRITE,
            batch_id="b2",
            content="别的批",
            created_at="2026-09-28T00:00:02.000Z",
        )
    )

    count = await store.close_batch("b1")
    assert count == 2
    assert store.get("item:b1:p1").status is EntryStatus.EVICTED
    assert store.get("batch:note").status is EntryStatus.EVICTED
    assert store.get("batch:other").status is EntryStatus.ACTIVE

    close_markers = [r for r in sink.records if r.action == JournalAction.BATCH_CLOSE]
    assert len(close_markers) == 1
    assert close_markers[0].batch_id == "b1"
    evict_reasons = [
        r.reason for r in sink.records if r.action == JournalAction.EVICT
    ]
    assert evict_reasons == ["batch_closed", "batch_closed"]


async def test_repair_same_item_key_idempotent_preserved():
    sink = _RecordingSink()
    store = ContextStore(
        owner_type="task", owner_id="t1", workspace_id="ws1", journal=sink
    )
    entry = _large_item_entry(7)
    await store.append(entry)
    # repair：同 item_key 同 entry_id 重放
    await store.append(_large_item_entry(7))

    same = [e for e in store.entries() if e.entry_id == "item:b1:p7"]
    assert len(same) == 1
    assert same[0].status is EntryStatus.ACTIVE
    assert same[0].item_key == "b1:p7"
    appends = [r for r in sink.records if r.action == JournalAction.APPEND]
    assert len(appends) == 1  # 第二次 append 幂等，不产生 journal
