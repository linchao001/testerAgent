"""WP-F2 最小后端增量：GET /api/v1/artifacts/{id} 返回完整 payload。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.graph.constants import STAGE_LINK_IDENTIFY
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

WS, CONV, TASK, ART = "ws-1", "conv-1", "task-1", "art-link-1"
KB_CONFIG = {"mode": "sdk", "target": "/tmp/reme", "kb_id": "kb-1", "options": {}}

LINK_PAYLOAD = {
    "links": [
        {
            "link_id": "L1",
            "title": "下单链路",
            "summary": "用户下单主流程",
            "hit": True,
            "entry_id": "e-link-1",
            "entry_version": "h-abc",
            "confidence": 0.9,
            "story_ids": ["S1"],
        }
    ],
    "stories": [
        {
            "story_id": "S1",
            "link_id": "L1",
            "title": "提交订单",
            "summary": "用户提交订单",
            "hit": True,
            "entry_id": "e-story-1",
            "entry_version": "h-def",
            "confidence": 0.85,
            "rationale": "需求条款 h2-1",
            "related_clause_ids": ["h2-1"],
        }
    ],
    "new_suggestions": [],
}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path):
    settings = Settings.from_env(
        {
            "TESTER_AGENT_DATA_DIR": str(tmp_path / "data"),
            "TESTER_AGENT_SINGLE_WORKER": "1",
        }
    )
    with TestClient(create_app(settings)) as client:
        db = Database(settings.app_db_path)
        yield client, db
        db.close()


async def _seed(db):
    await WorkspaceDAO(db).create(
        WorkspaceRow.create(id=WS, name="ws", kb_config=dict(KB_CONFIG))
    )
    await db.aexecute(
        "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (CONV, WS, "c", "2026-09-28T00:00:00.000Z", "2026-09-28T00:00:00.000Z"),
    )
    await TaskDAO(db).create(
        TaskRow.create(
            id=TASK,
            conversation_id=CONV,
            workspace_id=WS,
            status="waiting_confirm",
            current_stage=STAGE_LINK_IDENTIFY,
            langgraph_thread_id=f"th-{TASK}",
            graph_run_id=f"run-{TASK}",
        )
    )
    await ArtifactDAO(db).put(
        ArtifactRow.create(
            id=ART,
            task_id=TASK,
            stage=STAGE_LINK_IDENTIFY,
            graph_run_id=f"run-{TASK}",
            stage_version=1,
            payload=LINK_PAYLOAD,
        )
    )


class TestGetArtifact:
    def test_returns_payload_and_meta(self, env):
        client, db = env
        _run(_seed(db))
        resp = client.get(f"/api/v1/artifacts/{ART}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == ART
        assert body["task_id"] == TASK
        assert body["stage"] == STAGE_LINK_IDENTIFY
        assert body["stage_version"] == 1
        assert body["origin"] == "system"
        assert body["status"] == "active"
        assert body["confirmed_by"] is None
        assert body["payload"] == LINK_PAYLOAD
        assert "created_at" in body

    def test_missing_404(self, env):
        client, _db = env
        resp = client.get("/api/v1/artifacts/no-such")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"
