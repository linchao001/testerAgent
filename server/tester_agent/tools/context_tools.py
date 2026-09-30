"""会话侧 context_* 工具组（WP-33 / spec §14）。

解释权在 LLM，执行权在 ``context.intervention.execute``。
forget 要求 selector 精确命中单条（id: 或唯一命中）；多命中返回候选不执行。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..context.intervention import (
    ContextAction,
    ContextCommand,
    ConfirmRequiredError,
    ContextCommandError,
    execute,
    resolve_entries,
)
from ..errors import TaskStateConflict, ValidationError


class _SelectorArgs(BaseModel):
    selector: str = Field(description="id:|kind:|step:|recent:|item:|batch:|all")
    confirm: bool = Field(default=False, description="pinned forget 二次确认")
    reason: str | None = Field(default=None)


class _GoalArgs(BaseModel):
    text: str = Field(description="新目标文本")
    reason: str | None = Field(default=None)


class _BudgetArgs(BaseModel):
    profile: str = Field(default="chat")
    p0: int
    p1: int
    p2: int
    reason: str | None = Field(default=None)


class _ShowArgs(BaseModel):
    selector: str = Field(default="all")


def make_context_tools(store: Any) -> list[StructuredTool]:
    """构造 context_pin/unpin/forget/show/set_goal/budget 工具列表。"""

    async def _pin(selector: str, confirm: bool = False, reason: str | None = None) -> str:
        return await _run(
            store,
            ContextCommand(
                action=ContextAction.PIN,
                selector=selector,
                confirm=confirm,
                reason=reason,
            ),
        )

    async def _unpin(
        selector: str, confirm: bool = False, reason: str | None = None
    ) -> str:
        return await _run(
            store,
            ContextCommand(
                action=ContextAction.UNPIN,
                selector=selector,
                confirm=confirm,
                reason=reason,
            ),
        )

    async def _forget(
        selector: str, confirm: bool = False, reason: str | None = None
    ) -> str:
        # 模糊防护：非 id: 且命中 >1 → 返回候选，不执行
        try:
            hits = resolve_entries(store, selector)
        except ValidationError as exc:
            return f"Error: {exc.message}"
        if not selector.strip().startswith("id:") and len(hits) != 1:
            if not hits:
                return "Error: selector 命中 0 条"
            lines = ["候选（请改用 id: 精确选择）:"]
            for e in hits[:12]:
                lines.append(f"- id:{e.entry_id} kind:{e.entry_kind.value} {e.digest}")
            return "\n".join(lines)
        return await _run(
            store,
            ContextCommand(
                action=ContextAction.FORGET,
                selector=selector if selector.strip().startswith("id:") else f"id:{hits[0].entry_id}",
                confirm=confirm,
                reason=reason,
            ),
        )

    async def _show(selector: str = "all") -> str:
        return await _run(
            store,
            ContextCommand(action=ContextAction.SHOW, selector=selector),
        )

    async def _set_goal(text: str, reason: str | None = None) -> str:
        return await _run(
            store,
            ContextCommand(
                action=ContextAction.SET_GOAL, arg=text, reason=reason
            ),
        )

    async def _budget(
        profile: str,
        p0: int,
        p1: int,
        p2: int,
        reason: str | None = None,
    ) -> str:
        import json

        return await _run(
            store,
            ContextCommand(
                action=ContextAction.BUDGET,
                arg=json.dumps({"profile": profile, "p0": p0, "p1": p1, "p2": p2}),
                reason=reason,
            ),
        )

    return [
        StructuredTool.from_function(
            coroutine=_pin,
            name="context_pin",
            description="钉住上下文条目（selector=id:/kind:/...）",
            args_schema=_SelectorArgs,
        ),
        StructuredTool.from_function(
            coroutine=_unpin,
            name="context_unpin",
            description="解除钉住",
            args_schema=_SelectorArgs,
        ),
        StructuredTool.from_function(
            coroutine=_forget,
            name="context_forget",
            description="降级/淘汰条目；模糊指代返回候选列表",
            args_schema=_SelectorArgs,
        ),
        StructuredTool.from_function(
            coroutine=_show,
            name="context_show",
            description="查看上下文条目",
            args_schema=_ShowArgs,
        ),
        StructuredTool.from_function(
            coroutine=_set_goal,
            name="context_set_goal",
            description="设置当前任务目标",
            args_schema=_GoalArgs,
        ),
        StructuredTool.from_function(
            coroutine=_budget,
            name="context_budget",
            description="调整 profile 分区预算",
            args_schema=_BudgetArgs,
        ),
    ]


async def _run(store: Any, cmd: ContextCommand) -> str:
    try:
        result = await execute(store, cmd, operator="user")
    except ConfirmRequiredError as exc:
        return f"CONFIRM_REQUIRED: {exc.message}; candidates={exc.details.get('candidates')}"
    except (ContextCommandError, ValidationError, TaskStateConflict) as exc:
        return f"Error: {exc.message}"
    if cmd.action is ContextAction.SHOW:
        import json

        return json.dumps(
            {"affected": result.affected, "entries": result.entries},
            ensure_ascii=False,
        )
    return (
        f"ok action={result.action} affected={result.affected} "
        f"meta={result.affected_meta} {result.message}"
    )
