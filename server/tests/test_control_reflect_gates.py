"""Reflexion decision table and human-gate mapping."""

from tester_agent.domain import AgentPlan, PlanStep, PlanStepKind
from tester_agent.graph.control.reflect import decide_reflection


def _coverage_step(**kw) -> PlanStep:
    base = dict(
        step_id="s1",
        kind=PlanStepKind.COVERAGE_DESIGN,
        goal="x",
        requires_confirm=True,
        status="done",
        output_ref="art1",
    )
    base.update(kw)
    return PlanStep(**base)


def _plan(step: PlanStep | None = None) -> AgentPlan:
    return AgentPlan(
        plan_id="p",
        version=1,
        goal="g",
        steps=[step or _coverage_step()],
    )


def test_skipping_link_gate_forces_replan():
    step = _coverage_step()
    d = decide_reflection(
        _plan(step),
        step,
        reflection_count=0,
        replan_count=0,
        human_gates={"link": True, "point": True, "review": True},
        artifact_confirmed_by=None,
    )
    assert d == "replan"


def test_confirmed_gate_passes():
    step = _coverage_step()
    d = decide_reflection(
        _plan(step),
        step,
        reflection_count=0,
        replan_count=0,
        human_gates={"link": True, "point": True, "review": True},
        artifact_confirmed_by="user",
    )
    assert d == "pass"


def test_failed_step_repairs_within_max():
    step = _coverage_step(status="failed", requires_confirm=False)
    d = decide_reflection(
        _plan(step),
        step,
        reflection_count=0,
        replan_count=0,
        human_gates={"link": False, "point": False, "review": False},
        artifact_confirmed_by=None,
    )
    assert d == "repair"


def test_failed_step_replans_when_reflect_exhausted():
    step = _coverage_step(status="failed", requires_confirm=False, max_reflect=2)
    d = decide_reflection(
        _plan(step),
        step,
        reflection_count=2,
        replan_count=0,
        human_gates={"link": False, "point": False, "review": False},
        artifact_confirmed_by=None,
    )
    assert d == "replan"
