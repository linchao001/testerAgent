"""Capability dispatch must call real stage nodes when TaskContext is present."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.runnables import RunnableConfig

from tester_agent.domain import AgentPlan, PlanStep, PlanStepKind
from tester_agent.tools.capabilities import (
    CapabilityOutcome,
    invoke_capability,
    legacy_state_from_artifact,
)


@pytest.mark.asyncio
async def test_invoke_capability_calls_intake_node():
    calls: list[tuple] = []

    async def fake_intake(ctx, state):
        calls.append((ctx, state))
        return {"clauses": [{"clause_id": "h1-1", "title_path": ["A"]}]}

    ctx = SimpleNamespace(task=SimpleNamespace(id="t1"), daos=None)
    out = await invoke_capability(
        PlanStepKind.INTAKE_PARSE,
        ctx=ctx,
        state={"task_id": "t1"},
        nodes={PlanStepKind.INTAKE_PARSE: fake_intake},
    )
    assert len(calls) == 1
    assert out.artifact_kind == "clauses"
    assert out.increment["clauses"][0]["clause_id"] == "h1-1"
    assert out.artifact_id


@pytest.mark.asyncio
async def test_invoke_capability_maps_coverage_design_to_link_identify():
    async def fake_link(ctx, state):
        assert state.get("clauses")
        return {
            "link_plan": {"links": [], "stories": [{"story_id": "s1"}]},
            "current_stage_version": {"link_identify": 1},
        }

    ctx = SimpleNamespace(
        task=SimpleNamespace(id="t1"),
        daos=SimpleNamespace(
            artifact=SimpleNamespace(
                get_active=AsyncMock(
                    return_value=SimpleNamespace(
                        id="art-link-1",
                        stage_version=1,
                        payload_dict=lambda: {
                            "links": [],
                            "stories": [{"story_id": "s1"}],
                        },
                    )
                )
            )
        ),
    )
    out = await invoke_capability(
        PlanStepKind.COVERAGE_DESIGN,
        ctx=ctx,
        state={"clauses": [{"clause_id": "h1-1"}]},
        nodes={PlanStepKind.COVERAGE_DESIGN: fake_link},
    )
    assert out.artifact_id == "art-link-1"
    assert out.artifact_kind == "coverage_design"
    assert out.increment["link_plan"]["stories"][0]["story_id"] == "s1"


@pytest.mark.asyncio
async def test_invoke_capability_without_ctx_returns_stub():
    out = await invoke_capability(PlanStepKind.INTAKE_PARSE, ctx=None, state={})
    assert out.artifact_id
    assert out.increment == {}


@pytest.mark.asyncio
async def test_execute_step_merges_node_increment_and_artifact():
    from tester_agent.graph.control.nodes import execute_step_node

    plan = AgentPlan(
        plan_id="p1",
        version=1,
        goal="g",
        steps=[
            PlanStep(
                step_id="s1",
                kind=PlanStepKind.INTAKE_PARSE,
                goal="parse",
                status="pending",
            )
        ],
        status="active",
    )
    state = {
        "agent_plan": plan.model_dump(),
        "plan_cursor": "s1",
        "artifacts": {},
    }
    ctx = SimpleNamespace(task=SimpleNamespace(id="t1"), daos=None)
    config: RunnableConfig = {"configurable": {"ctx": ctx}}

    async def fake_invoke(kind, *, ctx, state, nodes=None):
        assert kind == PlanStepKind.INTAKE_PARSE
        return CapabilityOutcome(
            artifact_id="clauses-abc",
            artifact_kind="clauses",
            increment={"clauses": [{"clause_id": "c1"}]},
            payload=[{"clause_id": "c1"}],
            version=1,
        )

    with patch(
        "tester_agent.graph.control.nodes.invoke_capability",
        side_effect=fake_invoke,
    ):
        result = await execute_step_node(state, config)

    assert result["clauses"] == [{"clause_id": "c1"}]
    assert result["artifacts"]["clauses-abc"]["kind"] == "clauses"
    assert result["agent_plan"]["steps"][0]["status"] == "done"
    assert result["agent_plan"]["steps"][0]["output_ref"] == "clauses-abc"


def test_legacy_state_from_artifact_maps_kinds():
    assert legacy_state_from_artifact(
        PlanStepKind.COVERAGE_DESIGN,
        {"links": [], "stories": [{"story_id": "sx"}]},
    ) == {"link_plan": {"links": [], "stories": [{"story_id": "sx"}]}}
    assert legacy_state_from_artifact(
        PlanStepKind.POINT_DESIGN,
        {"points": [{"point_id": "p1"}]},
    ) == {"point_plan": {"points": [{"point_id": "p1"}]}}
    assert legacy_state_from_artifact(PlanStepKind.INTAKE_PARSE, [{"clause_id": "c"}]) == {
        "clauses": [{"clause_id": "c"}]
    }


@pytest.mark.asyncio
async def test_await_human_syncs_link_plan_on_resume_confirm():
    from tester_agent.graph.control.nodes import await_human_node

    plan = AgentPlan(
        plan_id="p1",
        version=1,
        goal="g",
        steps=[
            PlanStep(
                step_id="s2",
                kind=PlanStepKind.COVERAGE_DESIGN,
                goal="links",
                status="done",
                requires_confirm=True,
                output_ref="art1",
            )
        ],
        status="active",
    )
    state = {
        "agent_plan": plan.model_dump(),
        "plan_cursor": "s2",
        "human_gates": {"link": True, "point": True, "review": True},
        "artifacts": {
            "art1": {
                "kind": "coverage_design",
                "version": 1,
                "confirmed_by": None,
                "payload": {"links": [], "stories": [{"story_id": "sx"}]},
            }
        },
    }
    with patch(
        "tester_agent.graph.control.nodes.interrupt",
        return_value={"action": "confirm"},
    ):
        result = await await_human_node(state)

    assert result["artifacts"]["art1"]["confirmed_by"] == "user"
    assert result["link_plan"]["stories"][0]["story_id"] == "sx"
