"""FastAPI 组合根（WP-01 骨架）。

后续 WP 在此接线：DB 迁移（WP-02）、Runtime lifespan 序列（WP-23）、API 路由（WP-25~28）、
静态托管 web/dist（WP-F0）。本文件只保留骨架与 /healthz。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from . import __version__
from .api.error_handling import install_error_handling
from .logging_config import get_logger, setup_logging
from .settings import Settings, validate_single_worker
from .store.db import run_migrations

logger = get_logger(__name__)

_WEB_DIST = Path(__file__).resolve().parent.parent.parent / "web" / "dist"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    validate_single_worker(settings)  # 多 worker 配置 → 拒绝启动（dd §13.1）
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        # 启动即迁移：缺失或版本低则按序执行，已应用则跳过（dd §3.2）。
        # 完整启停序列（Reaper/优雅关闭）由 WP-23 扩展。
        loop = asyncio.get_running_loop()
        migrated = await loop.run_in_executor(None, run_migrations, settings.app_db_path)
        logger.info(
            "startup",
            extra={"data_dir": str(settings.data_dir), "new_migrations": migrated},
        )
        yield
        logger.info("shutdown")

    app = FastAPI(title="TesterAgent", version=__version__, lifespan=lifespan)

    install_error_handling(app)  # trace_id 中间件 + 错误信封（dd §17 §10.1）

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    # web/dist 存在时挂载静态资源（WP-F0 产出后生效；dd §19.2）
    if (_WEB_DIST / "index.html").is_file():  # pragma: no cover - 前端未产出前不触发
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web")

    return app


app = create_app()
