"""graph 侧上下文 hook（WP-31 Task 10，spec §6.3）。

hook 实现放在 graph 层（依赖方向 graph → context 合法），保证 context
叶子包不感知 LangGraph。职责：

1. 新 ToolMessage 首次出现时向 store 登记 P2 TOOL_RESULT 条目；
2. 下一次模型调用前把**超长** ToolMessage 在 store 中 demote（budget_cut），
   并以 tombstone SystemMessage 替换其在模型输入中的位置。

只改模型入参，不回写图 state——tool_trace 与消息审计均不动（I1）。
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage

from ..context._time import utcnow_iso
from ..context.models import ContextEntry, ContextPartition, EntryKind, EntryRefs
from ..context.policy import programmatic_digest
from ..context.tokens import estimate_tokens

# ToolMessage 正文超过该字符数即判定为"长输出"，下一轮模型调用前归档
DEFAULT_LONG_TOOL_CHARS = 2000


def make_tool_message_hook(
    store,
    *,
    long_tool_chars: int = DEFAULT_LONG_TOOL_CHARS,
):
    """构造 run_tool_agent 的 before_model_hook，闭包持有 owner store。"""

    registered: set[str] = set()
    replaced: set[str] = set()

    async def hook(messages: list[BaseMessage], step: int) -> list[BaseMessage]:
        out: list[BaseMessage] = []
        for msg in messages:
            if not isinstance(msg, ToolMessage):
                out.append(msg)
                continue
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            entry_id = f"tool:{msg.tool_call_id}"

            if entry_id not in registered:
                registered.add(entry_id)
                await store.append(
                    ContextEntry(
                        entry_id=entry_id,
                        partition=ContextPartition.P2,
                        entry_kind=EntryKind.TOOL_RESULT,
                        content=content,
                        digest=programmatic_digest(content),
                        tokens_est=estimate_tokens(content),
                        refs=EntryRefs(),
                        created_at=utcnow_iso(),
                    )
                )

            if len(content) > long_tool_chars:
                if entry_id not in replaced:
                    await store.demote(entry_id, "budget_cut")
                    replaced.add(entry_id)
                # 以 tombstone 替换模型输入中的原始 ToolMessage
                out.append(SystemMessage(content=store.get(entry_id).content))
            else:
                out.append(msg)
        return out

    return hook
