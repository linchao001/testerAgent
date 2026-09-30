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
from types import SimpleNamespace
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
    ContextJournalDAO,
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
    "编辑当前工作区内的文件。路径相对于工作区根目录。回答使用简洁中文。\n"
    "当用户已通过对话提供足够的需求正文（粘贴或附件），并明确要求生成/"
    "设计用例时，调用 start_case_generation："
    "优先传 requirement_md（完整 Markdown）；若需求已写入工作区文件可传 path。"
    "同会话已有进行中的任务时不要重复调用。"
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
    """Sandbox root aligned with FileStore layout: ``{workspace_root}/``."""
    return file_store.workspace_root(workspace_id)


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
    *,
    conv: ConversationRow,
    root: Path,
    runtime_config: dict,
    memory_manager: Any | None,
    context_store: Any | None = None,
    start_case_deps: Any | None = None,
):
    return build_case_designer_tools(
        ToolBuildContext(
            owner_id=f"conversation:{conv.id}",
            workspace_root=root,
            runtime_config=runtime_config,
            memory_manager=memory_manager
            if memory_manager and memory_manager.is_started
            else None,
            context_store=context_store,
            start_case_deps=start_case_deps,
        )
    )


def _make_start_case_deps(
    *,
    db,
    file_store: FileStore,
    conv: ConversationRow,
    root: Path,
    runner: Any | None,
) -> Any:
    from ..tools.start_case import StartCaseDeps

    return StartCaseDeps(
        db=db,
        file_store=file_store,
        conversation_id=conv.id,
        workspace_id=conv.workspace_id,
        workspace_root=root,
        runner=runner,
        outcome={},
    )


def _payload_with_started_task(
    payload: dict, start_case_deps: Any | None
) -> dict:
    out = dict(payload)
    if start_case_deps is None:
        return out
    outcome = getattr(start_case_deps, "outcome", None) or {}
    tid = outcome.get("started_task_id")
    if tid:
        out["started_task_id"] = tid
    return out


async def try_slash_context_command(
    store: Any, text: str
) -> tuple[bool, str]:
    """识别 ``/context <verb> <selector|args>``；命中则直通执行器。

    返回 ``(handled, reply_text)``；未命中前缀时 handled=False。
    """
    raw = (text or "").strip()
    if not raw.lower().startswith("/context"):
        return False, ""
    from ..context.intervention import ContextAction, ContextCommand, execute

    parts = raw.split(None, 2)  # /context VERB REST
    if len(parts) < 2:
        return True, "用法：/context <pin|unpin|forget|show|set_goal|budget|freeze|unfreeze> ..."
    verb = parts[1].lower()
    rest = parts[2] if len(parts) > 2 else ""
    try:
        action = ContextAction(verb)
    except ValueError:
        return True, f"未知 context 动词：{verb}"
    arg = None
    selector = rest
    if action is ContextAction.SET_GOAL:
        arg = rest
        selector = ""
    elif action is ContextAction.BUDGET:
        # /context budget chat 1000 2000 3000
        bits = rest.split()
        if len(bits) >= 4:
            import json

            arg = json.dumps(
                {
                    "profile": bits[0],
                    "p0": int(bits[1]),
                    "p1": int(bits[2]),
                    "p2": int(bits[3]),
                }
            )
            selector = ""
        else:
            return True, "用法：/context budget <profile> <p0> <p1> <p2>"
    result = await execute(
        store,
        ContextCommand(action=action, selector=selector, arg=arg),
        operator="user",
    )
    return True, (
        f"context {result.action} ok affected={result.affected} {result.message}"
    )


async def run_chat_turn(
    *,
    db,
    conv: ConversationRow,
    file_store: FileStore,
    user_message: MessageRow,
    memory_pool: Any | None = None,
    context_registry: ContextRegistry | Any | None = None,
    runner: Any | None = None,
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
                runner=runner,
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
            runner=runner,
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
    runner: Any | None = None,
) -> MessageRow:
    history_page = await MessageDAO(db).list_by_conversation(
        conv.id, cursor=None, limit=_LEGACY_HISTORY_LIMIT
    )
    history = db_messages_to_lc(list(reversed(history_page.items)))
    root = workspace_root_for(file_store, conv.workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    start_deps = _make_start_case_deps(
        db=db, file_store=file_store, conv=conv, root=root, runner=runner
    )

    tools = _build_tools(
        conv=conv,
        root=root,
        runtime_config=runtime_config,
        memory_manager=memory_manager,
        start_case_deps=start_deps,
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
        payload = _payload_with_started_task(
            {"tool_trace": [dict(t) for t in result.tool_trace]},
            start_deps,
        )
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
    runner: Any | None = None,
) -> MessageRow:
    # 无组合根注册表（app_ctx 未构造）时退回进程默认注册表：模块对象暴露
    # 同名 start_owner/get_owner 函数，结构对型。
    registry: Any = context_registry
    if registry is None:
        from ..context import registry as reg_mod

        registry = reg_mod

    store = registry.get_owner("conversation", conv.id)
    if store is None:
        journal_dao = ContextJournalDAO(db)
        daos = SimpleNamespace(
            message=MessageDAO(db),
            journal=journal_dao,
            artifact=None,
            trace=None,
        )
        store = await registry.restore_owner(
            owner_type="conversation",
            owner_id=conv.id,
            workspace_id=conv.workspace_id,
            daos=daos,
            journal_sink=journal_dao,
            policy_version=str(runtime_config.get("context.policy_version", "cp-v1")),
            step_window=int(runtime_config.get("context.step_window", 1)),
        )
        current_turn = _max_turn_seq(store)
        chat_eid = f"chat:{user_message.id}"
        if not any(e.entry_id == chat_eid for e in store.entries()):
            current_turn = current_turn + 1
            await store.append(
                ContextEntry(
                    entry_id=chat_eid,
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
    start_deps = _make_start_case_deps(
        db=db, file_store=file_store, conv=conv, root=root, runner=runner
    )
    tools = _build_tools(
        conv=conv,
        root=root,
        runtime_config=runtime_config,
        memory_manager=memory_manager,
        context_store=store,
        start_case_deps=start_deps,
    )
    max_steps = int(runtime_config.get("tool_agent_max_steps", 12))

    # 斜杠命令直通执行器（不经 LLM）
    if bool(runtime_config.get("context.intervention.enabled", True)):
        handled, slash_text = await try_slash_context_command(
            store, user_message.content
        )
        if handled:
            content = slash_text or "（已执行）"
            payload = {"tool_trace": [], "slash": True}
            assistant = MessageRow.create(
                id=uuid.uuid4().hex,
                conversation_id=conv.id,
                role=MessageRole.ASSISTANT,
                kind=MessageKind.CHAT,
                content=content,
                payload=payload,
            )
            await MessageDAO(db).put(assistant)
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
            return assistant

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
        payload = _payload_with_started_task(
            {"tool_trace": [dict(t) for t in result.tool_trace]},
            start_deps,
        )
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


# ---------- 辅助 ----------


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
