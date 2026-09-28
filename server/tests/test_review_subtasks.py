"""Review subtasks and human decision application."""

from __future__ import annotations

from tester_agent.domain import (
    AgentPlan,
    HumanDecision,
    PlanStep,
    PlanStepKind,
    ReviewProposal,
    ReviewStatus,
)
from tester_agent.graph.control.apply_decision import apply_human_decision
from tester_agent.graph.subtasks.review_adoption import build_adoption_proposal
from tester_agent.graph.subtasks.review_coverage import build_coverage_proposal
from tester_agent.graph.subtasks.review_quality import build_quality_proposal


def test_coverage_review_emits_proposal():
    proposal = build_coverage_proposal(
        uncovered_clause_ids=["c1", "c2"],
        matrix_ref="art-matrix-1",
    )
    assert isinstance(proposal, ReviewProposal)
    assert proposal.scope == "coverage"
    assert proposal.matrix_ref == "art-matrix-1"
    assert {i.target_id for i in proposal.items} == {"c1", "c2"}
    assert all(i.action == "add_case" for i in proposal.items)


def test_quality_review_emits_repair_items():
    proposal = build_quality_proposal(
        case_issues=[
            {"case_id": "case-1", "issue": "weak assertion"},
            {"case_id": "case-2", "issue": "redundant"},
        ]
    )
    assert proposal.scope == "quality"
    assert len(proposal.items) == 2
    assert proposal.items[0].action == "repair"


def test_adoption_proposal_defaults_pending_to_adopt():
    proposal = build_adoption_proposal(
        cases=[
            {"id": "a", "title": "A", "review_status": "pending"},
            {"id": "b", "title": "B", "review_status": "pending"},
        ]
    )
    assert proposal.scope == "adoption"
    assert all(i.action == "adopt" for i in proposal.items)


def test_apply_review_decision_inserts_repair_steps():
    plan = AgentPlan(
        plan_id="p1",
        version=1,
        goal="g",
        steps=[
            PlanStep(
                step_id="s-rev",
                kind=PlanStepKind.REVIEW_QUALITY,
                goal="q",
                status="done",
                output_ref="prop-1",
                requires_confirm=True,
            )
        ],
    )
    proposal = ReviewProposal(
        scope="quality",
        items=[
            {
                "target_id": "case-1",
                "action": "repair",
                "rationale": "weak",
            }
        ],
    )
    artifacts = {
        "prop-1": {
            "kind": "review_proposal",
            "confirmed_by": None,
            "payload": proposal.model_dump(),
        }
    }
    decision = HumanDecision(
        gate_kind="review_decision",
        action="confirm",
        artifact_id="prop-1",
    )
    out = apply_human_decision(
        decision,
        plan=plan,
        artifacts=artifacts,
        case_review_updates=[],
    )
    assert artifacts["prop-1"]["confirmed_by"] == "user"
    kinds = [s.kind for s in out["plan"].steps]
    assert PlanStepKind.REPAIR in kinds


def test_apply_adoption_updates_collects_review_status():
    plan = AgentPlan(
        plan_id="p1",
        version=1,
        goal="g",
        steps=[
            PlanStep(
                step_id="s-ad",
                kind=PlanStepKind.REVIEW_ADOPTION,
                goal="a",
                status="done",
                output_ref="prop-ad",
                requires_confirm=True,
            )
        ],
    )
    proposal = build_adoption_proposal(
        cases=[{"id": "case-1", "title": "T", "review_status": "pending"}]
    )
    artifacts = {
        "prop-ad": {
            "kind": "review_proposal",
            "confirmed_by": None,
            "payload": proposal.model_dump(),
        }
    }
    updates: list[tuple[str, str]] = []
    decision = HumanDecision(
        gate_kind="review_decision",
        action="confirm",
        artifact_id="prop-ad",
    )
    apply_human_decision(
        decision,
        plan=plan,
        artifacts=artifacts,
        case_review_updates=updates,
    )
    assert updates == [("case-1", ReviewStatus.ADOPTED.value)]


def test_reject_rerun_requeues_review_step():
    plan = AgentPlan(
        plan_id="p1",
        version=1,
        goal="g",
        steps=[
            PlanStep(
                step_id="s-rev",
                kind=PlanStepKind.REVIEW_COVERAGE,
                goal="c",
                status="done",
                output_ref="prop-1",
                requires_confirm=True,
            )
        ],
    )
    artifacts = {
        "prop-1": {
            "kind": "review_proposal",
            "confirmed_by": None,
            "payload": {"scope": "coverage", "items": []},
        }
    }
    decision = HumanDecision(
        gate_kind="review_decision",
        action="reject_rerun",
        artifact_id="prop-1",
    )
    out = apply_human_decision(
        decision,
        plan=plan,
        artifacts=artifacts,
        case_review_updates=[],
    )
    assert out["plan"].steps[0].status == "pending"
    assert out["plan"].steps[0].output_ref is None
