"""WP-31 Task 10：before_model_hook 与长 ToolMessage tombstone（spec §6.3）。

口径：
- 短 ToolMessage 原样进模型；长 ToolMessage 在 store 中 demote 为
  TOOL_RESULT tombstone，并以 SystemMessage 替换模型输入位；
- hook 只改模型入参：图 state/result.messages 保留原始 ToolMessage，
  tool_trace 不受影响；
- hook=None（默认）路径消息字节不变；
- 连续多次工具调用，每轮替换口径一致。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.context.models import EntryKind, EntryStatus
from tester_agent.context.store import ContextStore
from tester_agent.graph.context_hooks import make_tool_message_hook
from tester_agent.graph.tool_agent import run_tool_agent
from tester_agent.tools.registry import ToolBuildContext, build_case_designer_tools


class ScriptedModel(BaseChatModel):
    """按脚本产出 tool_calls / 最终答案，并记录每轮实际收到的消息。"""

    def __init__(self, script: list[dict]) -> None:
        super().__init__()
        self._script = script
        self._received: list[list[BaseMessage]] = []

    @property
    def received(self) -> list[list[BaseMessage]]:
        return self._received

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self

    def _generate(self, messages, stop=None, **kwargs):
        raise NotImplementedError

    async def _agenerate(self, messages, stop=None, **kwargs) -> ChatResult:
        self.received.append(list(messages))
        step = len(self.received) - 1
        spec = self._script[step]
        if "tool_calls" in spec:
            msg = AIMessage(content="", tool_calls=spec["tool_calls"])
        else:
            msg = AIMessage(content=spec["content"])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _tools(tmp_path):
    return build_case_designer_tools(
        ToolBuildContext(owner_id="t1", workspace_root=tmp_path, runtime_config={})
    )


def _store() -> ContextStore:
    return ContextStore(owner_type="task", owner_id="t1", workspace_id="ws1")


def _view_call(cid: str) -> dict:
    return {
        "name": "str_replace_editor",
        "args": {"command": "view", "path": "note.txt"},
        "id": cid,
        "type": "tool_call",
    }


def _run(coro):
    return asyncio.run(coro)


def test_long_tool_message_replaced_before_model(tmp_path):
    (tmp_path / "note.txt").write_text("长" * 3000, encoding="utf-8")
    store = _store()
    model = ScriptedModel(
        [
            {"tool_calls": [_view_call("call-1")]},
            {"content": "已读完。"},
        ]
    )

    result = _run(
        run_tool_agent(
            history=[HumanMessage(content="读文件")],
            system_prompt="sys",
            tools=_tools(tmp_path),
            model=model,
            max_steps=6,
            before_model_hook=make_tool_message_hook(store, long_tool_chars=2000),
        )
    )

    second_call = model.received[1]
    # 模型输入：无长 ToolMessage，有 tombstone SystemMessage
    assert not any(
        isinstance(m, ToolMessage) and len(m.content) > 2000 for m in second_call
    )
    tombstones = [
        m for m in second_call
        if isinstance(m, SystemMessage) and m.content.startswith("〔已归档 tool_result")
    ]
    assert len(tombstones) == 1

    entry = store.get("tool:call-1")
    assert entry.entry_kind is EntryKind.TOOL_RESULT
    assert entry.status is EntryStatus.DEMOTED
    assert entry.content == tombstones[0].content
    assert result.final_text == "已读完。"


def test_short_tool_message_passes_through(tmp_path):
    (tmp_path / "note.txt").write_text("短内容", encoding="utf-8")
    store = _store()
    model = ScriptedModel(
        [
            {"tool_calls": [_view_call("call-1")]},
            {"content": "ok"},
        ]
    )

    _run(
        run_tool_agent(
            history=[HumanMessage(content="读文件")],
            system_prompt="sys",
            tools=_tools(tmp_path),
            model=model,
            max_steps=6,
            before_model_hook=make_tool_message_hook(store),
        )
    )

    second_call = model.received[1]
    tool_msgs = [m for m in second_call if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert "短内容" in tool_msgs[0].content

    entry = store.get("tool:call-1")
    assert entry.entry_kind is EntryKind.TOOL_RESULT
    assert entry.status is EntryStatus.ACTIVE
    assert entry.tokens_est > 0


def test_hook_does_not_mutate_graph_state(tmp_path):
    (tmp_path / "note.txt").write_text("长" * 3000, encoding="utf-8")
    store = _store()
    model = ScriptedModel(
        [
            {"tool_calls": [_view_call("call-1")]},
            {"content": "完成"},
        ]
    )

    result = _run(
        run_tool_agent(
            history=[HumanMessage(content="读文件")],
            system_prompt="sys",
            tools=_tools(tmp_path),
            model=model,
            max_steps=6,
            before_model_hook=make_tool_message_hook(store, long_tool_chars=2000),
        )
    )

    # result.messages（图 state）保留原始长 ToolMessage
    long_tool = [
        m for m in result.messages
        if isinstance(m, ToolMessage) and len(m.content) > 2000
    ]
    assert len(long_tool) == 1
    # tool_trace 审计不动
    assert any(t["tool"] == "str_replace_editor" for t in result.tool_trace)


def test_default_no_hook_passes_raw_tool_message(tmp_path):
    (tmp_path / "note.txt").write_text("长" * 3000, encoding="utf-8")
    model = ScriptedModel(
        [
            {"tool_calls": [_view_call("call-1")]},
            {"content": "完成"},
        ]
    )

    _run(
        run_tool_agent(
            history=[HumanMessage(content="读文件")],
            system_prompt="sys",
            tools=_tools(tmp_path),
            model=model,
            max_steps=6,
        )
    )

    second_call = model.received[1]
    assert any(
        isinstance(m, ToolMessage) and len(m.content) > 2000 for m in second_call
    )


def test_consecutive_tool_calls_all_replaced(tmp_path):
    (tmp_path / "note.txt").write_text("长" * 3000, encoding="utf-8")
    store = _store()
    model = ScriptedModel(
        [
            {"tool_calls": [_view_call("call-1")]},
            {"tool_calls": [_view_call("call-2")]},
            {"content": "两批都读完"},
        ]
    )

    result = _run(
        run_tool_agent(
            history=[HumanMessage(content="读两次")],
            system_prompt="sys",
            tools=_tools(tmp_path),
            model=model,
            max_steps=6,
            before_model_hook=make_tool_message_hook(store, long_tool_chars=2000),
        )
    )

    third_call = model.received[2]
    assert not any(isinstance(m, ToolMessage) for m in third_call)
    tombstones = [
        m for m in third_call
        if isinstance(m, SystemMessage) and m.content.startswith("〔已归档 tool_result")
    ]
    assert len(tombstones) == 2
    assert store.get("tool:call-1").status is EntryStatus.DEMOTED
    assert store.get("tool:call-2").status is EntryStatus.DEMOTED
    # state 保留两个原始 ToolMessage
    assert len([m for m in result.messages if isinstance(m, ToolMessage)]) == 2
