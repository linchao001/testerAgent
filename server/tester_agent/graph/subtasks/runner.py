"""Serial in-process subtask runner."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from tester_agent.domain import SubtaskResult
from tester_agent.store.models import SubtaskDAO, SubtaskRow


class SubtaskBusyError(RuntimeError):
    """Raised when a task already has a running subtask."""


@dataclass
class SubtaskRunContext:
    """Minimal deps for subtask runner (avoids full TaskContext in unit tests)."""

    task_id: str
    subtask_dao: SubtaskDAO
    timeout_sec: float = 600.0
    cancel_event: asyncio.Event | None = None
    invoke_subgraph: Callable[[str, dict], Awaitable[dict]] | None = None


async def run_subtask(
    *,
    ctx: SubtaskRunContext,
    parent_thread_id: str,
    kind: str,
    goal: str,
    input_refs: list[str] | None = None,
) -> SubtaskResult:
    """Start a serial nested subtask; persist row; invoke subgraph; return result."""
    running = await ctx.subtask_dao.get_running(ctx.task_id)
    if running is not None:
        raise SubtaskBusyError(
            f"task {ctx.task_id} already has running subtask {running.id}"
        )

    subtask_id = f"sub-{uuid.uuid4().hex[:12]}"
    thread_id = f"{parent_thread_id}::sub::{subtask_id}"
    row = SubtaskRow(
        id=subtask_id,
        task_id=ctx.task_id,
        thread_id=thread_id,
        kind=kind,
        status="running",
    )
    await ctx.subtask_dao.create(row)

    try:
        if ctx.cancel_event is not None and ctx.cancel_event.is_set():
            await ctx.subtask_dao.update_status(subtask_id, status="cancelled")
            return SubtaskResult(
                subtask_id=subtask_id,
                kind=kind,
                thread_id=thread_id,
                status="cancelled",
                summary="cancelled before start",
            )

        async def _invoke() -> dict:
            if ctx.invoke_subgraph is None:
                return {
                    "summary": f"stub:{kind}:{goal}",
                    "output_ref": f"art-{subtask_id}",
                }
            return await ctx.invoke_subgraph(
                kind,
                {
                    "goal": goal,
                    "input_refs": list(input_refs or []),
                    "thread_id": thread_id,
                    "subtask_id": subtask_id,
                },
            )

        out = await asyncio.wait_for(_invoke(), timeout=ctx.timeout_sec)
        output_ref = out.get("output_ref")
        summary = str(out.get("summary") or "")
        await ctx.subtask_dao.update_status(
            subtask_id, status="done", result_artifact_id=output_ref
        )
        return SubtaskResult(
            subtask_id=subtask_id,
            kind=kind,
            thread_id=thread_id,
            status="done",
            summary=summary,
            output_ref=output_ref,
        )
    except asyncio.TimeoutError:
        await ctx.subtask_dao.update_status(subtask_id, status="failed")
        return SubtaskResult(
            subtask_id=subtask_id,
            kind=kind,
            thread_id=thread_id,
            status="failed",
            summary="subtask timeout",
        )
    except Exception as exc:  # noqa: BLE001 — surface as failed result
        await ctx.subtask_dao.update_status(subtask_id, status="failed")
        return SubtaskResult(
            subtask_id=subtask_id,
            kind=kind,
            thread_id=thread_id,
            status="failed",
            summary=f"subtask error: {exc}",
        )
