"""工作区 root_dir：API 创建/更新、路径落盘、冲突与有任务禁改。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.main import create_app
from tester_agent.settings import Settings
from tester_agent.store.db import Database
from tester_agent.store.models import TaskDAO, TaskRow, WorkspaceDAO, WorkspaceRow
from tester_agent.store.paths import resolve_workspace_root

KB_CONFIG = {
    "kb_id": "kb-1",
    "knowledge_bases_dir": "",
    "knowledge_dir": "knowledge",
    "create_knowledge_base": False,
    "options": {},
}


def _settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "TESTER_AGENT_DATA_DIR": str(tmp_path / "data"),
            "TESTER_AGENT_SINGLE_WORKER": "1",
        }
    )


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        db = Database(settings.app_db_path)
        yield client, db, settings, tmp_path
        db.close()


def test_create_with_custom_root_dir_writes_files_there(env):
    client, db, settings, tmp_path = env
    custom = (tmp_path / "custom-ws").resolve()
    resp = client.post(
        "/api/v1/workspaces",
        json={
            "name": "自定义目录区",
            "description": "",
            "root_dir": str(custom),
            "kb_config": KB_CONFIG,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["root_dir"] == str(custom)
    assert custom.is_dir()

    # FileStore 应把工作区根注册到 custom
    store = client.app.state.file_store
    assert store.workspace_root(body["id"]) == custom


def test_create_default_root_dir_empty(env):
    client, db, settings, tmp_path = env
    resp = client.post(
        "/api/v1/workspaces",
        json={"name": "默认区", "kb_config": KB_CONFIG},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["root_dir"] == ""
    expected = resolve_workspace_root(settings.data_dir, body["id"], "")
    store = client.app.state.file_store
    assert store.workspace_root(body["id"]) == expected


def test_create_rejects_relative_root_dir(env):
    client, *_ = env
    resp = client.post(
        "/api/v1/workspaces",
        json={
            "name": "坏路径",
            "root_dir": "relative/ws",
            "kb_config": KB_CONFIG,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_BODY"


def test_create_rejects_duplicate_root_dir(env):
    client, _, _, tmp_path = env
    custom = (tmp_path / "shared").resolve()
    custom.mkdir()
    r1 = client.post(
        "/api/v1/workspaces",
        json={"name": "a", "root_dir": str(custom), "kb_config": KB_CONFIG},
    )
    assert r1.status_code == 201
    r2 = client.post(
        "/api/v1/workspaces",
        json={"name": "b", "root_dir": str(custom), "kb_config": KB_CONFIG},
    )
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "VALIDATION_BODY"


def test_update_root_dir_blocked_when_tasks_exist(env):
    client, db, settings, tmp_path = env
    r = client.post(
        "/api/v1/workspaces",
        json={"name": "有任务区", "kb_config": KB_CONFIG},
    )
    wid = r.json()["id"]

    async def seed():
        await db.aexecute(
            "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (
                "c-root-1",
                wid,
                "c",
                "2026-09-29T00:00:00.000Z",
                "2026-09-29T00:00:00.000Z",
            ),
        )
        await TaskDAO(db).create(
            TaskRow.create(
                id="t-root-1",
                conversation_id="c-root-1",
                workspace_id=wid,
                status="completed",
                current_stage="intake",
                langgraph_thread_id="th-root-1",
                graph_run_id="run-root-1",
            )
        )

    _run(seed())

    custom = (tmp_path / "move-me").resolve()
    resp = client.put(
        f"/api/v1/workspaces/{wid}",
        json={"root_dir": str(custom)},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TASK_STATE_CONFLICT"


def test_update_root_dir_ok_without_tasks(env):
    client, _, _, tmp_path = env
    r = client.post(
        "/api/v1/workspaces",
        json={"name": "可改区", "kb_config": KB_CONFIG},
    )
    wid = r.json()["id"]
    custom = (tmp_path / "new-root").resolve()
    resp = client.put(
        f"/api/v1/workspaces/{wid}",
        json={"root_dir": str(custom)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["root_dir"] == str(custom)
    assert custom.is_dir()
    assert client.app.state.file_store.workspace_root(wid) == custom


def test_list_includes_root_dir(env):
    client, *_ = env
    client.post(
        "/api/v1/workspaces",
        json={"name": "列表区", "kb_config": KB_CONFIG},
    )
    items = client.get("/api/v1/workspaces").json()["items"]
    assert all("root_dir" in w for w in items)
