"""Optional pre-node tool gather for case_designer pipeline stages."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from langchain_core.messages import HumanMessage

from ..adapters.llm import get_chat_model
from ..graph.tool_agent import run_tool_agent
from ..tools.registry import ToolBuildContext, build_case_designer_tools

EventSink = Callable[[str, dict], Awaitable[None]]

_GATHER_SYSTEM = (
    "你在用例设计产线节点前置搜集上下文。可使用 bash 与 str_replace_editor "
    "查看工作区文件。最后用简洁中文总结与当前阶段相关的发现；不要编造路径。"
)


def append_tool_notes(user_content: str, notes: str) -> str:
    text = (notes or "").strip()
    if not text:
        return user_content
    return f"{user_content}\n\n## Tool gather notes\n{text}"


async def maybe_gather_with_tools(
    *,
    stage: str,
    agent_config: dict,
    task_id: str,
    workspace_id: str,
    workspace_root: Path,
    runtime_config: dict,
    model_config: dict,
    user_goal: str,
    cancel_event: asyncio.Event | None = None,
    event_sink: EventSink | None = None,
) -> str:
    """Return '' if stage not in enable_tools_stages; else tool_agent final_text."""
    enabled = agent_config.get("enable_tools_stages") or []
    if stage not in enabled:
        return ""

    workspace_root.mkdir(parents=True, exist_ok=True)
    tools = build_case_designer_tools(
        ToolBuildContext(
            owner_id=f"task:{task_id}",
            workspace_root=workspace_root,
            runtime_config=runtime_config or {},
            cancel_event=cancel_event,
        )
    )
    model = get_chat_model(
        model_config=model_config or {}, runtime_config=runtime_config or {}
    )
    max_steps = int((runtime_config or {}).get("tool_agent_max_steps", 12))
    goal = (user_goal or "").strip() or f"为阶段 {stage} 搜集工作区上下文"
    result = await run_tool_agent(
        history=[HumanMessage(content=goal)],
        system_prompt=_GATHER_SYSTEM,
        tools=tools,
        model=model,
        max_steps=max_steps,
    )
    if event_sink is not None:
        await event_sink(
            "tool_call",
            {
                "stage": stage,
                "workspace_id": workspace_id,
                "tool_trace": [dict(t) for t in result.tool_trace],
            },
        )
    return result.final_text or ""


async def gather_notes_for_ctx(ctx, stage: str, user_goal: str) -> str:
    """TaskContext convenience: load config and sandbox root from FileStore."""
    cfg = await ctx.app.config.get()
    root = ctx.files.root / "workspaces" / ctx.task.workspace_id
    sink = ctx.emit if callable(getattr(ctx, "emit", None)) else None
    return await maybe_gather_with_tools(
        stage=stage,
        agent_config=ctx.agent_config or {},
        task_id=ctx.task.id,
        workspace_id=ctx.task.workspace_id,
        workspace_root=root,
        runtime_config=cfg.runtime_dict(),
        model_config=cfg.model_dict(),
        user_goal=user_goal,
        cancel_event=ctx.cancel_event,
        event_sink=sink,
    )
