"""Plan-Execute scenarios: gates off, reject_rerun, cancel during subtask."""

from __future__ import annotations

import asyncio

import pytest

from tester_agent.domain import (
    AgentPlan,
    HumanDecision,
    PlanStep,
    PlanStepKind,
)
from tester_agent.graph.control.apply_decision import apply_human_decision
from tester_agent.graph.control.graph import build_control_graph
from tester_agent.graph.subtasks.runner import (
    SubtaskBusyError,
    SubtaskRunContext,
    run_subtask,
)
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import SubtaskDAO, SubtaskRow


@pytest.mark.asyncio
async def test_gates_off_stub_run_completes():
    g = build_control_graph()
    out = await g.ainvoke(
        {
            "task_id": "t-scen",
            "graph_run_id": "r1",
            "workspace_id": "w1",
            "human_gates": {"link": False, "point": False, "review": False},
        },
        {"recursion_limit": 64},
    )
    assert out["agent_plan"]["status"] == "completed"


def test_reject_rerun_requeues_review_step():
    plan = AgentPlan(
        plan_id="p",
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
    apply_human_decision(
        HumanDecision(
            gate_kind="review_decision",
            action="reject_rerun",
            artifact_id="prop-1",
        ),
        plan=plan,
        artifacts=artifacts,
        case_review_updates=[],
    )
    assert plan.steps[0].status == "pending"
    assert plan.steps[0].output_ref is None


@pytest.mark.asyncio
async def test_cancel_flag_before_subtask(tmp_path):
    db_path = tmp_path / "app.db"
    run_migrations(db_path)
    dao = SubtaskDAO(Database(db_path))
    cancel = asyncio.Event()
    cancel.set()
    ctx = SubtaskRunContext(
        task_id="t1",
        subtask_dao=dao,
        cancel_event=cancel,
    )
    result = await run_subtask(
        ctx=ctx,
        parent_thread_id="thr",
        kind="review_coverage",
        goal="g",
    )
    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_busy_subtask_blocks_second(tmp_path):
    db_path = tmp_path / "app.db"
    run_migrations(db_path)
    dao = SubtaskDAO(Database(db_path))
    await dao.create(
        SubtaskRow(
            id="sub-1",
            task_id="t1",
            thread_id="thr::sub::sub-1",
            kind="review_coverage",
            status="running",
        )
    )
    ctx = SubtaskRunContext(task_id="t1", subtask_dao=dao)
    with pytest.raises(SubtaskBusyError):
        await run_subtask(
            ctx=ctx, parent_thread_id="thr", kind="review_quality", goal="x"
        )
