"""Chat tool: start_case_generation — create task from dialog and run pipeline."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..domain import RequirementRef
from ..graph.constants import STAGE_INTAKE
from ..store.models import TaskDAO, TaskRow

_ACTIVE = ("running", "waiting_confirm", "waiting_input", "cancelling")


class StartCaseArgs(BaseModel):
    requirement_md: str | None = Field(
        default=None,
        description="完整需求 Markdown 正文。与 path 二选一，优先使用本字段。",
    )
    path: str | None = Field(
        default=None,
        description="工作区相对路径（如 uploads/req.md）；仅当未提供 requirement_md 时读取。",
    )


@dataclass
class StartCaseDeps:
    """Injected dependencies for start_case_generation (chat turn scoped)."""

    db: Any
    file_store: Any
    conversation_id: str
    workspace_id: str
    workspace_root: Path
    runner: Any | None
    outcome: dict[str, Any]


def make_start_case_tool(deps: StartCaseDeps) -> StructuredTool:
    async def _run(
        requirement_md: str | None = None,
        path: str | None = None,
    ) -> str:
        try:
            md = await _resolve_md(deps, requirement_md=requirement_md, path=path)
            task_id = await _create_and_run(deps, md)
            deps.outcome.clear()
            deps.outcome.update({"started_task_id": task_id, "ok": True})
            return json.dumps(
                {
                    "ok": True,
                    "task_id": task_id,
                    "message": "已创建任务并启动四阶段生成，请等待检查点确认。",
                },
                ensure_ascii=False,
            )
        except StartCaseError as exc:
            deps.outcome.clear()
            deps.outcome.update({"ok": False, "error": str(exc)})
            return json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
            )

    return StructuredTool.from_function(
        coroutine=_run,
        name="start_case_generation",
        description=(
            "在用户已通过对话提供足够需求信息且明确要求生成用例时调用。"
            "创建长程用例设计任务并立即启动流水线。"
            "传入 requirement_md（完整 Markdown）或工作区相对 path。"
            "同会话已有进行中的任务时会失败，勿重复调用。"
        ),
        args_schema=StartCaseArgs,
    )


class StartCaseError(Exception):
    """User-facing failure for start_case_generation."""


async def _resolve_md(
    deps: StartCaseDeps,
    *,
    requirement_md: str | None,
    path: str | None,
) -> str:
    text = (requirement_md or "").strip()
    if text:
        return text
    rel = (path or "").strip().replace("\\", "/")
    if not rel:
        raise StartCaseError("请提供 requirement_md 或 path（工作区相对路径）")
    if rel.startswith("/") or ".." in rel.split("/"):
        raise StartCaseError(f"非法路径：{path}")
    target = (deps.workspace_root / rel).resolve()
    try:
        target.relative_to(deps.workspace_root.resolve())
    except ValueError as exc:
        raise StartCaseError(f"路径越界：{path}") from exc
    if not target.is_file():
        raise StartCaseError(f"文件不存在：{rel}")
    content = target.read_text(encoding="utf-8").strip()
    if not content:
        raise StartCaseError(f"文件为空：{rel}")
    return content


async def _create_and_run(deps: StartCaseDeps, md: str) -> str:
    dao = TaskDAO(deps.db)
    page = await dao.list_by_conversation(
        deps.conversation_id, cursor=None, limit=50
    )
    active = [t for t in page.items if t.status in _ACTIVE]
    if active:
        ids = ", ".join(t.id[:8] for t in active[:3])
        raise StartCaseError(
            f"本会话已有进行中的任务（{ids}…），请先完成或取消后再启动"
        )
    if deps.runner is None:
        raise StartCaseError(
            "任务执行组件未就绪（模型未配置），请先在设置中配置模型并重启"
        )

    task_id = uuid.uuid4().hex
    ref = await deps.file_store.save_requirement(
        deps.workspace_id, task_id, md
    )
    task = TaskRow.create(
        id=task_id,
        conversation_id=deps.conversation_id,
        workspace_id=deps.workspace_id,
        status="waiting_input",
        current_stage=STAGE_INTAKE,
        langgraph_thread_id=f"th-{task_id}",
        graph_run_id=uuid.uuid4().hex,
        requirement=RequirementRef(
            path=ref.path, content_hash=ref.content_hash, clause_count=0
        ),
        snapshot_level="meta",
    )
    await dao.create(task)
    await deps.runner.start(task_id, event="run")
    return task_id
