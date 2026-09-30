"""FastAPI 组合根。

接线状态：EventBus/SSE、GraphRegistry/TaskRegistry/Runner、Reaper、
API A–D、ExportService、Reconciler、IdempotencyStore、Maintenance；
**嵌入式 ReMe**：``WorkspaceMemoryPool`` + ``register_sdk_builder`` +
``PoolRoutingWriter``（``app.state.kb_writer``；图路径仍不可达 Writer）。
启动时对全部未软删工作区 ``start_all`` 预热（对齐 QwenPaw）；单工作区失败
只记日志不阻断进程。静态托管 ``web/dist``。

lifespan 严格按 dd §6.5 六步启动序列，任一步失败阻断启动；关闭先走
Reaper.graceful_shutdown（取消在飞任务等 30s）再 ``memory_pool.shutdown_all``
与 DB 释放。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from . import __version__
from .adapters.llm import OpenAICompatLLMClient
from .adapters.reme import ReMeReaderFactory
from .adapters.reme_sdk import PoolRoutingWriter, register_sdk_builder
from .api.agents import router as agents_router
from .api.cases import router as cases_router
from .api.config import router as config_router
from .api.context import router as context_router
from .api.conversations import router as conversations_router
from .api.debug import router as debug_router
from .api.error_handling import install_error_handling
from .api.events import make_events_router
from .api.kb import router as kb_router
from .api.maintenance import router as maintenance_router
from .api.tasks import router as tasks_router
from .api.workspaces import router as workspaces_router
from .errors import LLMBadRequest
from .graph.constants import HEARTBEAT_INTERVAL_SEC, HEARTBEAT_STALE_SEC
from .graph.registry import GraphRegistry
from .logging_config import get_logger, setup_logging
from .memory.pool import WorkspaceMemoryPool
from .runtime.bus import EventBus
from .runtime.context import AppContext
from .runtime.idempotency import IdempotencyStore
from .runtime.maintenance import Maintenance
from .runtime.reaper import Reaper
from .runtime.reconciler import Reconciler
from .runtime.runner import Runner, TaskRegistry
from .settings import Settings, validate_single_worker
from .store.db import Database, run_migrations
from .store.models import ConfigDAO, EventDAO, TaskDAO, WorkspaceDAO
from .store.paths import resolve_workspace_root
from .store.workspace_files import FileStore

logger = get_logger(__name__)

_WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    validate_single_worker(settings)  # 多 worker 配置 → 拒绝启动（dd §13.1）
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # ---- §6.5 启动序列（任一步失败阻断启动）----

        # 1. 打开/迁移 app.db（WAL）；初始化 FileStore（目录校验）
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        migrated = await loop.run_in_executor(
            None, run_migrations, settings.app_db_path
        )
        logger.info(
            "startup",
            extra={"data_dir": str(settings.data_dir), "new_migrations": migrated},
        )
        db = Database(settings.app_db_path)
        _app.state.db = db
        _app.state.settings = settings
        # FileStore root = data_dir；任务路径 = {data}/workspaces/{id}/{task}/
        # （或 workspace.root_dir 覆盖）
        file_store = FileStore(settings.data_dir)
        _app.state.file_store = file_store

        # 2. 加载 config；EventBus、ReMeFactory 就位；预热全部工作区嵌入 ReMe
        bus = EventBus(EventDAO(db))
        _app.state.bus = bus
        config_dao = ConfigDAO(db)
        cfg_row = await config_dao.get()
        runtime_cfg = cfg_row.runtime_dict()
        model_cfg = cfg_row.model_dict()
        stale_sec = int(runtime_cfg.get("heartbeat_stale_sec", HEARTBEAT_STALE_SEC))
        reme_factory = ReMeReaderFactory()
        memory_pool = WorkspaceMemoryPool(settings.data_dir)
        register_sdk_builder(reme_factory, memory_pool)
        _app.state.reme_factory = reme_factory
        _app.state.memory_pool = memory_pool
        # 写实现仅 L2 提案确认端点可达（dd §9.2 / PRD 7）
        _app.state.kb_writer = PoolRoutingWriter(db, memory_pool)
        ws_rows = await WorkspaceDAO(db).list_all()
        for w in ws_rows:
            root = resolve_workspace_root(
                settings.data_dir, w.id, w.root_dir or ""
            )
            if w.root_dir:
                file_store.register_workspace_root(w.id, root)
                memory_pool.register_workspace_root(w.id, root)
        warm = await memory_pool.start_all(
            ((w.id, w.kb_config_obj()) for w in ws_rows),
            model_config=model_cfg,
        )
        if warm:
            started_n = sum(1 for ok in warm.values() if ok)
            logger.info(
                "reme_warm_start",
                extra={"started": started_n, "total": len(warm)},
            )
        # WP-29：进程内幂等键存储（dd §6.6 一期内存 TTL）
        idem_store = IdempotencyStore()
        _app.state.idem_store = idem_store

        # 3. GraphRegistry 编译主图（checkpointer → checkpoints.db）
        graphs = await GraphRegistry.create_production(settings.checkpoints_db_path)
        task_registry = TaskRegistry(TaskDAO(db), stale_sec=stale_sec)
        _app.state.graphs = graphs
        _app.state.registry = task_registry

        reaper = Reaper(TaskDAO(db), stale_sec=stale_sec)
        runner: Runner | None = None

        # 2（续）：LLM 配置有效才构造 AppContext/Runner；model_config={} 时
        # 容忍不阻断启动（SSE/health 可用），任务运行待配置模型后重启。
        try:
            llm = OpenAICompatLLMClient.from_configs(
                cfg_row.model_dict(), runtime_cfg
            )
        except LLMBadRequest:
            logger.warning(
                "LLM 未配置：任务运行暂不可用，请先在设置中配置模型后重启"
            )
        else:
            app_ctx = AppContext(
                db=db,
                file_store=file_store,
                llm=llm,
                reme_factory=reme_factory,
                config=config_dao,
                graphs=graphs,
                bus=bus,
                registry=task_registry,
            )
            _app.state.app_ctx = app_ctx  # WP-26：回退/regenerate 入口消费
            hb_interval = float(
                runtime_cfg.get("heartbeat_interval_sec", HEARTBEAT_INTERVAL_SEC)
            )
            runner = Runner(app_ctx, heartbeat_interval=hb_interval)
            _app.state.runner = runner

        # 4. Reaper：陈旧 running/cancelling → failed(INTERRUPTED_BY_RESTART)
        reaped = await reaper.reap_on_startup()
        if reaped:
            _app.state.last_reaped = reaped

        # 5. maintenance.lazy_purge：清理超保留期 event/快照/提案/.tmp/导出
        #    + 过期幂等键；WP-29 启动全量对账（DB↔文件一致性）
        purged = await Maintenance(
            db, config_dao, file_store=file_store, idempotency_store=idem_store,
        ).lazy_purge()
        if any(purged.values()):
            _app.state.last_purged = purged
        await Reconciler(db, file_store).reconcile_all()

        # 6. 开始服务（路由在 app 创建时已挂载）
        try:
            yield
        finally:
            # ---- §6.5 关闭序列：取消在飞任务等 30s → 图连接 → DB ----
            await reaper.graceful_shutdown(task_registry, timeout_sec=30.0)
            await memory_pool.shutdown_all()
            await graphs.aclose()
            db.close()
            logger.info("shutdown")

    app = FastAPI(title="TesterAgent", version=__version__, lifespan=lifespan)

    install_error_handling(app)  # trace_id 中间件 + 错误信封（dd §17 §10.1）

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    # WP-21：SSE 事件流（dd §6.2 §10.4）
    app.include_router(make_events_router())

    # WP-25：API-A（tech-design §5.1 §5.2 §5.6）
    app.include_router(workspaces_router)
    app.include_router(agents_router)
    app.include_router(conversations_router)
    app.include_router(config_router)

    # WP-26：API-B（tech-design §5.2 §5.3 / dd §10.2 §10.3 §7.6）
    app.include_router(tasks_router)

    # WP-27：API-C（tech-design §5.4 / dd §10.2 §10.3④⑤ §9.4 §7.6）
    app.include_router(cases_router)

    # WP-28：API-D（tech-design §5.5 §5.6 / dd §10.2 §8.7 §11.4 §10.3⑥）
    app.include_router(debug_router)
    app.include_router(kb_router)

    # WP-32：上下文调试视图 / evictions / playground（context-management §8）
    app.include_router(context_router)

    # WP-29：对账手动触发端点（dd §11.3）
    app.include_router(maintenance_router)

    # web/dist 存在时挂载静态资源（WP-F0 产出后生效；dd §19.2）
    if (_WEB_DIST / "index.html").is_file():  # pragma: no cover - 前端未产出前不触发
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web")

    return app


app = create_app()
