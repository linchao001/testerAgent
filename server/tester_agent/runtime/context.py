"""运行时上下文（dd §6.1）：AppContext 进程组合根 / TaskContext 单次图运行依赖包。

- AppContext：FastAPI lifespan 构造的进程内唯一组合根；WP-12 先落检索管线
  消费面，build_graph 在 WP-15 落地，EventBus（WP-21）与 GraphRegistry/
  TaskRegistry/Runner（WP-22）已接线（完整启停序列在 WP-23）；
- TaskContext：Runner 在 run 启动时构造、wrap() 注入每个节点；检索管线
  （WP-12）仅消费其检索面字段（task/run_id/files/reader/snapshot_level/mirror）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..adapters.llm import LLMClient
from ..adapters.reme import IndexMirror, ReMeReader, ReMeReaderFactory
from ..store.db import Database
from ..store.models import (
    ArtifactDAO,
    ConfigDAO,
    EventDAO,
    MessageDAO,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
    TraceDAO,
)
from ..store.workspace_files import FileStore

if TYPE_CHECKING:
    from ..graph.registry import GraphRegistry
    from ..graph.retrieval.cache import RetrievalCache
    from ..runtime.runner import TaskRegistry
    from .bus import EventBus


def _default_retrieval_cache() -> "RetrievalCache":
    # 惰性导入：runtime.context 可先于 graph.retrieval 包被导入，避免模块导入环
    from ..graph.retrieval.cache import RetrievalCache

    return RetrievalCache()


@dataclass
class AppContext:
    """进程内唯一组合根（dd §6.1）。

    与冻结字段的偏离（交接单登记）：WP-15 已交付 build_graph 工厂，
    GraphRegistry（WP-22）是其薄封装并持有生产图；EventBus（WP-21）与
    TaskRegistry/Runner（WP-22）已接线；``retrieval_cache`` 为 §8.5 进程级
    LRU 的组合根接线点（dd §6.1 字段表未列；默认工厂构造，进程内唯一，
    调用方也可显式注入做隔离测试）。
    """

    db: Database
    file_store: FileStore
    llm: LLMClient
    reme_factory: ReMeReaderFactory
    config: ConfigDAO
    graphs: "GraphRegistry | None" = None  # WP-22：生产图注册表
    bus: "EventBus | None" = None  # WP-21
    registry: "TaskRegistry | None" = None  # WP-22：任务运行锁注册表
    retrieval_cache: "RetrievalCache" = field(default_factory=_default_retrieval_cache)


@dataclass
class DAOs:
    """节点侧 DAO 组（WP-16 起经 ``ctx.daos`` 消费；Runner WP-22 构造注入）。

    按需扩展：后续 WP 消费 artifact/testcase 等 DAO 时在此追加字段，
    保持节点只经本组访问 DAO（不自行 new，避免绕过 workspace 归属）。

    ``artifact`` 为 WP-17 增量字段：早于 WP-17 的测试夹具未注入时为 None，
    link_identify 等产物节点显式校验并报清晰错误（Runner WP-22 全量注入）。
    """

    task: TaskDAO
    message: MessageDAO
    artifact: ArtifactDAO | None = None
    testcase: "TestcaseDAO | None" = None
    trace: "TraceDAO | None" = None
    event: "EventDAO | None" = None  # WP-21：EventBus 落库与 SSE 回放数据源


@dataclass
class TaskContext:
    """单次图运行内的依赖包，注入每个节点（dd §6.1）。

    与冻结字段的偏离：``daos``/``emit`` 随 WP-15/WP-21 落地（默认 None，
    字段顺序因此后置）；``mirror`` 为 §8.4 IndexMirror 的持有点
    （WP-08 交接单"管线方持有"，dd §6.1 字段表未列）；``agent_config``
    为 §13.2 agent.config 的解析结果（ambiguity_check/prompts_dir 等，
    Runner WP-22 从绑定智能体读出注入；dd 字段表未列，WP-16 起节点消费）。
    ``snapshot_level`` 三档：off / meta / full。
    """

    app: AppContext
    task: TaskRow
    run_id: str
    files: FileStore
    reader: ReMeReader
    snapshot_level: str
    mirror: IndexMirror
    agent_config: dict = field(default_factory=dict)
    daos: "DAOs | None" = None  # DAOs 组 — WP-16 起节点经此访问 DAO；Runner WP-22 构造
    emit: Any = None  # Emitter (type_, payload) -> Awaitable — WP-21 接线；None 时 wrap 静默
    # §6.4③ 取消信号：Runner 在 run 启动时置一个 asyncio.Event，cancel API /
    # 心跳 0 行更新 / 优雅关闭置位；wrap() 与 run_in_batches 在边界调 cancelled()。
    cancel_event: asyncio.Event | None = None

    async def cancelled(self) -> bool:
        """批次/节点边界的取消检查（dd §6.4③）；为真时调用方抛 TaskCancelled。

        v1 仅读内存事件（Runner WP-22 负责把 DB cancel_requested 与心跳判活
        结果汇总到事件）；事件载体缺省（None）视为不可取消（单测/eval 场景）。
        """
        return self.cancel_event is not None and self.cancel_event.is_set()
