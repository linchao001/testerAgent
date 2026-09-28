"""Optional tool-agent gather before structured stage LLM calls."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from tester_agent.graph.tool_agent import ToolAgentResult
from tester_agent.graph.tool_gather import maybe_gather_with_tools


def test_empty_stages_returns_empty_without_model(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr("tester_agent.graph.tool_gather.run_tool_agent", spy)
    monkeypatch.setattr(
        "tester_agent.graph.tool_gather.get_chat_model",
        MagicMock(side_effect=AssertionError("should not build model")),
    )

    async def _run():
        return await maybe_gather_with_tools(
            stage="intake",
            agent_config={"enable_tools_stages": []},
            task_id="t1",
            workspace_id="ws1",
            workspace_root=Path("."),
            runtime_config={},
            model_config={},
            user_goal="look around",
        )

    assert asyncio.run(_run()) == ""
    spy.assert_not_called()


def test_enabled_stage_runs_tool_agent(tmp_path: Path, monkeypatch):
    fake = ToolAgentResult(final_text="found requirement.md", tool_trace=[])
    spy = AsyncMock(return_value=fake)
    monkeypatch.setattr("tester_agent.graph.tool_gather.run_tool_agent", spy)
    monkeypatch.setattr(
        "tester_agent.graph.tool_gather.get_chat_model",
        lambda **_k: object(),
    )
    monkeypatch.setattr(
        "tester_agent.graph.tool_gather.build_case_designer_tools",
        lambda _ctx: [],
    )
    events: list[tuple[str, dict]] = []

    async def sink(type_: str, payload: dict) -> None:
        events.append((type_, payload))

    async def _run():
        return await maybe_gather_with_tools(
            stage="intake",
            agent_config={"enable_tools_stages": ["intake", "case_generate"]},
            task_id="t1",
            workspace_id="ws1",
            workspace_root=tmp_path,
            runtime_config={"tool_agent_max_steps": 4},
            model_config={"base_url": "x", "api_key": "y", "model": "z"},
            user_goal="inspect workspace",
            event_sink=sink,
        )

    assert asyncio.run(_run()) == "found requirement.md"
    spy.assert_awaited_once()
    assert events and events[0][0] == "tool_call"
    assert events[0][1]["stage"] == "intake"
