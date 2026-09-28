"""Map PlanStep.kind → human_gate config key."""

from __future__ import annotations

from ...domain import PlanStepKind

_KIND_TO_GATE: dict[PlanStepKind, str] = {
    PlanStepKind.COVERAGE_DESIGN: "link",
    PlanStepKind.POINT_DESIGN: "point",
    PlanStepKind.REVIEW_COVERAGE: "review",
    PlanStepKind.REVIEW_QUALITY: "review",
    PlanStepKind.REVIEW_ADOPTION: "review",
}


def gate_key_for_step(kind: PlanStepKind) -> str | None:
    """Return human_gates dict key for this step kind, or None if no gate."""
    return _KIND_TO_GATE.get(kind)


def gate_enabled(human_gates: dict | None, kind: PlanStepKind) -> bool:
    key = gate_key_for_step(kind)
    if key is None:
        return False
    gates = human_gates or {}
    return bool(gates.get(key, True))
