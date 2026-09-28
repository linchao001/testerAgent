"""Orchestration tools (main agent only): spawn_subtask, await_human_confirm."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool


def make_orchestration_tools(*, spawn_fn=None, await_fn=None) -> list:
    """Build orchestration tools; default implementations are placeholders."""

    def _spawn(kind: str, goal: str, input_refs: list[str] | None = None) -> str:
        if spawn_fn is not None:
            return str(spawn_fn(kind=kind, goal=goal, input_refs=input_refs or []))
        return f"spawned:{kind}"

    def _await(gate_kind: str, artifact_id: str) -> str:
        if await_fn is not None:
            return str(await_fn(gate_kind=gate_kind, artifact_id=artifact_id))
        return f"await:{gate_kind}:{artifact_id}"

    return [
        StructuredTool.from_function(
            name="spawn_subtask",
            description="Spawn a serial nested subtask (review etc). Main agent only.",
            func=_spawn,
        ),
        StructuredTool.from_function(
            name="await_human_confirm",
            description="Request human confirmation for an artifact gate.",
            func=_await,
        ),
    ]
