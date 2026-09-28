"""LangGraph tool-agent subgraph: model(bind_tools) ↔ ToolNode."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Sequence, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from ..tools.trace import ToolTraceEntry, args_digest


class _AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]
    step: int


@dataclass
class ToolAgentResult:
    final_text: str
    tool_trace: list[ToolTraceEntry] = field(default_factory=list)
    messages: list[BaseMessage] = field(default_factory=list)


def build_tool_agent_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    *,
    max_steps: int = 12,
    before_model_hook: Callable[[list[BaseMessage], int], Awaitable[list[BaseMessage]]]
    | None = None,
):
    """Compile model ↔ ToolNode loop.

    WP-31：``before_model_hook`` 在每次模型调用前对消息列表做变换（如把
    超长 ToolMessage 替换为 tombstone）；默认 None 时行为字节不变。hook
    只改模型入参，不回写图 state（tool_trace 不受影响）。
    """

    bound = model.bind_tools(tools)
    tool_node = ToolNode(tools)

    async def call_model(state: _AgentState) -> dict:
        step = int(state.get("step") or 0) + 1
        if step > max_steps:
            return {
                "messages": [
                    AIMessage(
                        content="达到工具轮次上限，已停止继续调用工具。请基于已有结果作答。"
                    )
                ],
                "step": step,
            }
        msgs: list[BaseMessage] = list(state["messages"])
        if before_model_hook is not None:
            msgs = await before_model_hook(msgs, step)
        resp = await bound.ainvoke(msgs)
        return {"messages": [resp], "step": step}

    def should_continue(state: _AgentState) -> str:
        step = int(state.get("step") or 0)
        if step >= max_steps:
            return "end"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "end"

    g = StateGraph(_AgentState)
    g.add_node("model", call_model)
    g.add_node("tools", tool_node)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", should_continue, {"tools": "tools", "end": END})
    g.add_edge("tools", "model")
    return g.compile()


async def run_tool_agent(
    *,
    history: list[BaseMessage] | None = None,
    system_prompt: str | None = None,
    tools: list[BaseTool],
    model: BaseChatModel,
    max_steps: int = 12,
    before_model_hook: Callable[[list[BaseMessage], int], Awaitable[list[BaseMessage]]]
    | None = None,
    initial_messages: list[BaseMessage] | None = None,
) -> ToolAgentResult:
    """运行工具环。

    消息入口二选一：``initial_messages``（调用方已组装好的完整序列，如
    context assembler 产出）或默认 ``[SystemMessage(system_prompt), *history]``。
    """
    graph = build_tool_agent_graph(
        model, tools, max_steps=max_steps, before_model_hook=before_model_hook
    )
    if initial_messages is not None:
        messages: list[BaseMessage] = list(initial_messages)
    else:
        messages = [SystemMessage(content=system_prompt or ""), *(history or [])]
    result = await graph.ainvoke(
        {"messages": messages, "step": 0},
        {"recursion_limit": max(max_steps * 2 + 2, 8)},
    )
    out_msgs: list[BaseMessage] = list(result["messages"])
    trace: list[ToolTraceEntry] = []
    pending: dict[str, ToolTraceEntry] = {}
    for msg in out_msgs:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tc in msg.tool_calls:
                tid = tc.get("id") or ""
                pending[tid] = {
                    "tool": tc.get("name") or "unknown",
                    "ok": True,
                    "latency_ms": 0,
                    "args_digest": args_digest(tc.get("args") or {}),
                }
        elif isinstance(msg, ToolMessage):
            entry = pending.pop(msg.tool_call_id, None) or {
                "tool": "unknown",
                "ok": True,
                "latency_ms": 0,
                "args_digest": "",
            }
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if content.startswith("Error:"):
                entry["ok"] = False
                entry["error_code"] = "ToolError"
            trace.append(entry)

    final_text = ""
    for msg in reversed(out_msgs):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            final_text = msg.content if isinstance(msg.content, str) else str(msg.content)
            break
    return ToolAgentResult(final_text=final_text, tool_trace=trace, messages=out_msgs)
