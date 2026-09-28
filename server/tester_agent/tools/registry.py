"""Build the case_designer builtin tool list."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool

from .bash_persistent import PersistentBashManager, make_bash_tool
from .sandbox import WorkspaceSandbox
from .shell_backend import detect_shell_backend
from .str_replace_editor import make_str_replace_editor_tool

# Process-wide shell manager (owner-keyed sessions).
_BASH_MANAGER: PersistentBashManager | None = None


def get_bash_manager(runtime_config: dict | None = None) -> PersistentBashManager:
    global _BASH_MANAGER
    if _BASH_MANAGER is None:
        pref = (runtime_config or {}).get("tool_shell_backend", "auto")
        _BASH_MANAGER = PersistentBashManager(backend=detect_shell_backend(str(pref)))
    return _BASH_MANAGER


@dataclass
class ToolBuildContext:
    owner_id: str
    workspace_root: Path
    runtime_config: dict
    cancel_event: asyncio.Event | None = None
    bash_manager: PersistentBashManager | None = None
    memory_manager: object | None = None


def build_case_designer_tools(
    ctx: ToolBuildContext,
    *,
    include_capabilities: bool = False,
    include_orchestration: bool = False,
) -> list[BaseTool]:
    sandbox = WorkspaceSandbox(ctx.workspace_root)
    rc = ctx.runtime_config or {}
    max_chars = int(rc.get("tool_max_output_chars", 16000))
    timeout_ms = int(rc.get("tool_bash_timeout_ms", 300000))
    manager = ctx.bash_manager or get_bash_manager(rc)
    tools: list[BaseTool] = [
        make_bash_tool(
            manager,
            owner_id=ctx.owner_id,
            sandbox=sandbox,
            timeout_ms=timeout_ms,
            max_output_chars=max_chars,
            cancel=ctx.cancel_event,
        ),
        make_str_replace_editor_tool(sandbox, max_output_chars=max_chars),
    ]
    mem = ctx.memory_manager
    if mem is not None and getattr(mem, "memory_search_enabled", lambda: False)():
        from ..memory.tools import make_memory_search_tool

        tools.append(make_memory_search_tool(mem))
    if include_capabilities:
        from .capabilities import make_capability_tools

        tools.extend(make_capability_tools())
    if include_orchestration:
        from .orchestration import make_orchestration_tools

        tools.extend(make_orchestration_tools())
    return tools
