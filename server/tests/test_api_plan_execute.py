"""Plan-Execute API: plan/subtasks/confirm gate_kind."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.api.tasks import ConfirmIn
from tester_agent.domain import AgentPlan, PlanStep, PlanStepKind
from tester_agent.main import create_app
from tester_agent.settings import Settings
from tester_agent.store.db import Database
from tester_agent.store.models import (
    ArtifactDAO,
    ArtifactRow,
    TaskDAO,
    TaskRow,
    WorkspaceDAO,
    WorkspaceRow,
)

WS = "ws-pe"
CONV = "conv-pe"
TASK = "task-pe"
KB = {"mode": "sdk", "target": "/tmp/reme", "kb_id": "kb-1", "options": {}}


def _settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "TESTER_AGENT_DATA_DIR": str(tmp_path / "data"),
            "TESTER_AGENT_SINGLE_WORKER": "1",
        }
    )


@pytest.fixture()
def env(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        db = Database(settings.app_db_path)
        yield client, db, settings
        db.close()


def _run(coro):
    return asyncio.run(coro)


def _seed_waiting_with_plan(db: Database):
    async def _go():
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=WS, name="ws", kb_config=dict(KB))
        )
        await db.aexecute(
            "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (CONV, WS, "c", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
        )
        await TaskDAO(db).create(
            TaskRow.create(
                id=TASK,
                conversation_id=CONV,
                workspace_id=WS,
                status="waiting_confirm",
                current_stage="coverage_design",
                langgraph_thread_id="th-pe",
                graph_run_id="run-pe",
            )
        )
        plan = AgentPlan(
            plan_id="plan-1",
            version=1,
            goal="g",
            steps=[
                PlanStep(
                    step_id="s2",
                    kind=PlanStepKind.COVERAGE_DESIGN,
                    goal="links",
                    requires_confirm=True,
                    status="done",
                    output_ref="art-plan-1",
                )
            ],
        )
        art = ArtifactRow.create(
            id="art-plan-1",
            task_id=TASK,
            stage="coverage_design",
            kind="agent_plan",
            graph_run_id="run-pe",
            stage_version=1,
            payload=plan.model_dump(),
        )
        await ArtifactDAO(db).put(art)
        await TaskDAO(db).set_current_plan_artifact(TASK, "art-plan-1")

    _run(_go())


def test_confirm_omitted_gate_kind_defaults_plan_confirm():
    """遗留客户端可省略 gate_kind；默认 plan_confirm。"""
    body = ConfirmIn.model_validate(
        {"action": "confirm", "artifact_id": "art-1"}
    )
    assert body.gate_kind == "plan_confirm"


def test_get_plan_returns_agent_plan(env):
    client, db, _ = env
    _seed_waiting_with_plan(db)
    r = client.get(f"/api/v1/tasks/{TASK}/plan")
    assert r.status_code == 200
    body = r.json()
    assert "steps" in body
    assert body["plan_id"] == "plan-1"


def test_get_subtasks_empty_list(env):
    client, db, _ = env
    _seed_waiting_with_plan(db)
    r = client.get(f"/api/v1/tasks/{TASK}/subtasks")
    assert r.status_code == 200
    assert r.json() == []


def test_get_review_proposal_404_when_missing(env):
    client, db, _ = env
    _seed_waiting_with_plan(db)
    r = client.get(f"/api/v1/tasks/{TASK}/review-proposals/nope")
    assert r.status_code == 404
