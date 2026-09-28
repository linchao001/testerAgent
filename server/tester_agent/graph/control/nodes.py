"""Control-loop node implementations (stub capabilities until Task 5)."""

from __future__ import annotations

import uuid
from typing import Any

from ...domain import AgentPlan, PlanStep, PlanStepKind


def _default_steps() -> list[PlanStep]:
    return [
        PlanStep(step_id="s1", kind=PlanStepKind.INTAKE_PARSE, goal="parse requirement"),
        PlanStep(
            step_id="s2",
            kind=PlanStepKind.COVERAGE_DESIGN,
            goal="coverage / link skeleton",
            requires_confirm=True,
        ),
        PlanStep(
            step_id="s3",
            kind=PlanStepKind.POINT_DESIGN,
            goal="test points",
            requires_confirm=True,
        ),
        PlanStep(step_id="s4", kind=PlanStepKind.CASE_GENERATE, goal="generate cases"),
        PlanStep(
            step_id="s5",
            kind=PlanStepKind.REVIEW_COVERAGE,
            goal="coverage review",
            requires_confirm=True,
        ),
        PlanStep(
            step_id="s6",
            kind=PlanStepKind.REVIEW_QUALITY,
            goal="quality review",
            requires_confirm=True,
        ),
        PlanStep(
            step_id="s7",
            kind=PlanStepKind.REVIEW_ADOPTION,
            goal="adoption review",
            requires_confirm=True,
        ),
    ]


def plan_node(state: dict) -> dict[str, Any]:
    """Generate a deterministic initial plan when missing; keep existing on replan stub."""
    existing = state.get("agent_plan")
    if existing and existing.get("status") in ("active", "draft"):
        # replan stub: bump version / replan_count; keep unfinished steps pending
        plan = AgentPlan.model_validate(existing)
        plan.version += 1
        plan.replan_count += 1
        for step in plan.steps:
            if step.status == "failed":
                step.status = "pending"
        return {"agent_plan": plan.model_dump()}

    plan = AgentPlan(
        plan_id=f"plan-{uuid.uuid4().hex[:12]}",
        version=1,
        goal="generate test cases with coverage",
        steps=_default_steps(),
        status="active",
    )
    gates = state.get("human_gates") or {
        "link": True,
        "point": True,
        "review": True,
    }
    return {
        "agent_plan": plan.model_dump(),
        "plan_cursor": None,
        "human_gates": gates,
        "artifacts": state.get("artifacts") or {},
        "reflection_log": state.get("reflection_log") or [],
    }


def _pending_step(plan: AgentPlan) -> PlanStep | None:
    for step in plan.steps:
        if step.status == "pending":
            return step
    return None


def dispatch_node(state: dict) -> dict[str, Any]:
    plan = AgentPlan.model_validate(state["agent_plan"])
    nxt = _pending_step(plan)
    if nxt is None:
        plan.status = "completed"
        return {"agent_plan": plan.model_dump(), "plan_cursor": None}
    return {"plan_cursor": nxt.step_id}


def route_after_dispatch(state: dict) -> str:
    plan = AgentPlan.model_validate(state["agent_plan"])
    if plan.status == "completed" or not state.get("plan_cursor"):
        return "end"
    return "execute"


def execute_step_node(state: dict) -> dict[str, Any]:
    """Stub execute: mark current step done (real capabilities in Task 5)."""
    plan = AgentPlan.model_validate(state["agent_plan"])
    cursor = state.get("plan_cursor")
    artifacts = dict(state.get("artifacts") or {})
    for step in plan.steps:
        if step.step_id == cursor:
            step.status = "running"
            # stub artifact
            art_id = f"art-{step.step_id}-{uuid.uuid4().hex[:8]}"
            artifacts[art_id] = {
                "kind": step.kind.value,
                "version": 1,
                "payload_ref": art_id,
            }
            step.output_ref = art_id
            step.status = "done"
            break
    return {
        "agent_plan": plan.model_dump(),
        "artifacts": artifacts,
    }


def reflect_node(state: dict) -> dict[str, Any]:
    """Pass-through reflect (real rules in Task 4)."""
    log = list(state.get("reflection_log") or [])
    cursor = state.get("plan_cursor")
    log.append({"step_id": cursor, "decision": "pass"})
    log = log[-20:]
    plan = AgentPlan.model_validate(state["agent_plan"])
    updates: dict[str, Any] = {
        "reflection_log": log,
        "_reflect_decision": "pass",
    }
    if _pending_step(plan) is None:
        plan.status = "completed"
        updates["agent_plan"] = plan.model_dump()
        updates["plan_cursor"] = None
    return updates


def route_after_reflect(state: dict) -> str:
    decision = state.get("_reflect_decision") or "pass"
    if decision == "replan":
        return "plan"
    if decision == "repair":
        return "execute"
    if decision == "pass":
        plan = AgentPlan.model_validate(state["agent_plan"])
        if _pending_step(plan) is None:
            return "end"
        return "dispatch"
    return "dispatch"
