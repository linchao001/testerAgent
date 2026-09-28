"""context.assembler：唯一组装入口四步管线（spec §5/§4.5）。"""

from __future__ import annotations

import hashlib

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from tester_agent.context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryStatus,
    Phase,
    ProfileName,
    ScopeLevel,
)
from tester_agent.context.scopes import ExecScope
from tester_agent.context.store import ContextStore
from tester_agent.context.assembler import AssemblyResult, assemble

MODEL_WINDOW = 128_000
P0 = [SystemMessage(content="你是用例设计助手。规则集 v1。")]


def _entry(eid, **kw) -> ContextEntry:
    base = dict(
        entry_id=eid,
        partition=ContextPartition.P2,
        entry_kind=EntryKind.ARTIFACT_DIGEST,
        content="条目正文",
        digest="摘要",
        created_at="2026-09-28T00:00:00.000Z",
    )
    base.update(kw)
    return ContextEntry(**base)


def _store() -> ContextStore:
    return ContextStore(
        owner_type="task", owner_id="t1", workspace_id="ws1", journal=None
    )


def _contents(result: AssemblyResult) -> list[str]:
    return [m.content for m in result.messages]


async def _seed(store: ContextStore, *entries: ContextEntry) -> None:
    for e in entries:
        await store.append(e)


async def test_p0_segment_byte_stable_and_first():
    store = _store()
    r1 = await assemble(store, ProfileName.PLAN, p0_messages=P0, model_window=MODEL_WINDOW)
    r2 = await assemble(store, ProfileName.PLAN, p0_messages=P0, model_window=MODEL_WINDOW)
    assert isinstance(r1.messages[0], SystemMessage)
    assert r1.messages[0].content == P0[0].content
    assert r1.report.p0_version == ""
    h1 = hashlib.sha256(str(_contents(r1)).encode()).hexdigest()
    h2 = hashlib.sha256(str(_contents(r2)).encode()).hexdigest()
    assert h1 == h2


async def test_p1_ordered_by_recent_mount_step_and_deduped():
    store = _store()
    await _seed(
        store,
        _entry("kb1", partition=ContextPartition.P1, entry_kind=EntryKind.KB_BLOCK,
               content="旧知识块", digest="旧", step_seq=1, tokens_est=3),
        _entry("kb2", partition=ContextPartition.P1, entry_kind=EntryKind.KB_BLOCK,
               content="新知识块", digest="新", step_seq=2, tokens_est=3),
    )
    r = await assemble(
        store, ProfileName.EXECUTE, p0_messages=P0, model_window=MODEL_WINDOW,
        scope=ExecScope(phase=Phase.WRITE, step_seq=2),
    )
    body = "\n".join(_contents(r))
    assert body.index("新知识块") < body.index("旧知识块")
    assert r.report.per_partition_tokens["P1"] == 6


async def test_p2_pinned_always_included_and_chat_recent_k_whole():
    store = _store()
    # 8 轮对话，K=6；另一个 pinned 决策
    for i in range(1, 9):
        await _seed(
            store,
            _entry(
                f"chat:u{i}", entry_kind=EntryKind.CHAT_TURN, content=f"用户{i}问",
                digest=f"u{i}", turn_seq=i, role="user",
                created_at=f"2026-09-28T00:00:{i:02d}.000Z", tokens_est=3,
            ),
        )
        await _seed(
            store,
            _entry(
                f"chat:a{i}", entry_kind=EntryKind.CHAT_TURN, content=f"助手{i}答",
                digest=f"a{i}", turn_seq=i, role="assistant",
                created_at=f"2026-09-28T00:00:{i+10:02d}.000Z", tokens_est=3,
            ),
        )
    await _seed(
        store,
        _entry("dec1", entry_kind=EntryKind.DECISION, content="只测支付模块", digest="支付决策",
               pinned=True, tokens_est=5),
    )
    r = await assemble(
        store, ProfileName.CHAT, p0_messages=P0, model_window=MODEL_WINDOW,
        scope=ExecScope(turn_seq=8), recent_turns=6,
    )
    body = _contents(r)
    # 最近 6 轮（3~8）完整
    assert "用户3问" in body and "助手8答" in body
    assert "用户1问" not in body and "助手2答" not in body
    # 对话消息保持 Human/AI 类型且按轮次顺序
    chat_msgs = [m for m in r.messages if isinstance(m, (HumanMessage, AIMessage))]
    assert isinstance(chat_msgs[0], HumanMessage)
    assert isinstance(chat_msgs[1], AIMessage)
    # pinned 决策在窗
    assert "只测支付模块" in body
    assert "dec1" in r.report.included


async def test_demoted_render_as_tombstone_section_max_20_plus_count():
    store = _store()
    for i in range(22):
        e = _entry(
            f"old{i}", entry_kind=EntryKind.REFLECTION, content=f"反思{i}",
            digest=f"反思要点{i}",
            created_at=f"2026-09-28T00:00:{i:02d}.000Z", tokens_est=2,
        )
        await store.append(e)
        await store.demote(f"old{i}", "step_window")
    r = await assemble(store, ProfileName.REFLECT, p0_messages=P0, model_window=MODEL_WINDOW)
    body = "\n".join(_contents(r))
    assert "已归档 reflection #old0" in body
    assert "其余 2 条已归档" in body
    assert r.report.demoted_shown.__len__() == 20


async def test_budget_cut_evicts_lowest_with_reason_and_report():
    store = _store()
    # execute p2 容量 6700；每条 7500 → 尾部截断
    await _seed(
        store,
        _entry(
            "big1", content="无关旧讨论" * 1500, digest="无关1", step_seq=1,
            created_at="2026-09-28T00:00:01.000Z",
        ),
        _entry(
            "big2", content="无关旧讨论" * 1500, digest="无关2", step_seq=1,
            created_at="2026-09-28T00:00:02.000Z",
        ),
        _entry(
            "related", content="支付模块测试用例设计" * 50, digest="相关", step_seq=2,
            created_at="2026-09-28T00:00:03.000Z", pinned=True,
        ),
    )
    r = await assemble(
        store, ProfileName.PLAN, p0_messages=P0, model_window=MODEL_WINDOW,
        scope=ExecScope(phase=Phase.DESIGN, step_seq=2),
        goal="支付模块测试用例设计",
    )
    cut = {x.entry_id: x.reason for x in r.report.evicted_in_assembly}
    assert "big1" in cut and "big2" in cut
    assert "related" not in cut  # pinned
    # 已在 store 落 demote
    assert store.get("big1").status is EntryStatus.DEMOTED
    assert "big1" not in r.report.included


async def test_pinned_over_budget_gets_override_not_cut():
    store = _store()
    await _seed(
        store,
        _entry("pin-huge", content="支付模块测试" * 2000, digest="大", pinned=True, step_seq=2),
    )
    r = await assemble(
        store, ProfileName.CASE_ITEM, p0_messages=P0, model_window=MODEL_WINDOW,
        scope=ExecScope(phase=Phase.WRITE, step_seq=2), goal="支付模块测试",
    )
    assert "pin-huge" in r.report.budget_overrides
    assert "pin-huge" in r.report.included


async def test_assembly_is_deterministic():
    store = _store()
    await _seed(
        store,
        _entry("a", content="支付模块测试用例", step_seq=2,
               created_at="2026-09-28T00:00:01.000Z"),
        _entry("b", content="支付模块测试用例", step_seq=2,
               created_at="2026-09-28T00:00:02.000Z"),
    )
    kw = dict(p0_messages=P0, model_window=MODEL_WINDOW,
              scope=ExecScope(phase=Phase.WRITE, step_seq=2), goal="支付模块测试用例")
    r1 = await assemble(store, ProfileName.CASE_ITEM, **kw)
    r2 = await assemble(store, ProfileName.CASE_ITEM, **kw)
    assert _contents(r1) == _contents(r2)
    assert r1.report.included == r2.report.included


async def test_case_item_window_excludes_design_and_previous_items():
    store = _store()
    await _seed(
        store,
        _entry("design-draft", phase=Phase.DESIGN, step_id="s1", content="设计草稿"),
        _entry("outline", entry_kind=EntryKind.OUTLINE_DIGEST, phase=Phase.SHARED,
               content="确认大纲摘要"),
        _entry("item-prev", scope_level=ScopeLevel.ITEM, phase=Phase.WRITE,
               batch_id="b1", item_key="b1:p1", content="上一条用例过程"),
        _entry("item-cur", scope_level=ScopeLevel.ITEM, phase=Phase.WRITE,
               batch_id="b1", item_key="b1:p2", content="当前用例过程"),
        _entry("batch-info", scope_level=ScopeLevel.BATCH, phase=Phase.WRITE,
               batch_id="b1", content="当前测试点全文"),
    )
    r = await assemble(
        store, ProfileName.CASE_ITEM, p0_messages=P0, model_window=MODEL_WINDOW,
        scope=ExecScope(phase=Phase.WRITE, batch_id="b1", item_key="b1:p2"),
    )
    body = _contents(r)
    assert "设计草稿" not in body
    assert "上一条用例过程" not in body
    assert "确认大纲摘要" in body
    assert "当前用例过程" in body
    assert "当前测试点全文" in body


async def test_frozen_store_skips_cutting():
    store = _store()
    store.frozen = True
    await _seed(
        store,
        _entry("big1", content="无关旧讨论" * 200, digest="无关1", step_seq=1,
               created_at="2026-09-28T00:00:01.000Z"),
    )
    r = await assemble(
        store, ProfileName.PLAN, p0_messages=P0, model_window=MODEL_WINDOW,
        scope=ExecScope(phase=Phase.DESIGN, step_seq=9), goal="支付模块测试",
    )
    assert r.report.frozen is True
    assert r.report.evicted_in_assembly == []
    assert "big1" in r.report.included
    assert store.get("big1").status is EntryStatus.ACTIVE


async def test_goal_drift_entries_cut_with_drift_reason():
    store = _store()
    await _seed(
        store,
        _entry("drift", content="量子力学讨论zzzzzz" * 1500, digest="漂移", step_seq=1,
               created_at="2026-09-28T00:00:01.000Z"),
    )
    r = await assemble(
        store, ProfileName.PLAN, p0_messages=P0, model_window=MODEL_WINDOW,
        scope=ExecScope(phase=Phase.DESIGN, step_seq=9), goal="支付模块测试",
    )
    reasons = {x.entry_id: x.reason for x in r.report.evicted_in_assembly}
    # 无论因预算还是漂移出窗，漂移条目必须带 goal_drift 归因
    assert reasons.get("drift") == "goal_drift"


async def test_scope_filter_applies_before_tombstone():
    store = _store()
    e = _entry("design-old", phase=Phase.DESIGN, content="设计期旧反思", digest="设计旧")
    await store.append(e)
    await store.demote("design-old", "step_window")
    r = await assemble(
        store, ProfileName.CASE_ITEM, p0_messages=P0, model_window=MODEL_WINDOW,
        scope=ExecScope(phase=Phase.WRITE, batch_id="b1", item_key="b1:p1"),
    )
    assert "设计期旧反思" not in _contents(r)
    assert "design-old" not in r.report.demoted_shown


async def test_persist_false_does_not_mutate_store_playground():
    store = _store()
    await _seed(
        store,
        _entry("big1", content="无关旧讨论" * 1500, digest="无关1", step_seq=1,
               created_at="2026-09-28T00:00:01.000Z"),
    )
    r = await assemble(
        store, ProfileName.PLAN, p0_messages=P0, model_window=MODEL_WINDOW,
        scope=ExecScope(phase=Phase.DESIGN, step_seq=2), goal="支付",
        persist_evictions=False,
    )
    assert r.report.evicted_in_assembly  # 本次窗口确实裁了
    assert store.get("big1").status is EntryStatus.ACTIVE  # 但零写入


async def test_invalid_model_window_rejected():
    store = _store()
    with pytest.raises(Exception):
        await assemble(store, ProfileName.PLAN, p0_messages=P0, model_window=0)
