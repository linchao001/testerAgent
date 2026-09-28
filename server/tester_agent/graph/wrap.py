"""节点统一包装（能力单测 / 节点函数 → LangGraph 节点）。

``wrap(node_fn)`` 把"业务节点函数 (ctx, state) -> 状态增量 dict"包装成
LangGraph 节点函数 ``(state, config) -> dict``，统一承担：

1. **TaskContext 注入**：``config["configurable"]["ctx"]``；
2. **取消检查**：节点执行前 ``ctx.cancelled()`` → :class:`TaskCancelled`；
3. **事件发射**：``node_start`` / ``node_end``；
4. **异常映射**：``TaskCancelled`` / ``GraphInterrupt`` / ``AppError`` 穿透，
   其余兜底为 INTERNAL。

生产控制环经 ``invoke_capability`` 直接调节点函数；``wrap`` 主要用于
节点级单测小图。
"""

# 注意：本模块不能加 ``from __future__ import annotations`` —— LangGraph
# 对节点函数 config 参数做的是注解对象相等性检查（要求 RunnableConfig 类型
# 本体），字符串化注解会被判定为类型错误并拒绝注入 RunnableConfig。

import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt

from ..errors import AppError, TaskCancelled, current_trace_id
from ..logging_config import get_logger
from .state import TaskState

logger = get_logger(__name__)

NodeFn = Callable[..., Awaitable[dict[str, Any]]]
CTX_KEY = "ctx"


def _node_name_of(node_fn: Callable[..., Any], explicit: str | None) -> str:
    if explicit:
        return explicit
    name = getattr(node_fn, "__name__", "")
    return name[: -len("_node")] if name.endswith("_node") else name


def wrap(node_fn: NodeFn, *, name: str | None = None) -> Callable[..., Awaitable[dict]]:
    """包装业务节点为 LangGraph 节点。"""
    node_name = _node_name_of(node_fn, name)

    async def _wrapped(
        state: TaskState, config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        ctx = _ctx_from_config(config, node_name)

        if await ctx.cancelled():
            raise TaskCancelled(f"节点 {node_name} 启动前检测到取消信号")

        await _emit(
            ctx,
            "node_start",
            {
                "node": node_name,
                "stage_version": _stage_version(state, node_name),
                "batch_id": None,
            },
        )

        t0 = time.perf_counter()
        try:
            result = await node_fn(ctx, state)
        except (TaskCancelled, GraphInterrupt):
            raise
        except AppError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(
                "node unhandled error wrapped to INTERNAL",
                extra={"node": node_name, "trace_id": current_trace_id()},
            )
            raise AppError(
                f"节点 {node_name} 内部错误: {e}",
                details={"node": node_name, "trace_id": current_trace_id()},
            ) from e

        latency_ms = int((time.perf_counter() - t0) * 1000)
        await _emit(ctx, "node_end", {"node": node_name, "latency_ms": latency_ms})

        if not isinstance(result, dict):
            raise AppError(
                f"节点 {node_name} 返回值必须是状态增量 dict，得到 {type(result).__name__}",
                details={"node": node_name},
            )
        return result

    _wrapped.__name__ = f"wrapped_{node_name}"
    _wrapped.__doc__ = f"LangGraph wrapper of {getattr(node_fn, '__name__', node_name)}"
    return _wrapped


def _ctx_from_config(config: dict | None, node_name: str):
    configurable = (config or {}).get("configurable") or {}
    ctx = configurable.get(CTX_KEY)
    if ctx is None:
        raise AppError(
            f"节点 {node_name} 缺少 TaskContext：ainvoke config.configurable.ctx 未注入",
            details={"node": node_name},
        )
    return ctx


def _stage_version(state: TaskState, node_name: str) -> int | None:
    versions = (state or {}).get("current_stage_version") or {}
    val = versions.get(node_name)
    return val if isinstance(val, int) else None


async def _emit(ctx: Any, type_: str, payload: dict[str, Any]) -> None:
    emit = getattr(ctx, "emit", None)
    if emit is None:
        return
    await emit(type_, payload)
