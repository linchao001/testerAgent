"""主图装配：生产路径仅为 Plan-Execute 控制环。

阶段能力（intake / link_identify / …）由 ``execute_step`` 经
``invoke_capability`` 调度，不再作为 StateGraph 拓扑节点。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from .constants import STAGE_NODES
from .control.graph import build_control_graph
from .wrap import NodeFn

__all__ = ["STAGE_NODES", "build_graph"]


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
