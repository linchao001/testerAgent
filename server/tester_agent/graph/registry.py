"""图注册表（dd §6.1 GraphRegistry / §6.5 启动序列第 3 步）。

生产图为 Plan-Execute 控制环。Runner 经 ``app.graphs.get("case_designer")``
取已编译图。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .constants import (
    STAGE_CASE_GENERATE,
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from .control.graph import build_control_graph
from .nodes import (
    case_generate_node,
    intake_node,
    link_identify_node,
    point_write_node,
)
from .wrap import NodeFn

CASE_DESIGNER = "case_designer"

#: 阶段名 → 节点函数（能力包装 / 测试 caps 组装）
PRODUCTION_NODES: dict[str, NodeFn] = {
    STAGE_INTAKE: intake_node,
    STAGE_LINK_IDENTIFY: link_identify_node,
    STAGE_POINT_WRITE: point_write_node,
    STAGE_CASE_GENERATE: case_generate_node,
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
        caps: Mapping[Any, NodeFn] | None = None,
    ) -> "GraphRegistry":
        """编译生产主图（控制环）；``caps`` 仅测试/工具覆盖能力节点。"""
        conn = await aiosqlite.connect(
            str(checkpoint_db_path), check_same_thread=False
        )
        saver = AsyncSqliteSaver(conn)
        await saver.setup()

        registry = cls()
        registry._conn = conn
        registry._graphs[CASE_DESIGNER] = build_control_graph(saver, caps=caps)
        return registry

    @classmethod
    def from_graph(cls, name: str, graph: Any) -> "GraphRegistry":
        """用已编译图直接构造（测试/无 checkpointer 场景）。"""
        registry = cls()
        registry._graphs[name] = graph
        return registry

    def get(self, name: str) -> Any:
        return self._graphs[name]

    async def start_run_from_plan(
        self,
        new_thread: str,
        *,
        entry_state: dict,
    ) -> None:
        """回退派生 thread：注入入口 state（含 AgentPlan），从 plan 之后续跑。

        ``as_node="plan"`` → next=dispatch，由 dispatch 选取第一个 pending 步。
        """
        graph = self.get(CASE_DESIGNER)
        cfg = {"configurable": {"thread_id": new_thread}}
        await graph.aupdate_state(cfg, entry_state, as_node="plan")

    async def aclose(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
