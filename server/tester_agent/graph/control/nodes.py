"""Control-loop node implementations (capability dispatch + human gates)."""

# 注意：不能加 ``from __future__ import annotations`` —— LangGraph 要求
# config 参数注解为 RunnableConfig 本体（与 wrap.py 同理）。

import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from ...domain import AgentPlan, PlanStep, PlanStepKind, ReviewProposal
from ...tools.capabilities import invoke_capability, legacy_state_from_artifact
from .gates import gate_enabled
from .reflect import decide_reflection


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


def _step_by_id(plan: AgentPlan, step_id: str | None) -> PlanStep | None:
    if not step_id:
        return None
    for step in plan.steps:
        if step.step_id == step_id:
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


def _ctx_from_config(config: RunnableConfig | None) -> Any | None:
    if not config:
        return None
    configurable = config.get("configurable") or {}
    return configurable.get("ctx")


def _store_review_artifact(
    step: PlanStep,
    artifacts: dict[str, Any],
    proposal: ReviewProposal,
    *,
    id_prefix: str,
) -> None:
    """Attach a review_proposal artifact and mark the step done."""
    art_id = step.output_ref or f"{id_prefix}-{uuid.uuid4().hex[:8]}"
    artifacts[art_id] = {
        "kind": "review_proposal",
        "version": 1,
        "payload_ref": art_id,
        "confirmed_by": None,
        "payload": proposal.model_dump(),
    }
    step.output_ref = art_id
    step.status = "done"


def _build_review_proposal(
    step: PlanStep,
) -> tuple[ReviewProposal, str] | None:
    """Return (proposal, id_prefix) for review kinds; None for capability steps."""
    from ..subtasks.review_adoption import build_adoption_proposal
    from ..subtasks.review_coverage import build_coverage_proposal
    from ..subtasks.review_quality import build_quality_proposal

    if step.kind == PlanStepKind.REVIEW_COVERAGE:
        return (
            build_coverage_proposal(
                uncovered_clause_ids=list(step.input_refs or []),
                matrix_ref=None,
            ),
            "art-review-cov",
        )
    if step.kind == PlanStepKind.REVIEW_QUALITY:
        return build_quality_proposal(case_issues=[]), "art-review-qual"
    if step.kind == PlanStepKind.REVIEW_ADOPTION:
        return build_adoption_proposal(cases=[]), "art-review-ad"
    return None


async def execute_step_node(
    state: dict,
    config: RunnableConfig | None = None,
    *,
    nodes: dict | None = None,
) -> dict[str, Any]:
    """Execute current step via real stage nodes (or stub without TaskContext)."""
    plan = AgentPlan.model_validate(state["agent_plan"])
    cursor = state.get("plan_cursor")
    artifacts = dict(state.get("artifacts") or {})
    ctx = _ctx_from_config(config)
    out: dict[str, Any] = {
        "agent_plan": plan.model_dump(),
        "artifacts": artifacts,
    }
    for step in plan.steps:
        if step.step_id != cursor:
            continue
        if step.status not in ("pending", "running"):
            break
        review = _build_review_proposal(step)
        if review is not None:
            proposal, prefix = review
            _store_review_artifact(step, artifacts, proposal, id_prefix=prefix)
        else:
            outcome = await invoke_capability(
                step.kind, ctx=ctx, state=state, nodes=nodes
            )
            art_id = step.output_ref or outcome.artifact_id
            artifacts[art_id] = {
                "kind": outcome.artifact_kind,
                "version": outcome.version,
                "payload_ref": art_id,
                "confirmed_by": None,
                "payload": outcome.payload,
            }
            step.output_ref = art_id
            step.status = "done"
            out.update(outcome.increment)
        break
    out["agent_plan"] = plan.model_dump()
    out["artifacts"] = artifacts
    return out


def await_human_node(state: dict) -> dict[str, Any]:
    """Interrupt when step requires an enabled human gate and is not confirmed."""
    plan = AgentPlan.model_validate(state["agent_plan"])
    step = _step_by_id(plan, state.get("plan_cursor"))
    if step is None or not step.requires_confirm:
        return {}
    if not gate_enabled(state.get("human_gates"), step.kind):
        return {}
    artifacts = dict(state.get("artifacts") or {})
    art_id = step.output_ref
    if not art_id:
        return {}
    art = dict(artifacts.get(art_id) or {})
    if art.get("confirmed_by") == "user":
        return {}

    gate_kind = (
        "review_decision"
        if step.kind.value.startswith("review_")
        else "plan_confirm"
    )
    resume = interrupt(
        {
            "gate_kind": gate_kind,
            "artifact_id": art_id,
            "step_id": step.step_id,
            "kind": step.kind.value,
        }
    )
    if isinstance(resume, dict):
        if resume.get("action") == "modify" and resume.get("payload") is not None:
            art["payload"] = resume["payload"]
            art["version"] = int(art.get("version") or 1) + 1
        art["confirmed_by"] = "user"
    else:
        # Bare resume (ainvoke None after API marked confirm in state)
        art["confirmed_by"] = art.get("confirmed_by") or "user"
    artifacts[art_id] = art
    synced = legacy_state_from_artifact(step.kind, art.get("payload"))
    return {"artifacts": artifacts, **synced}


def reflect_node(state: dict) -> dict[str, Any]:
    """Apply Reflexion rules; set _reflect_decision for routing."""
    plan = AgentPlan.model_validate(state["agent_plan"])
    step = _step_by_id(plan, state.get("plan_cursor"))
    log = list(state.get("reflection_log") or [])
    if step is None:
        plan.status = "completed"
        return {
            "agent_plan": plan.model_dump(),
            "plan_cursor": None,
            "_reflect_decision": "pass",
            "reflection_log": log,
        }

    artifacts = state.get("artifacts") or {}
    art = artifacts.get(step.output_ref or "") or {}
    reflect_counts = dict(state.get("reflect_counts") or {})
    reflection_count = int(reflect_counts.get(step.step_id, 0))

    decision = decide_reflection(
        plan,
        step,
        reflection_count=reflection_count,
        replan_count=plan.replan_count,
        human_gates=state.get("human_gates"),
        artifact_confirmed_by=art.get("confirmed_by"),
    )

    if decision == "repair":
        reflect_counts[step.step_id] = reflection_count + 1
        step.status = "pending"
        log.append({"step_id": step.step_id, "decision": "repair"})
        return {
            "agent_plan": plan.model_dump(),
            "reflect_counts": reflect_counts,
            "reflection_log": log[-20:],
            "_reflect_decision": "repair",
        }

    if decision == "replan":
        # Ensure confirming steps stay pending until confirmed
        if step.requires_confirm and art.get("confirmed_by") != "user":
            step.status = "pending"
        log.append({"step_id": step.step_id, "decision": "replan"})
        return {
            "agent_plan": plan.model_dump(),
            "reflection_log": log[-20:],
            "_reflect_decision": "replan",
        }

    log.append({"step_id": step.step_id, "decision": "pass"})
    updates: dict[str, Any] = {
        "reflection_log": log[-20:],
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
        if _pending_step(plan) is None or plan.status == "completed":
            return "end"
        return "dispatch"
    return "dispatch"
