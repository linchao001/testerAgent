"""Tests for serial subtask runner and orchestration tools."""

from pathlib import Path

import pytest

from tester_agent.graph.subtasks.runner import (
    SubtaskBusyError,
    SubtaskRunContext,
    run_subtask,
)
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import SubtaskDAO, SubtaskRow
from tester_agent.tools.registry import ToolBuildContext, build_case_designer_tools


@pytest.fixture
async def subtask_dao(tmp_path: Path):
    db_path = tmp_path / "app.db"
    run_migrations(db_path)
    db = Database(db_path)
    return SubtaskDAO(db)


@pytest.mark.asyncio
async def test_thread_id_suffix(subtask_dao):
    ctx = SubtaskRunContext(task_id="t1", subtask_dao=subtask_dao)
    result = await run_subtask(
        ctx=ctx,
        parent_thread_id="thr-1",
        kind="review_coverage",
        goal="g",
        input_refs=[],
    )
    assert result.thread_id.startswith("thr-1::sub::")
    assert result.status == "done"


@pytest.mark.asyncio
async def test_serial_subtask_rejects_second(subtask_dao):
    await subtask_dao.create(
        SubtaskRow(
            id="sub-busy",
            task_id="t1",
            thread_id="thr-1::sub::sub-busy",
            kind="review_coverage",
            status="running",
        )
    )
    ctx = SubtaskRunContext(task_id="t1", subtask_dao=subtask_dao)
    with pytest.raises(SubtaskBusyError):
        await run_subtask(
            ctx=ctx, parent_thread_id="thr-1", kind="review_quality", goal="b"
        )


def test_orchestration_included_when_flagged(tmp_path: Path):
    tools = build_case_designer_tools(
        ToolBuildContext(owner_id="t:1", workspace_root=tmp_path, runtime_config={}),
        include_orchestration=True,
    )
    names = {t.name for t in tools}
    assert "spawn_subtask" in names
    assert "await_human_confirm" in names
