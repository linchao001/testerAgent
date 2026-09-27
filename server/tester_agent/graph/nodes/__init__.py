"""主图业务节点（dd §7.5 关键节点处理逻辑；WP-16~20 逐阶段交付）。

节点函数契约（WP-15 定）：``(ctx: TaskContext, state: TaskState) -> 状态增量
dict``；由 graph.wrap 包装后注入 build_graph(nodes={stage: node_fn})。
"""

from .intake import ClauseSpan, intake_node, split_clauses
from .link_identify import (
    build_link_intent,
    finalize_link_plan,
    link_identify_node,
    render_knowledge_block,
    render_user_message,
)
from .point_write import (
    build_point_intent,
    finalize_point_plan,
    order_stories_by_link,
    point_write_node,
)
from .case_generate import (
    build_case_intent,
    case_generate_node,
    commit_case_batch,
    finalize_cases,
    generate_case_batch,
)
from .coverage_check import (
    build_coverage_matrix,
    build_virtual_points,
    coverage_check_node,
    count_supp_rounds,
    matrix_summary,
)

__all__ = [
    "ClauseSpan",
    "intake_node",
    "split_clauses",
    "build_link_intent",
    "finalize_link_plan",
    "link_identify_node",
    "render_knowledge_block",
    "render_user_message",
    "build_point_intent",
    "finalize_point_plan",
    "order_stories_by_link",
    "point_write_node",
    "build_case_intent",
    "case_generate_node",
    "commit_case_batch",
    "finalize_cases",
    "generate_case_batch",
    "build_coverage_matrix",
    "build_virtual_points",
    "coverage_check_node",
    "count_supp_rounds",
    "matrix_summary",
]
