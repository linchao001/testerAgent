"""主图状态定义（dd §7.1）。

TaskState 是 LangGraph 通道状态：
- 节点统一出口返回**状态增量 dict**（dd §7.2）：plan 类字段整体替换；
  batch_cursor 按 node key merge（批次执行器 WP-18 用）；
- 全部字段可选（total=False）：中断恢复/modify 注入入口 state 时允许部分键，
  节点读 state 时自行按缺省值处理。
"""

from __future__ import annotations

from typing import TypedDict


class TaskState(TypedDict, total=False):
    """主图全量状态（控制面 Plan-Execute；兼容旧阶段字段）。"""

    task_id: str
    graph_run_id: str
    workspace_id: str
    # intake 产物：list[ClauseRef 序列化 dict]（WP-16）
    clauses: list[dict]
    # 各阶段产物（stage_artifact.payload 镜像；confirm modify 时整体替换）
    link_plan: dict | None
    point_plan: dict | None
    case_batch: dict | None
    coverage: dict | None
    # 当前挂起的澄清问题（list[{"id","question","options"}]，WP-16/17）
    clarification_questions: list[dict]
    # 各阶段当前版本号：node -> stage_version（node_start 事件取此值）
    current_stage_version: dict[str, int]
    # 批次游标：node -> {idempotency_nonce, next_index, ...}（WP-18）
    batch_cursor: dict[str, dict]
    # ---- Plan-Execute 控制面 ----
    agent_plan: dict
    plan_cursor: str | None
    artifacts: dict
    subtask: dict | None
    reflection_log: list[dict]
    human_gates: dict
    reflect_counts: dict
    _reflect_decision: str
