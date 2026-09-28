"""Apply HumanDecision to plan / artifacts / case review updates."""

from __future__ import annotations

import uuid
from typing import Any

from ...domain import (
    AgentPlan,
    HumanDecision,
    PlanStep,
    PlanStepKind,
    ReviewProposal,
    ReviewStatus,
)


_ACTION_TO_REVIEW: dict[str, str] = {
    "adopt": ReviewStatus.ADOPTED.value,
    "edit_adopt": ReviewStatus.EDITED_ADOPTED.value,
    "reject": ReviewStatus.REJECTED.value,
}


def apply_human_decision(
    decision: HumanDecision,
    *,
    plan: AgentPlan,
    artifacts: dict[str, Any],
    case_review_updates: list[tuple[str, str]],
) -> dict[str, Any]:
    """Mutate ``plan`` / ``artifacts`` / ``case_review_updates`` in place; return summary.

    Pure sync helper — DAO writes happen in API/Runner (Task 8).
    """
    art = artifacts.get(decision.artifact_id)
    if art is None:
        raise KeyError(f"artifact not found: {decision.artifact_id}")

    if decision.action == "reject_rerun":
        for step in plan.steps:
            if step.output_ref == decision.artifact_id:
                step.status = "pending"
                step.output_ref = None
                break
        art["confirmed_by"] = None
        return {"plan": plan, "artifacts": artifacts, "case_review_updates": case_review_updates}

    if decision.action == "modify" and decision.payload is not None:
        art["payload"] = decision.payload
        art["version"] = int(art.get("version") or 1) + 1

    art["confirmed_by"] = "user"
    payload = art.get("payload") or {}
    if decision.gate_kind == "plan_confirm":
        return {"plan": plan, "artifacts": artifacts, "case_review_updates": case_review_updates}

    # review_decision
    proposal = ReviewProposal.model_validate(payload)
    if proposal.scope == "adoption":
        for item in proposal.items:
            status = _ACTION_TO_REVIEW.get(item.action)
            if status:
                case_review_updates.append((item.target_id, status))
        return {"plan": plan, "artifacts": artifacts, "case_review_updates": case_review_updates}

    # coverage / quality → insert repair or case_generate steps
    for item in proposal.items:
        if item.action in ("repair", "add_case", "add_point"):
            kind = (
                PlanStepKind.CASE_GENERATE
                if item.action == "add_case"
                else PlanStepKind.REPAIR
            )
            plan.steps.append(
                PlanStep(
                    step_id=f"s-{uuid.uuid4().hex[:8]}",
                    kind=kind,
                    goal=f"{item.action}:{item.target_id} — {item.rationale}",
                    input_refs=[item.target_id],
                    status="pending",
                )
            )
    return {"plan": plan, "artifacts": artifacts, "case_review_updates": case_review_updates}
