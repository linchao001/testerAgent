"""Unit tests for start_case_generation tool."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.domain import RequirementRef
from tester_agent.settings import Settings
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ConversationDAO,
    ConversationRow,
    TaskDAO,
    TaskRow,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore
from tester_agent.tools.start_case import StartCaseDeps, make_start_case_tool

WS = "ws-start-case"
CONV = "conv-start-case"
KB = {
    "kb_id": "kb-1",
    "knowledge_bases_dir": "",
    "knowledge_dir": "knowledge",
    "create_knowledge_base": False,
    "options": {},
}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env(
        {
            "TESTER_AGENT_DATA_DIR": str(data),
            "TESTER_AGENT_SINGLE_WORKER": "1",
        }
    )
    run_migrations(settings.app_db_path)
    db = Database(settings.app_db_path)
    store = FileStore(settings.data_dir)
    root = settings.data_dir / "workspaces" / WS
    root.mkdir(parents=True, exist_ok=True)

    async def seed():
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=WS, name="ws", kb_config=dict(KB))
        )
        await ConversationDAO(db).create(
            ConversationRow.create(
                id=CONV, workspace_id=WS, title="t"
            )
        )

    _run(seed())
    yield db, store, root
    db.close()


def _deps(db, store, root, runner=None, outcome=None) -> StartCaseDeps:
    return StartCaseDeps(
        db=db,
        file_store=store,
        conversation_id=CONV,
        workspace_id=WS,
        workspace_root=root,
        runner=runner if runner is not None else AsyncMock(),
        outcome=outcome if outcome is not None else {},
    )


def test_start_case_with_requirement_md_creates_and_runs(env):
    db, store, root = env
    runner = AsyncMock()
    runner.start = AsyncMock(
        return_value=type("H", (), {"task_id": "x", "graph_run_id": "g"})()
    )
    outcome: dict = {}
    tool = make_start_case_tool(_deps(db, store, root, runner=runner, outcome=outcome))

    raw = _run(tool.ainvoke({"requirement_md": "## 登录\n用户可登录系统"}))
    body = json.loads(raw)
    assert body["ok"] is True
    assert body["task_id"]
    assert outcome["started_task_id"] == body["task_id"]
    runner.start.assert_awaited_once()
    task = _run(TaskDAO(db).get(body["task_id"]))
    assert task.conversation_id == CONV
    assert task.workspace_id == WS
    assert task.current_stage == "intake"


def test_start_case_from_workspace_path(env):
    db, store, root = env
    uploads = root / "uploads"
    uploads.mkdir(parents=True)
    (uploads / "req.md").write_text("## 需求\n从文件启动", encoding="utf-8")
    runner = AsyncMock()
    runner.start = AsyncMock(return_value=object())
    tool = make_start_case_tool(_deps(db, store, root, runner=runner))

    raw = _run(tool.ainvoke({"path": "uploads/req.md"}))
    body = json.loads(raw)
    assert body["ok"] is True
    runner.start.assert_awaited_once()


def test_start_case_rejects_when_active_task_exists(env):
    db, store, root = env

    async def seed_task():
        await TaskDAO(db).create(
            TaskRow.create(
                id="task-active",
                conversation_id=CONV,
                workspace_id=WS,
                status="running",
                current_stage="intake",
                langgraph_thread_id="th-1",
                graph_run_id="run-1",
                requirement=RequirementRef(
                    path="x", content_hash="h", clause_count=0
                ),
            )
        )

    _run(seed_task())
    tool = make_start_case_tool(_deps(db, store, root))
    raw = _run(tool.ainvoke({"requirement_md": "## 又一份"}))
    body = json.loads(raw)
    assert body["ok"] is False
    assert "进行中" in body["error"]


def test_start_case_requires_md_or_path(env):
    db, store, root = env
    tool = make_start_case_tool(_deps(db, store, root))
    raw = _run(tool.ainvoke({}))
    body = json.loads(raw)
    assert body["ok"] is False
    assert "requirement_md" in body["error"] or "path" in body["error"]
