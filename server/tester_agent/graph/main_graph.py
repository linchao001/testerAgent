"""主图装配：生产路径仅为 Plan-Execute 控制环。

阶段能力（intake / link_identify / …）由 ``execute_step`` 经
``invoke_capability`` 调度，不再作为 StateGraph 拓扑节点。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from .control.graph import build_control_graph
from .wrap import NodeFn

# 阶段名常量仍导出，供 DAO/产物 stage 字段与能力映射使用
from .constants import (
    STAGE_CASE_GENERATE,
    STAGE_COVERAGE_CHECK,
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)

STAGE_NODES: tuple[str, ...] = (
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
    STAGE_CASE_GENERATE,
    STAGE_COVERAGE_CHECK,
)


def build_graph(
    checkpointer=None,
    *,
    caps: Mapping[Any, NodeFn] | None = None,
) -> CompiledStateGraph:
    """编译生产主图（控制环）。

    ``caps``：可选 ``PlanStepKind → 节点函数`` 覆盖（测试注入假能力）；
    生产不传，走 ``default_nodes()``。
    """
    return build_control_graph(checkpointer, caps=caps)
