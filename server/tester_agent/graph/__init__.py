"""LangGraph 主图包（dd §7）。

- :mod:`tester_agent.graph.constants`  阶段常量与检索预设（§1.3 §8.1）
- :mod:`tester_agent.graph.state`      TaskState（§7.1）
- :mod:`tester_agent.graph.wrap`       wrap() 节点包装（能力/单测）
- :mod:`tester_agent.graph.main_graph` build_graph → 控制环
- :mod:`tester_agent.graph.control`    Plan-Execute 控制环
- :mod:`tester_agent.graph.retrieval`  检索管线（§8）
"""

from .constants import (
    STAGE_CASE_GENERATE,
    STAGE_COVERAGE_CHECK,
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_NODES,
    STAGE_POINT_WRITE,
)
from .main_graph import build_graph
from .state import TaskState
from .wrap import CTX_KEY, NodeFn, wrap

__all__ = [
    "CTX_KEY",
    "NodeFn",
    "STAGE_CASE_GENERATE",
    "STAGE_COVERAGE_CHECK",
    "STAGE_INTAKE",
    "STAGE_LINK_IDENTIFY",
    "STAGE_NODES",
    "STAGE_POINT_WRITE",
    "TaskState",
    "build_graph",
    "wrap",
]
