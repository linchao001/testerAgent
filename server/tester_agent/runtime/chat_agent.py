"""Conversation chat orchestration with tool_agent_graph."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from ..adapters.llm import get_chat_model
from ..domain import MessageKind, MessageRole
from ..graph.tool_agent import ToolAgentResult, run_tool_agent
from ..memory.prompts import build_memory_guidance_prompt
from ..store.models import (
    ConfigDAO,
    ConversationRow,
    MessageDAO,
    MessageRow,
    WorkspaceDAO,
)
from ..store.workspace_files import FileStore
from ..tools.registry import ToolBuildContext, build_case_designer_tools

logger = logging.getLogger(__name__)

_SYSTEM = (
    "你是 TesterAgent 用例设计助手。可使用 bash 与 str_replace_editor 查看/"
    "编辑当前工作区内的文件。路径相对于工作区根目录。回答使用简洁中文。"
)

_locks: dict[str, asyncio.Lock] = {}
_auto_memory_counts: dict[str, int] = {}


def _conv_lock(conversation_id: str) -> asyncio.Lock:
    lock = _locks.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[conversation_id] = lock
    return lock


def db_messages_to_lc(rows: list[MessageRow]) -> list[BaseMessage]:
    out: list[BaseMessage] = []
    for m in rows:
        if m.role == MessageRole.USER:
            out.append(HumanMessage(content=m.content))
        elif m.role == MessageRole.ASSISTANT:
            out.append(AIMessage(content=m.content))
    return out


def workspace_root_for(file_store: FileStore, workspace_id: str) -> Path:
    """Sandbox root aligned with FileStore layout: ``{root}/workspaces/{id}/``."""
    return file_store.root / "workspaces" / workspace_id


def _system_prompt(memory_manager: Any | None) -> str:
    base = _SYSTEM
    if memory_manager is None:
        return base
    guidance = build_memory_guidance_prompt(
        memory_search_enabled=memory_manager.memory_search_enabled(),
        knowledge_enabled=memory_manager.kb_enabled(),
    )
    if guidance:
        return f"{base}\n\n{guidance}"
    return base


async def _maybe_auto_memory(
    *,
    memory_manager: Any,
    conversation_id: str,
    history: list[BaseMessage],
) -> None:
    interval = memory_manager.auto_memory_interval()
    if interval <= 0 or not memory_manager.is_started:
        return
    count = _auto_memory_counts.get(conversation_id, 0) + 1
    _auto_memory_counts[conversation_id] = count
    if count % interval != 0:
        return
    messages = [
        {"role": "user" if isinstance(m, HumanMessage) else "assistant", "content": str(m.content)}
        for m in history[-12:]
    ]
    try:
        await memory_manager.run_job(
            "auto_memory",
            messages=messages,
            session_id=conversation_id,
        )
    except Exception:
        logger.exception("auto_memory failed for %s", conversation_id)


async def run_chat_turn(
    *,
    db,
    conv: ConversationRow,
    file_store: FileStore,
    user_message: MessageRow,
    memory_pool: Any | None = None,
) -> MessageRow:
    """Run tool agent for a chat user message; persist and return assistant row."""
    async with _conv_lock(conv.id):
        cfg = await ConfigDAO(db).get()
        model_config = cfg.model_dict()
        runtime_config = cfg.runtime_dict()
        history_page = await MessageDAO(db).list_by_conversation(
            conv.id, cursor=None, limit=40
        )
        history = db_messages_to_lc(list(reversed(history_page.items)))
        root = workspace_root_for(file_store, conv.workspace_id)
        root.mkdir(parents=True, exist_ok=True)

        memory_manager = None
        if memory_pool is not None:
            try:
                ws = await WorkspaceDAO(db).get(conv.workspace_id)
                memory_manager = await memory_pool.get_or_start(
                    conv.workspace_id,
                    ws.kb_config_obj(),
                    model_config=model_config,
                )
            except Exception:
                logger.exception(
                    "memory pool start failed for ws=%s", conv.workspace_id
                )
                memory_manager = None

        tools = build_case_designer_tools(
            ToolBuildContext(
                owner_id=f"conversation:{conv.id}",
                workspace_root=root,
                runtime_config=runtime_config,
                memory_manager=memory_manager
                if memory_manager and memory_manager.is_started
                else None,
            )
        )
        max_steps = int(runtime_config.get("tool_agent_max_steps", 12))
        try:
            model = get_chat_model(
                model_config=model_config, runtime_config=runtime_config
            )
            result: ToolAgentResult = await run_tool_agent(
                history=history,
                system_prompt=_system_prompt(memory_manager),
                tools=tools,
                model=model,
                max_steps=max_steps,
            )
            content = result.final_text or "（无文本回复）"
            payload = {"tool_trace": [dict(t) for t in result.tool_trace]}
        except Exception as exc:  # noqa: BLE001 — surface to chat UI
            content = f"工具对话失败：{exc}"
            payload = {"tool_trace": [], "error": type(exc).__name__}

        assistant = MessageRow.create(
            id=uuid.uuid4().hex,
            conversation_id=conv.id,
            role=MessageRole.ASSISTANT,
            kind=MessageKind.CHAT,
            content=content,
            payload=payload,
        )
        await MessageDAO(db).put(assistant)

        if memory_manager is not None:
            asyncio.create_task(
                _maybe_auto_memory(
                    memory_manager=memory_manager,
                    conversation_id=conv.id,
                    history=history + [HumanMessage(content=user_message.content)],
                )
            )
        return assistant
