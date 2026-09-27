"""Reaper：启动改判 + 优雅关闭（dd §6.5）。

职责两件：

1. :meth:`Reaper.reap_on_startup`：进程启动时把上次异常退出遗留的
   running/cancelling 任务（心跳为空或早于 ``stale_sec``，默认 120s）改判
   failed，error_info = ``{code:'INTERRUPTED_BY_RESTART', retryable:true}``；
   waiting_confirm / waiting_input 不动（用户态挂起，重启后仍可续跑）。
   用户点重试即走 failed→running 从批次游标恢复（dd §6.3 R3）。
2. :meth:`Reaper.graceful_shutdown`：关闭时向 registry 中全部在飞 run 任务
   发取消信号并等待最多 ``timeout_sec``（默认 30s）；节点在批次边界或当前
   LLM 调用结束处退出。**不强制 kill 中途写盘**——原子写协议保证现场可恢复，
   未在时限内退出的任务也随进程结束，重启时由 reap_on_startup 兜底改判。

SIGTERM/SIGINT 由 uvicorn 自身处理并触发 FastAPI lifespan 关闭，故本模块
不注册信号处理器；集成点在 main.py lifespan 的 finally。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..errors import AppError
from ..logging_config import get_logger
from ..store.db import iso_ago

if TYPE_CHECKING:
    from ..store.models import TaskDAO, TaskRow
    from .runner import TaskRegistry

logger = get_logger(__name__)

#: Reaper 改判错误码（dd §14：HTTP —，retryable true，点重试从批次恢复）
INTERRUPTED_BY_RESTART = "INTERRUPTED_BY_RESTART"


class Reaper:
    """任务生命周期收割器（dd §6.5）。"""

    def __init__(
        self,
        task_dao: "TaskDAO",
        *,
        stale_sec: int = 120,
    ) -> None:
        self._task_dao = task_dao
        self._stale_sec = int(stale_sec)

    async def reap_on_startup(self) -> list[str]:
        """改判陈旧 running/cancelling → failed，返回被改判的 task_id 列表。

        判定口径与 TaskDAO.list_stale_running 一致：
        status IN running/cancelling 且（心跳 NULL 或心跳 < now-stale_sec）。
        """
        cutoff = iso_ago(self._stale_sec)
        stale: list[TaskRow] = await self._task_dao.list_stale_running(cutoff)

        reaped: list[str] = []
        for row in stale:
            error_info = {
                "code": INTERRUPTED_BY_RESTART,
                "message": "服务异常重启，任务被中断",
                "retryable": True,
                "node": None,
                "details": {},
            }
            await self._task_dao.update_status(
                row.id,
                status="failed",
                error_info=error_info,
            )
            reaped.append(row.id)

        if reaped:
            logger.warning(
                "reap_on_startup reclaimed stale tasks",
                extra={"count": len(reaped), "task_ids": reaped},
            )
        return reaped

    async def graceful_shutdown(
        self,
        registry: "TaskRegistry",
        *,
        timeout_sec: float = 30.0,
    ) -> bool:
        """取消全部在飞任务并等待，返回是否全部在时限内退出（dd §6.5 关闭序列）。"""
        tasks = registry.all_tasks()
        if not tasks:
            return True

        logger.info(
            "graceful shutdown: cancelling in-flight tasks",
            extra={"count": len(tasks), "timeout_sec": timeout_sec},
        )
        for t in tasks:
            t.cancel()

        done, pending = await asyncio.wait(tasks, timeout=timeout_sec)
        if pending:
            # 不做强制 kill：超时任务随进程终止，重启后 reap 兜底。
            logger.error(
                "graceful shutdown timeout: tasks did not finish",
                extra={"pending": len(pending)},
            )
            return False

        # Runner._run 对 CancelledError 重新抛出，任务以 CancelledError 结束属正常
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None and not isinstance(exc, AppError):
                logger.warning(
                    "task raised during shutdown",
                    extra={"task": t.get_name(), "error": str(exc)},
                )
        return True
