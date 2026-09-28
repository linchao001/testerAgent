"""Reflexion decision rules for the control loop."""

from __future__ import annotations

from typing import Literal

from ...domain import AgentPlan, PlanStep
from .gates import gate_enabled

ReflectDecision = Literal["pass", "repair", "replan"]


def decide_reflection(
    plan: AgentPlan,
    step: PlanStep,
    *,
    reflection_count: int,
    replan_count: int,
    human_gates: dict | None,
    artifact_confirmed_by: str | None,
    replan_max: int = 3,
) -> ReflectDecision:
    """Decide pass | repair | replan after a step completes.

    Rules (spec §3 / Task 4):
    1. requires_confirm + matching gate on + not confirmed_by=user → replan
    2. step failed and reflection_count < max_reflect → repair
    3. step failed and reflect exhausted → replan (caller may fail plan if replan_max)
    4. else pass
    """
    del plan  # reserved for future global checks
    if (
        step.requires_confirm
        and gate_enabled(human_gates, step.kind)
        and artifact_confirmed_by != "user"
    ):
        return "replan"

    if step.status == "failed":
        if reflection_count < step.max_reflect:
            return "repair"
        if replan_count < replan_max:
            return "replan"
        return "replan"

    return "pass"
