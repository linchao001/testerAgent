"""主图装配。

生产路径：Plan-Execute 控制环（:func:`build_control_graph`）。
遗留五阶段拓扑保留为 :func:`build_legacy_stage_graph`，供既有节点/图测
与能力包装过渡期使用。
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
from .control.graph import build_control_graph
from .state import TaskState
from .wrap import NodeFn, NodeNotImplemented, gate_node, wrap

# 遗留主图业务节点（能力包装仍按阶段名引用）
STAGE_NODES: tuple[str, ...] = (
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
    STAGE_CASE_GENERATE,
    STAGE_COVERAGE_CHECK,
)

GATE_NODES: tuple[str, str] = (GATE_CP1, GATE_CP2)


def _stub_node(stage: str) -> NodeFn:
    """未实现阶段的占位节点。"""

    async def _node(ctx, state) -> dict:  # noqa: ANN001
        raise NodeNotImplemented(f"节点 {stage} 尚未实现（待对应 WP 注册）")

    _node.__name__ = f"{stage}_node"
    return _node


def build_graph(
    checkpointer=None,
    *,
    nodes: Mapping[str, NodeFn] | None = None,
) -> CompiledStateGraph:
    """编译生产主图（控制环）。

    若传入 ``nodes``（遗留阶段注入），回退到 :func:`build_legacy_stage_graph`
    以兼容现有单测；生产 Registry 不传 ``nodes``。
    """
    if nodes is not None:
        return build_legacy_stage_graph(checkpointer, nodes=nodes)
    return build_control_graph(checkpointer)


def build_legacy_stage_graph(
    checkpointer=None,
    *,
    nodes: Mapping[str, NodeFn] | None = None,
) -> CompiledStateGraph:
    """编译遗留五阶段 + CP gate 主图（过渡期）。"""
    overrides = dict(nodes or {})
    unknown = sorted(set(overrides) - set(STAGE_NODES))
    if unknown:
        raise TypeError(f"build_legacy_stage_graph 收到未知阶段节点名: {unknown}")

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

    compile_kwargs: dict = {"interrupt_before": list(GATE_NODES)}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer
    return g.compile(**compile_kwargs)
