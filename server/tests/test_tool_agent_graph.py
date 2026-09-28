"""Fake chat model that issues one tool call then a final answer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tester_agent.graph.tool_agent import run_tool_agent
from tester_agent.tools.registry import ToolBuildContext, build_case_designer_tools


class SequencedFakeModel(BaseChatModel):
    def __init__(self) -> None:
        super().__init__()
        self._n = 0

    @property
    def _llm_type(self) -> str:
        return "sequenced-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> SequencedFakeModel:
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, **kwargs: Any) -> ChatResult:
        raise NotImplementedError

    async def _agenerate(
        self, messages: list[BaseMessage], stop: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._n += 1
        if self._n == 1:
            msg = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "str_replace_editor",
                        "args": {"command": "view", "path": "note.txt"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            msg = AIMessage(content="The note says hello.")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def test_tool_agent_one_tool_then_answer(tmp_path: Path):
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    tools = build_case_designer_tools(
        ToolBuildContext(owner_id="t1", workspace_root=tmp_path, runtime_config={})
    )
    model = SequencedFakeModel()

    async def _run():
        return await run_tool_agent(
            history=[HumanMessage(content="read note.txt")],
            system_prompt="You are a helpful assistant with tools.",
            tools=tools,
            model=model,
            max_steps=6,
        )

    result = asyncio.run(_run())
    assert "hello" in result.final_text.lower()
    assert any(t["tool"] == "str_replace_editor" for t in result.tool_trace)
