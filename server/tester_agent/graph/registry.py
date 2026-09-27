"""图注册表（dd §6.1 GraphRegistry / §6.5 启动序列第 3 步）。

GraphRegistry 是 :func:`tester_agent.graph.main_graph.build_graph` 的薄封装：
lifespan 启动时编译主图（checkpointer 指向 checkpoints.db，生产五节点全部
注册），Runner 经 ``app.graphs.get("case_designer")`` 取已编译图。

生产图名为常量 ``CASE_DESIGNER``。拓扑/节点单测可直接用 build_graph，
不经本注册表。

WP-24：新增 :meth:`GraphRegistry.start_run_from_stage` ——回退协议（§11.2）
派生新 thread 并从目标阶段入口起跑，复用 SP-2 验证的 fork 配方
（``aupdate_state(cfg, entry_state, as_node=前驱)``）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .constants import (
    STAGE_CASE_GENERATE,
    STAGE_COVERAGE_CHECK,
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from .main_graph import build_graph
from .nodes import (
    case_generate_node,
    coverage_check_node,
    intake_node,
    link_identify_node,
    point_write_node,
)
from .wrap import NodeFn

CASE_DESIGNER = "case_designer"

#: 生产五节点映射（阶段常量 -> WP-16~20 交付的节点函数）
PRODUCTION_NODES: dict[str, NodeFn] = {
    STAGE_INTAKE: intake_node,
    STAGE_LINK_IDENTIFY: link_identify_node,
    STAGE_POINT_WRITE: point_write_node,
    STAGE_CASE_GENERATE: case_generate_node,
    STAGE_COVERAGE_CHECK: coverage_check_node,
}

#: 阶段前驱映射（回退 fork 时 as_node 取前驱，使目标阶段成为下一个执行节点）
#: SP-2 验证：要重跑某阶段必须 as_node 取其前驱节点。
_STAGE_PREDECESSOR: dict[str, str] = {
    STAGE_LINK_IDENTIFY: STAGE_INTAKE,
    STAGE_POINT_WRITE: STAGE_LINK_IDENTIFY,
    STAGE_CASE_GENERATE: STAGE_POINT_WRITE,
    STAGE_COVERAGE_CHECK: STAGE_CASE_GENERATE,
}


class GraphRegistry:
    """已编译图的进程内持有点（dd §6.1 ``graphs``）。"""

    def __init__(self) -> None:
        self._graphs: dict[str, Any] = {}
        self._conn: Any = None  # checkpointer 的 aiosqlite 连接（close 时用）

    @classmethod
    async def create_production(
        cls,
        checkpoint_db_path: str | Path,
        *,
        nodes: Mapping[str, NodeFn] | None = None,
    ) -> "GraphRegistry":
        """编译生产主图：AsyncSqliteSaver 指向 ``checkpoint_db_path``。

        :param nodes: 覆盖默认生产节点（测试注入假节点用）；键必须属于
            build_graph 认可的阶段名。
        """
        conn = await aiosqlite.connect(
            str(checkpoint_db_path), check_same_thread=False
        )
        saver = AsyncSqliteSaver(conn)
        await saver.setup()

        registry = cls()
        registry._conn = conn
        registry._graphs[CASE_DESIGNER] = build_graph(
            saver, nodes=dict(nodes or PRODUCTION_NODES)
        )
        return registry

    @classmethod
    def from_graph(cls, name: str, graph: Any) -> "GraphRegistry":
        """用已编译图直接构造（测试/无 checkpointer 场景）。"""
        registry = cls()
        registry._graphs[name] = graph
        return registry

    def get(self, name: str) -> Any:
        return self._graphs[name]

    async def start_run_from_stage(
        self,
        new_thread: str,
        *,
        stage: str,
        entry_state: dict,
    ) -> None:
        """回退派生 thread：注入入口 state 并从目标阶段起跑（dd §11.2）。

        SP-2 验证配方：新 thread_id + ``aupdate_state(cfg, entry_state,
        as_node=前驱)`` → ``next=目标阶段``，重跑经真实边重新到达 gate 时
        interrupt_before 照常生效。

        :param new_thread: 派生线程 id（``{base}::run{n}``）
        :param stage: 回退目标阶段
        :param entry_state: 入口 state（含 task_id/graph_run_id/workspace_id
            及全部上游阶段产物；目标阶段的修订产物已在 DB 落账，节点经
            get_active 回放）
        """
        graph = self.get(CASE_DESIGNER)
        predecessor = _STAGE_PREDECESSOR.get(stage)
        if predecessor is None:
            raise ValueError(f"不支持回退到阶段 {stage}（无前驱）")
        cfg = {"configurable": {"thread_id": new_thread}}
        await graph.aupdate_state(cfg, entry_state, as_node=predecessor)

    async def aclose(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
