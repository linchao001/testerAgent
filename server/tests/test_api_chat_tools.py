"""Chat messages run tool_agent_graph and persist tool_trace (Task 6)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.graph.tool_agent import ToolAgentResult
from tester_agent.main import create_app
from tester_agent.settings import Settings
from tester_agent.store.db import Database
from tester_agent.store.models import WorkspaceDAO, WorkspaceRow
from tester_agent.tools.trace import ToolTraceEntry

WS = "ws-chat-tools"
KB_CONFIG = {"mode": "sdk", "target": "/tmp/reme", "kb_id": "kb-1", "options": {}}


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


def _seed_workspace(db, ws_id=WS):
    async def _go():
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=ws_id, name="chat-tools", kb_config=dict(KB_CONFIG))
        )

    _run(_go())


def test_chat_persists_assistant_with_tool_trace(env, monkeypatch):
    client, db, _ = env
    _seed_workspace(db)
    cid = client.post(
        "/api/v1/conversations", json={"workspace_id": WS, "title": "t"}
    ).json()["id"]

    fake = ToolAgentResult(
        final_text="已查看文件。",
        tool_trace=[
            ToolTraceEntry(
                tool="str_replace_editor",
                ok=True,
                latency_ms=12,
                args_digest="abcd1234abcd1234",
            )
        ],
    )
    monkeypatch.setattr(
        "tester_agent.runtime.chat_agent.get_chat_model",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "tester_agent.runtime.chat_agent.run_tool_agent",
        AsyncMock(return_value=fake),
    )

    resp = client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"content": "看看工作区", "kind": "chat"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["user"]["role"] == "user"
    assert body["user"]["content"] == "看看工作区"
    assert body["assistant"] is not None
    assert body["assistant"]["role"] == "assistant"
    assert body["assistant"]["content"] == "已查看文件。"
    assert body["assistant"]["payload"]["tool_trace"] == [
        {
            "tool": "str_replace_editor",
            "ok": True,
            "latency_ms": 12,
            "args_digest": "abcd1234abcd1234",
        }
    ]

    detail = client.get(f"/api/v1/conversations/{cid}").json()
    roles = [m["role"] for m in detail["messages"]["items"]]
    assert roles == ["assistant", "user"]


def test_change_request_skips_tool_loop(env, monkeypatch):
    client, db, _ = env
    _seed_workspace(db)
    cid = client.post(
        "/api/v1/conversations", json={"workspace_id": WS}
    ).json()["id"]
    spy = AsyncMock()
    monkeypatch.setattr("tester_agent.runtime.chat_agent.run_tool_agent", spy)

    resp = client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={
            "content": "改摘要",
            "kind": "change_request",
            "context": {"stage": "intake"},
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["user"]["kind"] == "change_request"
    assert body["user"]["payload"] == {"stage": "intake"}
    assert body["assistant"] is None
    spy.assert_not_called()
