"""WP-32 Task 14：控制环 T1 钩子（step 结束 P1 demote / replan / repair / confirm pin）。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryRefs,
    EntryStatus,
    Phase,
    ScopeLevel,
)
from tester_agent.context.store import ContextStore
from tester_agent.domain import AgentPlan, PlanStep, PlanStepKind
from tester_agent.graph.control.context_t1 import (
    on_await_human_confirmed,
    on_plan_updated,
    on_reflect_decision,
    on_step_completed,
)
from tester_agent.graph.control import nodes as control_nodes
from tester_agent.graph.wrap import map_phase


TS = "2026-09-29T12:00:00.000Z"


def _entry(eid: str, **kw) -> ContextEntry:
    base = dict(
        entry_id=eid,
        partition=ContextPartition.P1,
        entry_kind=EntryKind.KB_BLOCK,
        content="kb",
        digest="kb",
        created_at=TS,
        refs=EntryRefs(),
    )
    base.update(kw)
    return ContextEntry(**base)


async def _store() -> ContextStore:
    return ContextStore(owner_type="task", owner_id="t1", workspace_id="ws1")


# ---------- map_phase（wrap 已覆盖，再锁控制环口径） ----------


def test_map_phase_control_kinds():
    assert map_phase(PlanStepKind.COVERAGE_DESIGN) is Phase.DESIGN
    assert map_phase(PlanStepKind.CASE_GENERATE) is Phase.WRITE
    assert map_phase(PlanStepKind.AWAIT_HUMAN) is None


# ---------- on_step_completed：P1 step_window demote ----------


async def test_step_completed_demotes_p1_outside_window():
    store = await _store()
    await store.append(
        _entry("kb:old", step_id="s0", step_seq=0, phase=Phase.DESIGN)
    )
    await store.append(
        _entry("kb:cur", step_id="s1", step_seq=1, phase=Phase.DESIGN)
    )
    step = PlanStep(
        step_id="s1",
        kind=PlanStepKind.COVERAGE_DESIGN,
        goal="g",
        status="done",
        output_ref="art1",
    )
    await on_step_completed(
        store,
        step=step,
        step_seq=1,
        artifact_id="art1",
        artifact_kind="coverage_design",
        payload={"links": []},
        step_window=1,
    )
    assert store.get("kb:old").status is EntryStatus.DEMOTED
    assert store.get("kb:cur").status is EntryStatus.ACTIVE
    dig = store.get("artifact:art1")
    assert dig.entry_kind is EntryKind.ARTIFACT_DIGEST
    assert dig.phase is Phase.DESIGN


# ---------- on_plan_updated：旧 PLAN superseded ----------


async def test_plan_updated_demotes_old_plan_pins_new():
    store = await _store()
    await store.append(
        ContextEntry(
            entry_id="artifact:plan-old",
            partition=ContextPartition.P2,
            entry_kind=EntryKind.PLAN,
            content="old",
            digest="old",
            pinned=True,
            created_at=TS,
            refs=EntryRefs(payload_ref="plan-old"),
        )
    )
    plan = AgentPlan(
        plan_id="p2",
        version=2,
        goal="new goal",
        steps=[
            PlanStep(step_id="s1", kind=PlanStepKind.INTAKE_PARSE, goal="x"),
        ],
    )
    await on_plan_updated(store, plan=plan, plan_artifact_id="plan-new")
    assert store.get("artifact:plan-old").status is EntryStatus.DEMOTED
    assert store.get("artifact:plan-old").pinned is False
    neu = store.get("artifact:plan-new")
    assert neu.entry_kind is EntryKind.PLAN
    assert neu.pinned is True
    assert neu.content == "new goal"


# ---------- on_reflect_decision：repair 归档旧 reflection；去掉截断依赖 ----------


async def test_reflect_repair_demotes_prior_reflection():
    store = await _store()
    await store.append(
        ContextEntry(
            entry_id="refl:s1:0",
            partition=ContextPartition.P2,
            entry_kind=EntryKind.REFLECTION,
            content="first",
            digest="first",
            step_id="s1",
            created_at=TS,
            refs=EntryRefs(),
        )
    )
    step = PlanStep(
        step_id="s1",
        kind=PlanStepKind.CASE_GENERATE,
        goal="g",
        status="pending",
    )
    await on_reflect_decision(
        store, step=step, decision="repair", reflection_count=1
    )
    assert store.get("refl:s1:0").status is EntryStatus.DEMOTED
    assert store.get("refl:s1:1").entry_kind is EntryKind.REFLECTION


async def test_reflect_node_no_longer_truncates_log_at_20():
    """reflection_log 不再 [-20:] 截断（策略接管窗口）。"""
    plan = AgentPlan(
        plan_id="p",
        version=1,
        goal="g",
        steps=[
            PlanStep(
                step_id="s1",
                kind=PlanStepKind.COVERAGE_DESIGN,
                goal="x",
                status="done",
                output_ref="a1",
                requires_confirm=True,
            )
        ],
    )
    log = [{"step_id": "s0", "decision": "pass", "i": i} for i in range(25)]
    state = {
        "agent_plan": plan.model_dump(),
        "plan_cursor": "s1",
        "artifacts": {"a1": {"confirmed_by": "user"}},
        "human_gates": {"link": True, "point": True, "review": True},
        "reflection_log": log,
        "reflect_counts": {},
    }
    out = await control_nodes.reflect_node(state)
    # 原 25 + 本轮 1 = 26，若仍 [-20:] 则只有 20
    assert len(out["reflection_log"]) == 26


# ---------- confirm pin + outline digest ----------


async def test_await_human_confirmed_pins_and_outline_for_coverage():
    store = await _store()
    await store.append(
        ContextEntry(
            entry_id="artifact:art1",
            partition=ContextPartition.P2,
            entry_kind=EntryKind.ARTIFACT_DIGEST,
            content="draft outline",
            digest="draft",
            phase=Phase.DESIGN,
            created_at=TS,
            refs=EntryRefs(payload_ref="art1"),
        )
    )
    await store.append(
        ContextEntry(
            entry_id="outline:old",
            partition=ContextPartition.P2,
            entry_kind=EntryKind.OUTLINE_DIGEST,
            content="old outline",
            digest="old",
            phase=Phase.SHARED,
            pinned=True,
            created_at=TS,
            refs=EntryRefs(),
        )
    )
    await on_await_human_confirmed(
        store,
        artifact_id="art1",
        artifact_kind="coverage_design",
        payload={"stories": [{"id": "st1", "title": "支付"}]},
    )
    assert store.get("artifact:art1").pinned is True
    assert store.get("outline:old").status is EntryStatus.DEMOTED
    outline = [
        e
        for e in store.entries()
        if e.entry_kind is EntryKind.OUTLINE_DIGEST and e.status is EntryStatus.ACTIVE
    ]
    assert len(outline) == 1
    assert outline[0].phase is Phase.SHARED
    assert outline[0].pinned is True
    # 编写窗口可见 shared outline，design 草稿条目 phase 仍为 DESIGN
    assert store.get("artifact:art1").phase is Phase.DESIGN


# ---------- execute_step 接线（ctx.context_store） ----------


async def test_execute_step_invokes_t1_when_store_present(monkeypatch):
    store = await _store()
    await store.append(_entry("kb:x", step_id="s0", step_seq=0))

    async def fake_cap(kind, *, ctx, state, nodes=None):
        from tester_agent.tools.capabilities import CapabilityOutcome

        return CapabilityOutcome(
            artifact_id="art-x",
            artifact_kind="point_plan",
            increment={},
            payload={"points": []},
            version=1,
        )

    monkeypatch.setattr(control_nodes, "invoke_capability", fake_cap)
    plan = AgentPlan(
        plan_id="p",
        version=1,
        goal="g",
        steps=[
            PlanStep(
                step_id="s0",
                kind=PlanStepKind.INTAKE_PARSE,
                goal="parse",
                status="done",
            ),
            PlanStep(
                step_id="s1",
                kind=PlanStepKind.POINT_DESIGN,
                goal="pts",
                status="pending",
            ),
        ],
    )
    ctx = SimpleNamespace(
        context_store=store,
        runtime_config={"context.step_window": 1},
    )
    state = {
        "agent_plan": plan.model_dump(),
        "plan_cursor": "s1",
        "artifacts": {},
    }
    await control_nodes.execute_step_node(
        state, config={"configurable": {"ctx": ctx}}
    )
    # s1 的 step_seq=1，window=1 → demote step_seq<=0 的 P1
    assert store.get("kb:x").status is EntryStatus.DEMOTED
    assert store.get("artifact:art-x").entry_kind is EntryKind.ARTIFACT_DIGEST
