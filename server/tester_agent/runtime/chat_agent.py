"""Conversation chat orchestration with tool_agent_graph.

WP-31：历史窗从「最近 40 条全量」改为经 context 层 CHAT profile 组装
（P0 methodology 冻结段 + P2 最近 K=6 轮完整对话 + tombstone）。用户/
助手消息在落库前后同步 append 到 owner(conversation) store。特性开关
``context.enabled=false`` 时走 :func:`_run_legacy`，行为与旧路径完全一致。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from ..adapters.llm import get_chat_model
from ..context.assembler import assemble
from ..context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryRefs,
    ProfileName,
)
from ..context.p0 import bootstrap_p0
from ..context.policy import programmatic_digest
from ..context.registry import ContextRegistry
from ..context.scopes import scope
from ..domain import MessageKind, MessageRole
from ..graph.context_hooks import make_tool_message_hook
from ..graph.tool_agent import ToolAgentResult, run_tool_agent
from ..memory.prompts import build_memory_guidance_prompt
from ..prompts.loader import PromptLoader
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

# 旧路径历史窗大小（context.enabled=false 时保持）
_LEGACY_HISTORY_LIMIT = 40
# P0 未显式配置模型窗口时的兜底（model_config 无 context_window 约定键）
_DEFAULT_MODEL_WINDOW = 128_000


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


async def _start_memory_manager(
    *, db, conv: ConversationRow, memory_pool: Any | None, model_config: dict
) -> Any | None:
    if memory_pool is None:
        return None
    try:
        ws = await WorkspaceDAO(db).get(conv.workspace_id)
        return await memory_pool.get_or_start(
            conv.workspace_id,
            ws.kb_config_obj(),
            model_config=model_config,
        )
    except Exception:
        logger.exception("memory pool start failed for ws=%s", conv.workspace_id)
        return None


def _build_tools(
    *, conv: ConversationRow, root: Path, runtime_config: dict, memory_manager: Any | None
):
    return build_case_designer_tools(
        ToolBuildContext(
            owner_id=f"conversation:{conv.id}",
            workspace_root=root,
            runtime_config=runtime_config,
            memory_manager=memory_manager
            if memory_manager and memory_manager.is_started
            else None,
        )
    )


async def run_chat_turn(
    *,
    db,
    conv: ConversationRow,
    file_store: FileStore,
    user_message: MessageRow,
    memory_pool: Any | None = None,
    context_registry: ContextRegistry | Any | None = None,
) -> MessageRow:
    """Run tool agent for a chat user message; persist and return assistant row."""
    async with _conv_lock(conv.id):
        cfg = await ConfigDAO(db).get()
        model_config = cfg.model_dict()
        runtime_config = cfg.runtime_dict()

        memory_manager = await _start_memory_manager(
            db=db, conv=conv, memory_pool=memory_pool, model_config=model_config
        )

        if not bool(runtime_config.get("context.enabled", True)):
            return await _run_legacy(
                db=db,
                conv=conv,
                file_store=file_store,
                user_message=user_message,
                runtime_config=runtime_config,
                memory_manager=memory_manager,
            )
        return await _run_context(
            db=db,
            conv=conv,
            file_store=file_store,
            user_message=user_message,
            model_config=model_config,
            runtime_config=runtime_config,
            memory_manager=memory_manager,
            context_registry=context_registry,
        )


# ---------- 旧路径（context.enabled=false，完全可回退） ----------


async def _run_legacy(
    *,
    db,
    conv: ConversationRow,
    file_store: FileStore,
    user_message: MessageRow,
    runtime_config: dict,
    memory_manager: Any | None,
) -> MessageRow:
    history_page = await MessageDAO(db).list_by_conversation(
        conv.id, cursor=None, limit=_LEGACY_HISTORY_LIMIT
    )
    history = db_messages_to_lc(list(reversed(history_page.items)))
    root = workspace_root_for(file_store, conv.workspace_id)
    root.mkdir(parents=True, exist_ok=True)

    tools = _build_tools(
        conv=conv, root=root, runtime_config=runtime_config,
        memory_manager=memory_manager,
    )
    max_steps = int(runtime_config.get("tool_agent_max_steps", 12))
    try:
        model = get_chat_model(
            model_config=(await ConfigDAO(db).get()).model_dict(),
            runtime_config=runtime_config,
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


# ---------- context 路径（CHAT profile 组装） ----------


async def _run_context(
    *,
    db,
    conv: ConversationRow,
    file_store: FileStore,
    user_message: MessageRow,
    model_config: dict,
    runtime_config: dict,
    memory_manager: Any | None,
    context_registry: ContextRegistry | Any | None,
) -> MessageRow:
    # 无组合根注册表（app_ctx 未构造）时退回进程默认注册表：模块对象暴露
    # 同名 start_owner/get_owner 函数，结构对型。
    registry: Any = context_registry
    if registry is None:
        from ..context import registry as reg_mod

        registry = reg_mod

    store = registry.get_owner("conversation", conv.id)
    if store is None:
        store = registry.start_owner(
            owner_type="conversation",
            owner_id=conv.id,
            workspace_id=conv.workspace_id,
            policy_version=str(runtime_config.get("context.policy_version", "cp-v1")),
        )
        # WP-32 Task 13 前的过渡：首次 start 从 MessageDAO 回填 CHAT_TURN
        current_turn = await _backfill_chat_history(db, conv.id, store)
    else:
        current_turn = _max_turn_seq(store) + 1
        await store.append(
            ContextEntry(
                entry_id=f"chat:{user_message.id}",
                partition=ContextPartition.P2,
                entry_kind=EntryKind.CHAT_TURN,
                role="user",
                content=user_message.content,
                digest=programmatic_digest(user_message.content),
                turn_seq=current_turn,
                refs=EntryRefs(message_id=user_message.id),
                created_at=user_message.created_at,
            )
        )

    root = workspace_root_for(file_store, conv.workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    tools = _build_tools(
        conv=conv, root=root, runtime_config=runtime_config,
        memory_manager=memory_manager,
    )
    max_steps = int(runtime_config.get("tool_agent_max_steps", 12))

    # P0：methodology 模板 + 经 extra_static 注入会话 persona（含记忆指引）
    p0_messages, p0_version, _ = bootstrap_p0(
        loader=PromptLoader(),
        template_names=["methodology"],
        extra_static=_system_prompt(memory_manager),
    )
    store.bind_p0(p0_version)

    recent_turns = int(runtime_config.get("context.chat_recent_turns", 6))
    model_window = int(model_config.get("context_window") or _DEFAULT_MODEL_WINDOW)
    drift_window = int(runtime_config.get("context.goal_drift_window", 5))

    try:
        model = get_chat_model(
            model_config=model_config, runtime_config=runtime_config
        )
        async with scope(turn_seq=current_turn):
            assembled = await assemble(
                store,
                ProfileName.CHAT,
                p0_messages=p0_messages,
                model_window=model_window,
                recent_turns=recent_turns,
                goal_drift_window=drift_window,
            )
            result = await run_tool_agent(
                initial_messages=assembled.messages,
                tools=tools,
                model=model,
                max_steps=max_steps,
                before_model_hook=make_tool_message_hook(store),
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

    # assistant CHAT_TURN（与 user 同 turn_seq）
    await store.append(
        ContextEntry(
            entry_id=f"chat:{assistant.id}",
            partition=ContextPartition.P2,
            entry_kind=EntryKind.CHAT_TURN,
            role="assistant",
            content=content,
            digest=programmatic_digest(content),
            turn_seq=current_turn,
            refs=EntryRefs(message_id=assistant.id),
            created_at=assistant.created_at,
        )
    )

    if memory_manager is not None:
        asyncio.create_task(
            _maybe_auto_memory(
                memory_manager=memory_manager,
                conversation_id=conv.id,
                history=_recent_active_lc(store),
            )
        )
    return assistant


# ---------- 过渡回填与辅助 ----------


async def _backfill_chat_history(
    db, conversation_id: str, store, *, limit: int = _LEGACY_HISTORY_LIMIT
) -> int:
    """首次 start：把最近消息转 CHAT_TURN 灌入空 store。

    TODO(WP-32 Task 13): 由 rebuild_store（artifact/message + journal
    重放）替换本过渡逻辑。
    """
    page = await MessageDAO(db).list_by_conversation(
        conversation_id, cursor=None, limit=limit
    )
    rows = [
        m
        for m in reversed(page.items)
        if m.kind == MessageKind.CHAT
        and m.role in (MessageRole.USER, MessageRole.ASSISTANT)
    ]
    turn = 0
    expect_assistant = False
    for m in rows:
        if m.role == MessageRole.USER:
            turn += 1
            expect_assistant = True
        elif not expect_assistant:
            turn += 1  # 无配对 user 的孤立 assistant（防御性编号）
        await store.append(
            ContextEntry(
                entry_id=f"chat:{m.id}",
                partition=ContextPartition.P2,
                entry_kind=EntryKind.CHAT_TURN,
                role=m.role,
                content=m.content,
                digest=programmatic_digest(m.content),
                turn_seq=turn,
                refs=EntryRefs(message_id=m.id),
                created_at=m.created_at,
            )
        )
        if m.role == MessageRole.ASSISTANT:
            expect_assistant = False
    return turn


def _max_turn_seq(store) -> int:
    return max((e.turn_seq for e in store.entries() if e.turn_seq), default=0)


def _recent_active_lc(store, *, limit: int = 12) -> list[BaseMessage]:
    from ..context.models import EntryStatus

    turns = sorted(
        (
            e
            for e in store.entries()
            if e.entry_kind is EntryKind.CHAT_TURN and e.status is EntryStatus.ACTIVE
        ),
        key=lambda e: (e.turn_seq, e.created_at, e.entry_id),
    )[-limit:]
    out: list[BaseMessage] = []
    for e in turns:
        if e.role == "user":
            out.append(HumanMessage(content=e.content))
        elif e.role == "assistant":
            out.append(AIMessage(content=e.content))
    return out
