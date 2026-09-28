"""M3: memory_search tool registration + auto_memory (mock ReMe)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tester_agent.memory.prompts import build_memory_guidance_prompt
from tester_agent.memory.tools import make_memory_search_tool
from tester_agent.tools.registry import ToolBuildContext, build_case_designer_tools


class FakeManager:
    def __init__(
        self,
        *,
        kb: bool = True,
        search_enabled: bool = True,
        started: bool = True,
        interval: int = 0,
    ):
        self._kb = kb
        self._search = search_enabled
        self.is_started = started
        self._interval = interval
        self.jobs: list[tuple[str, dict]] = []

    def kb_enabled(self) -> bool:
        return self._kb

    def memory_search_enabled(self) -> bool:
        return self._search

    def auto_memory_interval(self) -> int:
        return self._interval

    async def run_job(self, name: str, **kwargs):
        self.jobs.append((name, kwargs))
        return SimpleNamespace(
            success=True,
            answer=f"ok:{name}",
            metadata={"results": [{"id": "e1", "score": 0.9}]},
        )


def test_guidance_prompt_mentions_kb_when_enabled():
    text = build_memory_guidance_prompt(
        memory_search_enabled=True, knowledge_enabled=True
    )
    assert "memory_search" in text
    assert "确认" in text
    assert build_memory_guidance_prompt(memory_search_enabled=False) == ""


def test_build_tools_registers_memory_search_when_enabled(tmp_path: Path):
    mgr = FakeManager(search_enabled=True)
    tools = build_case_designer_tools(
        ToolBuildContext(
            owner_id="conversation:c1",
            workspace_root=tmp_path,
            runtime_config={},
            memory_manager=mgr,
        )
    )
    names = [t.name for t in tools]
    assert "memory_search" in names
    assert "save_to_knowledge" not in names


def test_build_tools_skips_memory_when_disabled(tmp_path: Path):
    mgr = FakeManager(search_enabled=False)
    tools = build_case_designer_tools(
        ToolBuildContext(
            owner_id="conversation:c1",
            workspace_root=tmp_path,
            runtime_config={},
            memory_manager=mgr,
        )
    )
    assert "memory_search" not in [t.name for t in tools]


@pytest.mark.asyncio
async def test_memory_search_agent_scope_calls_search():
    mgr = FakeManager(kb=False)
    tool = make_memory_search_tool(mgr)
    raw = await tool.ainvoke({"query": "prefs", "scope": "agent", "max_results": 3})
    payload = json.loads(raw)
    assert payload["success"] is True
    assert mgr.jobs[0][0] == "search"
    assert mgr.jobs[0][1]["query"] == "prefs"
    assert mgr.jobs[0][1]["limit"] == 3


@pytest.mark.asyncio
async def test_memory_search_knowledge_scope():
    mgr = FakeManager(kb=True)
    tool = make_memory_search_tool(mgr)
    await tool.ainvoke({"query": "链路", "scope": "knowledge", "bucket": "wiki"})
    assert mgr.jobs[0][0] == "knowledge_search"
    assert mgr.jobs[0][1]["bucket"] == "wiki"


@pytest.mark.asyncio
async def test_memory_search_rejects_empty_query():
    mgr = FakeManager()
    tool = make_memory_search_tool(mgr)
    out = await tool.ainvoke({"query": "  "})
    assert "Error" in out
    assert mgr.jobs == []


@pytest.mark.asyncio
async def test_auto_memory_fires_on_interval(monkeypatch, tmp_path: Path):
    from tester_agent.runtime import chat_agent as ca

    ca._auto_memory_counts.clear()
    mgr = FakeManager(interval=2, started=True)
    history = [
        HumanMessage(content="hi"),
        AIMessage(content="hello"),
    ]
    await ca._maybe_auto_memory(
        memory_manager=mgr, conversation_id="c-auto", history=history
    )
    assert mgr.jobs == []
    await ca._maybe_auto_memory(
        memory_manager=mgr, conversation_id="c-auto", history=history
    )
    assert len(mgr.jobs) == 1
    assert mgr.jobs[0][0] == "auto_memory"
    assert mgr.jobs[0][1]["session_id"] == "c-auto"


@pytest.mark.asyncio
async def test_run_chat_turn_passes_memory_manager_to_tools(
    monkeypatch, tmp_path: Path
):
    from tester_agent.runtime import chat_agent as ca
    from tester_agent.domain import MessageKind, MessageRole
    from tester_agent.store.models import ConversationRow, MessageRow

    mgr = FakeManager(search_enabled=True, started=True)
    pool = MagicMock()
    pool.get_or_start = AsyncMock(return_value=mgr)

    captured: dict = {}

    async def fake_run_tool_agent(**kwargs):
        captured["system_prompt"] = kwargs["system_prompt"]
        captured["tools"] = kwargs["tools"]
        from tester_agent.graph.tool_agent import ToolAgentResult

        return ToolAgentResult(final_text="ok", tool_trace=[])

    monkeypatch.setattr(ca, "run_tool_agent", fake_run_tool_agent)
    monkeypatch.setattr(
        ca,
        "get_chat_model",
        lambda **_k: object(),
    )

    class _Cfg:
        def model_dict(self):
            return {"provider": "deepseek", "model": "x"}

        def runtime_dict(self):
            return {"tool_agent_max_steps": 3}

    class _CfgDao:
        def __init__(self, *_a):
            pass

        async def get(self):
            return _Cfg()

    class _MsgDao:
        def __init__(self, *_a):
            pass

        async def list_by_conversation(self, *_a, **_k):
            return SimpleNamespace(items=[])

        async def put(self, row):
            return row

    class _Ws:
        def kb_config_obj(self):
            return {"kb_id": "kb-1", "options": {"memory_search_enabled": True}}

    class _WsDao:
        def __init__(self, *_a):
            pass

        async def get(self, *_a):
            return _Ws()

    monkeypatch.setattr(ca, "ConfigDAO", _CfgDao)
    monkeypatch.setattr(ca, "MessageDAO", _MsgDao)
    monkeypatch.setattr(ca, "WorkspaceDAO", _WsDao)

    file_store = SimpleNamespace(root=tmp_path)
    conv = ConversationRow.create(
        id="c1", workspace_id="ws1", title="t"
    )
    user = MessageRow.create(
        id="m1",
        conversation_id="c1",
        role=MessageRole.USER,
        kind=MessageKind.CHAT,
        content="hello",
    )
    out = await ca.run_chat_turn(
        db=object(),
        conv=conv,
        file_store=file_store,
        user_message=user,
        memory_pool=pool,
    )
    assert out.content == "ok"
    assert "memory_search" in captured["system_prompt"]
    assert "memory_search" in [t.name for t in captured["tools"]]
    pool.get_or_start.assert_awaited()
