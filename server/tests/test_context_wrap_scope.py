"""Wave 0.1：wrap / invoke_capability 推入 ExecScope（spec §15.6 防线 1）。

- map_phase：kind → DESIGN/WRITE；await_human 不覆盖父 phase；
- wrap：从 agent_plan + plan_cursor 解析 running step 后压栈；
- invoke_capability：生产路径同样压栈（控制环不经 wrap）；
- store.append 未显式传 scope 时继承 current_scope。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryRefs,
    Phase,
)
from tester_agent.context.scopes import current_scope
from tester_agent.context.store import ContextStore
from tester_agent.domain import AgentPlan, PlanStep, PlanStepKind
from tester_agent.graph.wrap import map_phase, resolve_scope_kwargs, wrap
from tester_agent.tools.capabilities import invoke_capability


# ---------- map_phase ----------


@pytest.mark.parametrize(
    "kind,expected",
    [
        (PlanStepKind.INTAKE_PARSE, Phase.DESIGN),
        (PlanStepKind.COVERAGE_DESIGN, Phase.DESIGN),
        (PlanStepKind.POINT_DESIGN, Phase.DESIGN),
        (PlanStepKind.CASE_GENERATE, Phase.WRITE),
        (PlanStepKind.REPAIR, Phase.WRITE),
        (PlanStepKind.REVIEW_COVERAGE, Phase.WRITE),
        (PlanStepKind.REVIEW_QUALITY, Phase.WRITE),
        (PlanStepKind.REVIEW_ADOPTION, Phase.WRITE),
        (PlanStepKind.AWAIT_HUMAN, None),
    ],
)
def test_map_phase_table(kind, expected):
    assert map_phase(kind) is expected


# ---------- resolve_scope_kwargs ----------


def _plan(*steps: PlanStep) -> dict:
    return AgentPlan(
        plan_id="p1", version=1, goal="g", steps=list(steps)
    ).model_dump()


def test_resolve_scope_kwargs_from_running_step():
    state = {
        "agent_plan": _plan(
            PlanStep(step_id="s0", kind=PlanStepKind.INTAKE_PARSE, goal="g0"),
            PlanStep(step_id="s1", kind=PlanStepKind.CASE_GENERATE, goal="g1"),
        ),
        "plan_cursor": "s1",
    }
    kw = resolve_scope_kwargs(state)
    assert kw["phase"] is Phase.WRITE
    assert kw["step_id"] == "s1"
    assert kw["step_seq"] == 1


def test_resolve_scope_kwargs_await_human_omits_phase():
    state = {
        "agent_plan": _plan(
            PlanStep(step_id="s0", kind=PlanStepKind.AWAIT_HUMAN, goal="wait"),
        ),
        "plan_cursor": "s0",
    }
    kw = resolve_scope_kwargs(state)
    assert "phase" not in kw
    assert kw["step_id"] == "s0"
    assert kw["step_seq"] == 0


def test_resolve_scope_kwargs_missing_plan_empty():
    assert resolve_scope_kwargs({}) == {}
    assert resolve_scope_kwargs({"plan_cursor": "s1"}) == {}


# ---------- wrap 压栈 ----------


def _cfg(ctx: Any) -> dict:
    return {"configurable": {"ctx": ctx}}


def _make_ctx() -> Any:
    async def _cancelled() -> bool:
        return False

    async def _emit(type_: str, payload: dict) -> None:
        return None

    return SimpleNamespace(cancelled=_cancelled, emit=_emit)


async def test_wrap_pushes_design_scope_for_intake_step():
    seen: dict[str, Any] = {}

    async def node(ctx, state):
        s = current_scope()
        seen["phase"] = s.phase
        seen["step_id"] = s.step_id
        seen["step_seq"] = s.step_seq
        return {"ok": True}

    state = {
        "agent_plan": _plan(
            PlanStep(step_id="s0", kind=PlanStepKind.INTAKE_PARSE, goal="parse"),
        ),
        "plan_cursor": "s0",
    }
    wrapped = wrap(node, name="intake")
    await wrapped(state, _cfg(_make_ctx()))
    assert seen == {
        "phase": Phase.DESIGN,
        "step_id": "s0",
        "step_seq": 0,
    }
    # 弹栈后恢复默认
    assert current_scope().step_id is None
    assert current_scope().phase is Phase.SHARED


async def test_wrap_pops_scope_on_node_error():
    async def boom(ctx, state):
        assert current_scope().step_id == "s0"
        raise RuntimeError("x")

    state = {
        "agent_plan": _plan(
            PlanStep(step_id="s0", kind=PlanStepKind.POINT_DESIGN, goal="pts"),
        ),
        "plan_cursor": "s0",
    }
    wrapped = wrap(boom, name="point_write")
    with pytest.raises(Exception):
        await wrapped(state, _cfg(_make_ctx()))
    assert current_scope().step_id is None


async def test_wrap_without_plan_keeps_shared_default():
    seen: list[Phase] = []

    async def node(ctx, state):
        seen.append(current_scope().phase)
        return {}

    wrapped = wrap(node, name="intake")
    await wrapped({}, _cfg(_make_ctx()))
    assert seen == [Phase.SHARED]


# ---------- invoke_capability 压栈 ----------


async def test_invoke_capability_pushes_write_scope():
    seen: dict[str, Any] = {}

    async def fake_case(ctx, state):
        s = current_scope()
        seen["phase"] = s.phase
        seen["step_id"] = s.step_id
        seen["step_seq"] = s.step_seq
        return {"case_count": 0, "case_ids": []}

    state = {
        "agent_plan": _plan(
            PlanStep(step_id="s0", kind=PlanStepKind.COVERAGE_DESIGN, goal="d"),
            PlanStep(step_id="s1", kind=PlanStepKind.CASE_GENERATE, goal="c"),
        ),
        "plan_cursor": "s1",
    }
    ctx = SimpleNamespace(daos=None, task=SimpleNamespace(id="t1"))
    await invoke_capability(
        PlanStepKind.CASE_GENERATE,
        ctx=ctx,
        state=state,
        nodes={PlanStepKind.CASE_GENERATE: fake_case},
    )
    assert seen == {
        "phase": Phase.WRITE,
        "step_id": "s1",
        "step_seq": 1,
    }
    assert current_scope().step_id is None


# ---------- append 继承 current_scope ----------


async def test_append_inherits_current_scope_when_unspecified():
    store = ContextStore(
        owner_type="task", owner_id="t1", workspace_id="ws1"
    )
    from tester_agent.context.scopes import scope

    async with scope(phase=Phase.DESIGN, step_id="s9", step_seq=3):
        ok = await store.append(
            ContextEntry(
                entry_id="e1",
                partition=ContextPartition.P2,
                entry_kind=EntryKind.REFLECTION,
                content="note",
                digest="note",
                refs=EntryRefs(),
                created_at="2026-09-29T00:00:00.000Z",
            )
        )
    assert ok is True
    e = store.get("e1")
    assert e.phase is Phase.DESIGN
    assert e.step_id == "s9"
    assert e.step_seq == 3
