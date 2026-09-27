"""主图装配（dd §7.1）。

拓扑（§7.1 定稿）：

    START -> intake -> link_identify -> [cp1_gate] -> point_write
          -> [cp2_gate] -> case_generate -> coverage_check -> END

- 五个业务节点经 :func:`wrap` 包装，ctx 由 Runner 经
  ``config.configurable.ctx`` 注入；两个 gate 为纯透传锚点；
- ``interrupt_before`` 静态断在两个 gate：图运行到 CP 自动挂起，
  confirm/modify API 落库后 ``ainvoke(None)`` 放行（§7.6）；
- waiting_input（澄清）不走静态 gate，由节点内部 ``interrupt()``
  函数式挂起，``Command(resume=answers)`` 恢复（SP-2 已验证 GO）；
- 业务节点 WP-16~20 才实现，本包以占位节点装配，图可编译、拓扑可测；
  调用方经 ``nodes=`` 注入已实现节点（生产侧由 GraphRegistry WP-22
  统一注册）。占位节点若被执行，wrap 兜底为 INTERNAL。

checkpointer 注入已构造好的 saver（lifespan 按 SP-2 结论构造：
``aiosqlite.connect(checkpoints.db)`` → ``AsyncSqliteSaver(conn)`` →
``setup()``）；拓扑单测可传 None 编译无 checkpointer 的图。
"""

from __future__ import annotations

from collections.abc import Mapping

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .constants import (
    CHECKPOINT_1,
    CHECKPOINT_2,
    GATE_CP1,
    GATE_CP2,
    STAGE_CASE_GENERATE,
    STAGE_COVERAGE_CHECK,
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from .state import TaskState
from .wrap import NodeFn, NodeNotImplemented, gate_node, wrap

# 主图业务节点（图内顺序即此元组顺序；gate 位置单独在装配时表达）
STAGE_NODES: tuple[str, ...] = (
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
    STAGE_CASE_GENERATE,
    STAGE_COVERAGE_CHECK,
)

# 静态 gate 顺序（interrupt_before；index 与拓扑断点一一对应）
GATE_NODES: tuple[str, str] = (GATE_CP1, GATE_CP2)


def _stub_node(stage: str) -> NodeFn:
    """未实现阶段的占位节点（WP-16~20 经 nodes= 替换）。"""

    async def _node(ctx, state) -> dict:  # noqa: ANN001 —— 契约见 NodeFn
        raise NodeNotImplemented(f"节点 {stage} 尚未实现（待对应 WP 注册）")

    _node.__name__ = f"{stage}_node"
    return _node


def build_graph(
    checkpointer=None,
    *,
    nodes: Mapping[str, NodeFn] | None = None,
) -> CompiledStateGraph:
    """编译主图。

    :param checkpointer: 已构造的 LangGraph checkpointer（生产为
        AsyncSqliteSaver 指向 checkpoints.db）；None 时图无持久化，
        仅可用于拓扑/编译断言（中断恢复不可用）。
    :param nodes: 阶段名 -> 业务节点函数 ``(ctx, state) -> 状态增量``；
        缺省阶段装占位节点。键必须属于 :data:`STAGE_NODES`。
    """
    overrides = dict(nodes or {})
    unknown = sorted(set(overrides) - set(STAGE_NODES))
    if unknown:
        raise TypeError(f"build_graph 收到未知阶段节点名: {unknown}")

    g = StateGraph(TaskState)

    for stage in STAGE_NODES:
        node_fn = overrides.get(stage) or _stub_node(stage)
        g.add_node(stage, wrap(node_fn, name=stage))

    g.add_node(GATE_CP1, gate_node(CHECKPOINT_1))
    g.add_node(GATE_CP2, gate_node(CHECKPOINT_2))

    g.add_edge(START, STAGE_INTAKE)
    g.add_edge(STAGE_INTAKE, STAGE_LINK_IDENTIFY)
    g.add_edge(STAGE_LINK_IDENTIFY, GATE_CP1)
    g.add_edge(GATE_CP1, STAGE_POINT_WRITE)
    g.add_edge(STAGE_POINT_WRITE, GATE_CP2)
    g.add_edge(GATE_CP2, STAGE_CASE_GENERATE)
    g.add_edge(STAGE_CASE_GENERATE, STAGE_COVERAGE_CHECK)
    g.add_edge(STAGE_COVERAGE_CHECK, END)

    compile_kwargs = {"interrupt_before": list(GATE_NODES)}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer
    return g.compile(**compile_kwargs)
