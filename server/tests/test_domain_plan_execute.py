"""Domain models for Plan-Execute + Reflexion paradigm."""

from tester_agent.domain import AgentPlan, MessageKind, PlanStep, PlanStepKind


def test_agent_plan_roundtrip():
    plan = AgentPlan(
        plan_id="p1",
        version=1,
        goal="cover login",
        steps=[
            PlanStep(
                step_id="s1",
                kind=PlanStepKind.COVERAGE_DESIGN,
                goal="links",
                requires_confirm=True,
            ),
            PlanStep(step_id="s2", kind=PlanStepKind.CASE_GENERATE, goal="cases"),
        ],
    )
    data = plan.model_dump()
    assert AgentPlan.model_validate(data).steps[0].kind == PlanStepKind.COVERAGE_DESIGN


def test_message_kind_extensions():
    assert MessageKind.PLAN_REVISION.value == "plan_revision"
    assert MessageKind.REVIEW_DECISION.value == "review_decision"
    assert MessageKind.GATE_CONFIRM.value == "gate_confirm"
