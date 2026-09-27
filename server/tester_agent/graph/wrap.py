"""节点统一包装与 gate（dd §7.1 wrap() 职责）。

``wrap(node_fn)`` 把"业务节点函数 (ctx, state) -> 状态增量 dict"包装成
LangGraph 节点函数 ``(state, config) -> dict``，统一承担：

1. **TaskContext 注入**：Runner 把 :class:`TaskContext` 放在
   ``config["configurable"]["ctx"]``（dd §6.4 ainvoke config 透传），
   业务节点不直接接触 RunnableConfig；
2. **取消检查**：节点执行前调 ``ctx.cancelled()``，为真抛
   :class:`TaskCancelled`（批次边界另由 run_in_batches WP-18 检查，dd §6.4③）；
3. **事件发射**：成功进入发 ``node_start``（stage_version 从 state 读），
   正常返回发 ``node_end``（latency_ms）；中断/取消/异常不发 node_end
   （终态事件由 Runner §6.4 统一收口）；
4. **异常映射**（dd §17.2 边界）：``TaskCancelled`` 与 LangGraph 中断
   原样穿透；:class:`AppError` 子类原样上抛（保留 code/retryable）；
   其余未预期异常兜底包成 INTERNAL（AppError 基类默认 code=INTERNAL/500），
   打 run 级 trace_id 异常日志。

``gate_node(checkpoint_id)`` 是无 IO 纯透传节点（dd §7.1）：不包 wrap、
不发事件、不写 state，只作 ``interrupt_before`` 的静态锚点——CP 的用户
放行不经过图节点逻辑（confirm API 直接写 artifact，§7.6）。
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

# 业务节点函数契约（WP-16~20 的节点实现遵循此签名）
NodeFn = Callable[..., Awaitable[dict[str, Any]]]

# RunnableConfig 中 TaskContext 的存放键（config["configurable"][_CTX_KEY]）
CTX_KEY = "ctx"


class NodeNotImplemented(RuntimeError):
    """主图装配了未实现的占位节点（WP-16~20 逐步替换）。

    非 AppError：被 wrap 兜底为 INTERNAL——在节点被真正实现前触发即编程/
    编排错误，不应静默。
    """


def _node_name_of(node_fn: Callable[..., Any], explicit: str | None) -> str:
    if explicit:
        return explicit
    name = getattr(node_fn, "__name__", "")
    return name[: -len("_node")] if name.endswith("_node") else name


def wrap(node_fn: NodeFn, *, name: str | None = None) -> Callable[..., Awaitable[dict]]:
    """包装业务节点为 LangGraph 节点（dd §7.1）。

    节点名默认取函数名去掉 ``_node`` 后缀（``intake_node`` -> ``intake``），
    与 §1.3 阶段常量 / 落库字符串一致；占位节点等无法靠函数名表达的场景
    用 ``name=`` 显式指定。
    """
    node_name = _node_name_of(node_fn, name)

    async def _wrapped(
        state: TaskState, config: RunnableConfig | None = None
    ) -> dict[str, Any]:
        # 不能用 functools.wraps：LangGraph 经 __wrapped__/签名探测决定注入，
        # 包装层必须保持自有 (state, config: RunnableConfig) 签名（见模块头注）。
        ctx = _ctx_from_config(config, node_name)

        # ① 取消前置检查（节点粒度边界）
        if await ctx.cancelled():
            raise TaskCancelled(f"节点 {node_name} 启动前检测到取消信号")

        # ② node_start（中断恢复重跑同一节点时也会再次发，Runner/事件侧幂等）
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
            # 内部控制流：取消 / 人工中断（interrupt()），不映射、不发 node_end
            raise
        except AppError:
            # 节点只允许抛 AppError 子类（dd §17.2）：原样上抛，code/retryable 保留
            raise
        except Exception as e:  # noqa: BLE001 —— wrap 是节点层唯一兜底边界
            logger.exception(
                "node unhandled error wrapped to INTERNAL",
                extra={"node": node_name, "trace_id": current_trace_id()},
            )
            raise AppError(
                f"节点 {node_name} 内部错误: {e}",
                details={"node": node_name, "trace_id": current_trace_id()},
            ) from e

        # ③ 正常出口：node_end + 透传状态增量
        latency_ms = int((time.perf_counter() - t0) * 1000)
        await _emit(ctx, "node_end", {"node": node_name, "latency_ms": latency_ms})

        if not isinstance(result, dict):
            # 节点契约违反属于编程错误：就地显形而不是让 LangGraph 报晦涩类型错
            raise AppError(
                f"节点 {node_name} 返回值必须是状态增量 dict，得到 {type(result).__name__}",
                details={"node": node_name},
            )
        return result

    # 手工保留可辨识名（不用 functools.wraps，理由见函数体内注释）
    _wrapped.__name__ = f"wrapped_{node_name}"
    _wrapped.__doc__ = f"LangGraph wrapper of {getattr(node_fn, '__name__', node_name)}"
    return _wrapped


def gate_node(checkpoint_id: str) -> Callable[..., Awaitable[dict]]:
    """CP gate：纯透传、无 IO、无 wrap（dd §7.1）。

    checkpoint_id 仅用于日志可辨识（CHECKPOINT_1/CHECKPOINT_2）；放行动作
    （confirm/modify）在 API 层完成后由 Runner ``ainvoke(None)`` 越过本节点。
    """

    async def _gate(state: TaskState) -> dict[str, Any]:
        logger.debug("gate pass-through", extra={"checkpoint": checkpoint_id})
        return {}

    _gate.__name__ = f"gate_{checkpoint_id}"
    return _gate


# ---------- 内部辅助 ----------


def _ctx_from_config(config: dict | None, node_name: str):
    """从 RunnableConfig 取 Runner 注入的 TaskContext（缺失即编排错误）。"""
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
    """经 TaskContext.emitter 发事件；WP-21 前 emit 为 None 时静默（单测/eval）。"""
    emit = getattr(ctx, "emit", None)
    if emit is None:
        return
    await emit(type_, payload)
