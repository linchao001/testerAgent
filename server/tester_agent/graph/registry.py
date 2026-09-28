"""图注册表（dd §6.1 GraphRegistry / §6.5 启动序列第 3 步）。

生产图为 Plan-Execute 控制环；遗留五阶段节点仍导出供能力包装与回退
过渡。Runner 经 ``app.graphs.get("case_designer")`` 取已编译图。
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
from .control.graph import build_control_graph
from .main_graph import build_legacy_stage_graph
from .nodes import (
    case_generate_node,
    coverage_check_node,
    intake_node,
    link_identify_node,
    point_write_node,
)
from .wrap import NodeFn

CASE_DESIGNER = "case_designer"

#: 遗留五节点映射（能力包装 / 遗留图测）
PRODUCTION_NODES: dict[str, NodeFn] = {
    STAGE_INTAKE: intake_node,
    STAGE_LINK_IDENTIFY: link_identify_node,
    STAGE_POINT_WRITE: point_write_node,
    STAGE_CASE_GENERATE: case_generate_node,
    STAGE_COVERAGE_CHECK: coverage_check_node,
}

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
        self._conn: Any = None

    @classmethod
    async def create_production(
        cls,
        checkpoint_db_path: str | Path,
        *,
        nodes: Mapping[str, NodeFn] | None = None,
        legacy: bool = False,
    ) -> "GraphRegistry":
        """编译生产主图：默认控制环；``legacy=True`` 或注入 ``nodes`` 时用五阶段图。"""
        conn = await aiosqlite.connect(
            str(checkpoint_db_path), check_same_thread=False
        )
        saver = AsyncSqliteSaver(conn)
        await saver.setup()

        registry = cls()
        registry._conn = conn
        if legacy or nodes is not None:
            registry._graphs[CASE_DESIGNER] = build_legacy_stage_graph(
                saver, nodes=dict(nodes or PRODUCTION_NODES)
            )
        else:
            registry._graphs[CASE_DESIGNER] = build_control_graph(saver)
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
        """回退派生 thread：注入入口 state 并从目标阶段起跑（遗留图）。

        控制环回退将在后续任务改为按 AgentPlan 入口；本期保留遗留 fork API。
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
