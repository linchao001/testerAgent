"""WP-31 Task 9：chat_agent 经 CHAT profile 组装历史窗（spec §6.2）。

断言口径：
- 超过 K=6 的旧轮次以 tombstone（SystemMessage）占位，不出现在
  HumanMessage/AIMessage 位；最近 6 轮完整对话与新请求原样在窗；
- P0 含 methodology（等价类/边界值/判定表/场景法）；
- context.enabled=false 退回 system_prompt + 40 条 history 旧路径；
- 同会话多轮复用 owner store，assistant/异常文本均 append。
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.context import registry as reg_mod
from tester_agent.domain import MessageKind, MessageRole
from tester_agent.graph.tool_agent import ToolAgentResult
from tester_agent.main import create_app
from tester_agent.settings import Settings
from tester_agent.store.db import Database
from tester_agent.store.models import (
    ConfigDAO,
    MessageDAO,
    MessageRow,
    WorkspaceDAO,
    WorkspaceRow,
)

WS = "ws-context-chat"
KB_CONFIG = {
    "kb_id": "kb-1",
    "knowledge_bases_dir": "",
    "knowledge_dir": "knowledge",
    "create_knowledge_base": False,
    "options": {},
}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path):
    settings = Settings.from_env(
        {
            "TESTER_AGENT_DATA_DIR": str(tmp_path / "data"),
            "TESTER_AGENT_SINGLE_WORKER": "1",
        }
    )
    with TestClient(create_app(settings)) as client:
        db = Database(settings.app_db_path)
        _run(
            WorkspaceDAO(db).create(
                WorkspaceRow.create(id=WS, name="ctx-chat", kb_config=dict(KB_CONFIG))
            )
        )
        cid = client.post(
            "/api/v1/conversations", json={"workspace_id": WS, "title": "t"}
        ).json()["id"]
        yield client, db, cid
        db.close()


async def _seed_turns(db, cid, n):
    for i in range(1, n + 1):
        await MessageDAO(db).put(
            MessageRow.create(
                id=uuid.uuid4().hex,
                conversation_id=cid,
                role=MessageRole.USER,
                kind=MessageKind.CHAT,
                content=f"旧请求编号{i:02d}的完整正文ABC",
                now=f"2026-09-01T00:{i:02d}:00Z",
            )
        )
        await MessageDAO(db).put(
            MessageRow.create(
                id=uuid.uuid4().hex,
                conversation_id=cid,
                role=MessageRole.ASSISTANT,
                kind=MessageKind.CHAT,
                content=f"旧回复编号{i:02d}的完整正文XYZ",
                now=f"2026-09-01T00:{i:02d}:30Z",
            )
        )


def _patch_loop(monkeypatch, *, final_text="最终回复内容", exc=None):
    monkeypatch.setattr(
        "tester_agent.runtime.chat_agent.get_chat_model", lambda **_kw: object()
    )
    if exc is None:
        spy = AsyncMock(
            return_value=ToolAgentResult(final_text=final_text, tool_trace=[])
        )
    else:
        spy = AsyncMock(side_effect=exc)
    monkeypatch.setattr("tester_agent.runtime.chat_agent.run_tool_agent", spy)
    return spy


def _post(env, content="新请求正文", *, expect=201):
    client, _, cid = env
    resp = client.post(
        f"/api/v1/conversations/{cid}/messages",
        json={"content": content, "kind": "chat"},
    )
    assert resp.status_code == expect
    return resp.json()


def _store(cid):
    return reg_mod.get_owner("conversation", cid)


def test_old_turns_tombstoned_recent_six_full(env, monkeypatch):
    _, db, cid = env
    _run(_seed_turns(db, cid, 10))
    spy = _patch_loop(monkeypatch)

    _post(env, "新请求正文")
    kw = spy.call_args.kwargs
    msgs = kw["initial_messages"]

    human_texts = [m.content for m in msgs if isinstance(m, HumanMessage)]
    ai_texts = [m.content for m in msgs if isinstance(m, AIMessage)]

    # 最近 6 轮（turn 6..10）完整 user/assistant 在对应消息位
    for i in range(6, 11):
        assert f"旧请求编号{i:02d}的完整正文ABC" in human_texts
        assert f"旧回复编号{i:02d}的完整正文XYZ" in ai_texts
    # turn 1..5 不在 Human/AI 位
    for i in range(1, 6):
        assert f"旧请求编号{i:02d}的完整正文ABC" not in human_texts
        assert f"旧回复编号{i:02d}的完整正文XYZ" not in ai_texts
    # 新请求在窗
    assert "新请求正文" in human_texts

    # 当轮 cut：turn 1..5 在 store 中已 DEMOTED，正文为 tombstone
    store = _store(cid)
    demoted = [e for e in store.entries() if e.status.value == "demoted"]
    assert len(demoted) == 10  # 5 轮 × user/assistant
    for e in demoted:
        assert e.content.startswith("〔已归档 chat_turn #chat:")

    # 次轮：tombstone 渲染进窗口，turn 6 被新 cut
    _post(env, "第二轮新请求")
    second = spy.call_args.kwargs["initial_messages"]
    human2 = [m.content for m in second if isinstance(m, HumanMessage)]
    sys_text = "\n".join(m.content for m in second if isinstance(m, SystemMessage))
    assert sys_text.count("〔已归档 chat_turn") == 10
    assert f"旧请求编号06的完整正文ABC" not in human2
    assert "第二轮新请求" in human2
    assert "新请求正文" in human2  # turn 11 仍在 K 窗


def test_p0_contains_methodology(env, monkeypatch):
    spy = _patch_loop(monkeypatch)
    _post(env)
    msgs = spy.call_args.kwargs["initial_messages"]
    p0 = msgs[0].content
    for word in ("等价类", "边界值", "判定表", "场景法"):
        assert word in p0
    # persona 也在 P0
    assert "TesterAgent" in p0


def test_second_turn_reuses_store_and_appends(env, monkeypatch):
    _, db, cid = env
    _run(_seed_turns(db, cid, 10))
    spy = _patch_loop(monkeypatch)

    _post(env, "第一轮新请求")
    _post(env, "第二轮新请求")

    second_msgs = spy.call_args.kwargs["initial_messages"]
    human_texts = [m.content for m in second_msgs if isinstance(m, HumanMessage)]
    assert "第二轮新请求" in human_texts
    assert "第一轮新请求" in human_texts  # turn 11 在最近 6 轮内

    store = _store(cid)
    chat_entries = [e for e in store.entries() if e.entry_kind.value == "chat_turn"]
    # 20 回填 + 2 user + 2 assistant
    assert len(chat_entries) == 24


def test_flag_off_uses_legacy_40_history(env, monkeypatch):
    _, db, cid = env
    _run(_seed_turns(db, cid, 10))
    _run(
        ConfigDAO(db).update_runtime(
            {"context.enabled": False, "tool_agent_max_steps": 12}
        )
    )
    spy = _patch_loop(monkeypatch)

    _post(env)
    kw = spy.call_args.kwargs
    assert kw.get("initial_messages") is None
    assert kw.get("before_model_hook") is None
    assert kw["system_prompt"]
    # 20 条旧消息 + 当前 user（端点先落库）
    assert len(kw["history"]) == 21
    assert isinstance(kw["history"][-1], HumanMessage)
    assert kw["history"][-1].content == "新请求正文"


def test_context_path_attaches_before_model_hook(env, monkeypatch):
    spy = _patch_loop(monkeypatch)
    _post(env)
    assert spy.call_args.kwargs["before_model_hook"] is not None


def test_assistant_appended_to_store_with_same_turn_seq(env, monkeypatch):
    _, db, cid = env
    _run(_seed_turns(db, cid, 10))
    _patch_loop(monkeypatch, final_text="最终回复内容")

    _post(env)
    store = _store(cid)

    new_user = next(
        e
        for e in store.entries()
        if e.entry_kind.value == "chat_turn" and e.content == "新请求正文"
    )
    new_ai = next(
        e
        for e in store.entries()
        if e.entry_kind.value == "chat_turn" and e.content == "最终回复内容"
    )
    assert new_user.role == "user"
    assert new_ai.role == "assistant"
    assert new_user.turn_seq == new_ai.turn_seq == 11


def test_exception_path_persists_and_appends(env, monkeypatch):
    _, db, cid = env
    _run(_seed_turns(db, cid, 10))
    spy = _patch_loop(monkeypatch, exc=RuntimeError("boom"))

    body = _post(env)
    assert "工具对话失败" in body["assistant"]["content"]

    store = _store(cid)
    ai_entries = [
        e
        for e in store.entries()
        if e.entry_kind.value == "chat_turn" and e.role == "assistant"
    ]
    assert "boom" in ai_entries[-1].content
