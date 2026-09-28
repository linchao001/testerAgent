"""导出服务（WP-27；dd §9.4 / §3.3④ R34 / tech-design §5.4 export 行）。

``ExportService.export_md_zip``：

1. 筛选——默认 ``status=active 且 review_status in (adopted, edited_adopted)``
   （dd §9.4），``case_ids`` 显式指定时按给定集合（仍要求属于本任务、仍过
   同一筛选口径）；
2. hash 校验——打包前逐文件 ``hash_of`` 与 DB ``content_hash`` 对拍，
   文件缺失/hash 不一致项列入 ``skipped`` 不进 zip（R34，不静默打包）；
3. 同步/异步阈值——``≤SYNC_MAX_CASES(200) 且总字节 ≤SYNC_MAX_BYTES(20MB)``
   同步打包返回 download_url；否则建后台 asyncio 任务，立即返回
   ``{status:"running", job_id}``，轮询 ``GET /tasks/{id}/export/{job_id}``；
4. zip 结构（dd §9.4）：``v{stage_version}/{point_id}-{case slug}.md``
   + 根目录 ``INDEX.md``（序号/标题/优先级/评审状态/溯源条款清单表）；
   目录内重名（同 point 多 slug 相同）追加 ``-2/-3`` 后缀；
5. job 登记——进程内 ``_EXPORT_JOBS`` dict（单 worker 模型，dd §6.2 同口径）；
   完成/失败均落 ``task_event``（export_ready / export_failed）供 SSE 可见；
   zip 留在任务目录 exports/ 下，下载走 GET 句柄（FileResponse），
   保留期物理清理属 WP-29。

与 dd 的偏离（交接单登记）：① job 状态为进程内存字典（dd §9.4 一期实现
"后台 asyncio task + 结果落 task_event/临时目录"——事件落库不变，job 结果
路径不落 DB 表，重启后 job 查询 404，zip 文件仍在盘）；
② zip 落任务目录 exports/{job_id}/export.zip 而非系统临时目录（复用
FileStore 路径安全校验，且 WP-29 保留期清理可统一按任务目录扫描）。
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..domain import ReviewStatus
from ..errors import AppError, NotFoundError
from ..logging_config import get_logger
from ..store.models import CaseRow, TaskDAO, TestcaseDAO
from ..store.workspace_files import (
    FileStore,
    _atomic_write,
    slugify,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import AppContext

logger = get_logger(__name__)

# dd §9.4 同步阈值：≤200 条且总字节 ≤20MB
SYNC_MAX_CASES = 200
SYNC_MAX_BYTES = 20 * 1024 * 1024

# 导出筛选口径（dd §9.4）
_EXPORT_REVIEW_STATES = {ReviewStatus.ADOPTED.value, ReviewStatus.EDITED_ADOPTED.value}
_EXPORT_ACTIVE = "active"

_FM_PRIORITY_RE = re.compile(r"(?m)^priority:\s*(\S+)\s*$")


@dataclass
class ExportOut:
    """dd §10.2 ExportOut。"""

    status: str  # "ready" | "running"
    download_url: str | None
    skipped: list[dict] = field(default_factory=list)
    job_id: str | None = None
    case_count: int = 0


@dataclass
class _ExportJob:
    """进程内导出任务句柄（单 worker；完成/失败终态写 task_event）。"""

    task_id: str
    status: str = "running"  # running / ready / failed
    zip_path: str | None = None  # 绝对路径（FileResponse 直接消费）
    skipped: list[dict] = field(default_factory=list)
    case_count: int = 0
    error: str | None = None
    task: asyncio.Task | None = None


# 进程内 job 表（模块级；app 单例共享，单 worker 模型合法——dd §6.2 同口径）
_EXPORT_JOBS: dict[str, _ExportJob] = {}


class ExportService:
    def __init__(self, app: "AppContext"):
        self._app = app

    # ---- 对外入口 ----

    async def export_md_zip(
        self, task_id: str, *, case_ids: list[str] | None = None
    ) -> ExportOut:
        """dd §9.4 冻结签名。小批量同步打包；超阈值转后台任务。"""
        db = self._app.db
        task = await TaskDAO(db).get(task_id)  # 404 不暴露存在性
        cases, skipped = await self._select_and_verify(
            task.workspace_id, task_id, case_ids
        )
        total_bytes = await self._total_bytes(task.workspace_id, task_id, cases)

        if len(cases) <= SYNC_MAX_CASES and total_bytes <= SYNC_MAX_BYTES:
            # 同步路径：打包落盘 → ready
            job_id = uuid.uuid4().hex
            target = await self._app.file_store.export_zip_path(
                task.workspace_id, task_id, job_id
            )
            await asyncio.to_thread(
                self._write_zip_sync,
                task.workspace_id,
                task_id,
                cases,
                target,
            )
            job = _ExportJob(
                task_id=task_id,
                status="ready",
                zip_path=str(target),
                skipped=skipped,
                case_count=len(cases),
            )
            _EXPORT_JOBS[job_id] = job
            await self._emit(task_id, "export_ready", {
                "job_id": job_id, "case_count": len(cases),
                "skipped": len(skipped), "sync": True,
            })
            logger.info(
                "export sync done",
                extra={"task_id": task_id, "case_count": len(cases)},
            )
            return ExportOut(
                status="ready",
                download_url=f"/api/v1/tasks/{task_id}/export/{job_id}",
                skipped=skipped,
                job_id=job_id,
                case_count=len(cases),
            )

        # 异步路径：登记 job → 后台打包 → 轮询句柄
        job_id = uuid.uuid4().hex
        job = _ExportJob(task_id=task_id)
        _EXPORT_JOBS[job_id] = job
        job.task = asyncio.create_task(
            self._run_async_export(job_id, task_id, cases, skipped)
        )
        logger.info(
            "export async started",
            extra={"task_id": task_id, "job_id": job_id,
                   "case_count": len(cases), "total_bytes": total_bytes},
        )
        return ExportOut(
            status="running",
            download_url=None,
            skipped=skipped,
            job_id=job_id,
            case_count=len(cases),
        )

    async def get_job(self, task_id: str, job_id: str) -> ExportOut:
        """轮询句柄（dd §9.4：结果落 job，GET /tasks/{id}/export/{job_id}）。"""
        await TaskDAO(self._app.db).get(task_id)  # 404（task 不存在不暴露 job）
        job = _EXPORT_JOBS.get(job_id)
        if job is None or job.task_id != task_id:
            raise NotFoundError(f"导出任务不存在：{job_id}")
        if job.status == "failed":
            raise AppError(
                f"导出失败：{job.error}",
                details={"job_id": job_id, "reason": job.error},
            )
        return ExportOut(
            status=job.status,
            download_url=(
                f"/api/v1/tasks/{task_id}/export/{job_id}"
                if job.status == "ready" else None
            ),
            skipped=job.skipped,
            job_id=job_id,
            case_count=job.case_count,
        )

    async def job_zip_path(self, task_id: str, job_id: str) -> str:
        """下载句柄：返回 zip 绝对路径（API 层 FileResponse）。"""
        await TaskDAO(self._app.db).get(task_id)  # 404
        job = _EXPORT_JOBS.get(job_id)
        if job is None or job.task_id != task_id:
            raise NotFoundError(f"导出任务不存在：{job_id}")
        if job.status != "ready" or job.zip_path is None:
            raise AppError(
                "导出尚未就绪", details={"job_id": job_id, "status": job.status}
            )
        return job.zip_path

    # ---- 内部：筛选 / 校验 / 打包 ----

    async def _select_and_verify(
        self, ws_id: str, task_id: str, case_ids: list[str] | None
    ) -> tuple[list[CaseRow], list[dict]]:
        """筛选 + 逐文件 hash 校验（R34）：不一致/缺文件项入 skipped 不进 zip。"""
        dao = TestcaseDAO(self._app.db)
        if case_ids is not None:
            rows: list[CaseRow] = []
            for cid in dict.fromkeys(case_ids):  # 去重保序
                row = await dao.get(cid)  # NotFoundError
                if row.task_id != task_id:
                    raise NotFoundError(f"测试用例不存在：{cid}")
                rows.append(row)
            candidates = [
                r for r in rows
                if r.status == _EXPORT_ACTIVE
                and r.review_status in _EXPORT_REVIEW_STATES
            ]
        else:
            candidates = []
            cursor: str | None = None
            while True:
                page = await dao.list_by_task(
                    task_id, status=_EXPORT_ACTIVE, review=None,
                    version=None, cursor=cursor, limit=200,
                )
                candidates.extend(
                    r for r in page.items
                    if r.review_status in _EXPORT_REVIEW_STATES
                )
                if not page.next_cursor:
                    break
                cursor = page.next_cursor
        # 导出顺序确定性：按 (created_at, id) 升序（生成序，INDEX 序号稳定）
        candidates.sort(key=lambda r: (r.created_at, r.id))

        store = self._app.file_store
        kept: list[CaseRow] = []
        skipped: list[dict] = []
        for row in candidates:
            try:
                disk_hash = await store.hash_of(ws_id, task_id, row.file_path)
            except NotFoundError:
                skipped.append({
                    "case_id": row.id, "title": row.title,
                    "reason": "file_missing", "file_path": row.file_path,
                })
                continue
            except AppError as e:
                skipped.append({
                    "case_id": row.id, "title": row.title,
                    "reason": e.code, "file_path": row.file_path,
                })
                continue
            if disk_hash != row.content_hash:
                skipped.append({
                    "case_id": row.id, "title": row.title,
                    "reason": "hash_mismatch", "file_path": row.file_path,
                })
                continue
            kept.append(row)
        return kept, skipped

    async def _total_bytes(
        self, ws_id: str, task_id: str, cases: list[CaseRow]
    ) -> int:
        store = self._app.file_store
        total = 0
        for row in cases:
            try:
                data = await store.read_case(ws_id, task_id, row.file_path)
            except (NotFoundError, AppError):
                continue
            total += len(data.encode("utf-8"))
        return total

    async def _run_async_export(
        self,
        job_id: str,
        task_id: str,
        cases: list[CaseRow],
        skipped: list[dict],
    ) -> None:
        job = _EXPORT_JOBS[job_id]
        try:
            task = await TaskDAO(self._app.db).get(task_id)
            target = await self._app.file_store.export_zip_path(
                task.workspace_id, task_id, job_id
            )
            await asyncio.to_thread(
                self._write_zip_sync, task.workspace_id, task_id, cases, target
            )
        except Exception as e:  # noqa: BLE001 —— 兜底收口 job 失败态
            job.status = "failed"
            job.error = str(e)
            logger.warning(
                "export async failed",
                extra={"task_id": task_id, "job_id": job_id}, exc_info=True,
            )
            await self._emit(task_id, "export_failed", {
                "job_id": job_id, "error": str(e)[:200],
            })
            return
        job.status = "ready"
        job.zip_path = str(target)
        job.skipped = skipped
        job.case_count = len(cases)
        await self._emit(task_id, "export_ready", {
            "job_id": job_id, "case_count": len(cases),
            "skipped": len(skipped), "sync": False,
        })
        logger.info(
            "export async done",
            extra={"task_id": task_id, "job_id": job_id,
                   "case_count": len(cases)},
        )

    def _write_zip_sync(
        self,
        ws_id: str,
        task_id: str,
        cases: list[CaseRow],
        target,
    ) -> None:
        """内存打包 zip 后原子落盘（exports/ 目录 mkdir 由调用链保证）。

        dd §9.4 zip 结构：``v{n}/{point_id}-{case slug}.md`` + 根目录
        ``INDEX.md``。本函数在 ``asyncio.to_thread`` 线程内运行，直接调
        FileStore 的同步私有读避免二次线程切换（同包口径，见模块头偏离②）。
        """
        store = self._app.file_store
        buf = io.BytesIO()
        used_names: set[str] = set()
        index_rows: list[tuple[int, CaseRow, str]] = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for seq, row in enumerate(cases, start=1):
                data = store._read_text_sync(  # noqa: SLF001 —— 见 docstring
                    ws_id, task_id, row.file_path
                )
                arc = self._arcname(row, used_names)
                zf.writestr(arc, data)
                index_rows.append((seq, row, _priority_of(data)))
            zf.writestr("INDEX.md", _render_index(index_rows))
        payload = buf.getvalue()
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, payload)

    @staticmethod
    def _arcname(row: CaseRow, used: set[str]) -> str:
        """dd §9.4：``v{stage_version}/{point_id}-{case slug}.md``；
        目录内重名追加 -2/-3（slug 冲突不保证唯一，dd §4.1 同口径）。"""
        base = f"v{row.stage_version}/{row.point_id}-{slugify(row.title)}"
        name = f"{base}.md"
        n = 2
        while name in used:
            name = f"{base}-{n}.md"
            n += 1
        used.add(name)
        return name

    async def _emit(self, task_id: str, type_: str, payload: dict) -> None:
        bus = self._app.bus
        if bus is not None:
            await bus.emit(task_id, type_, payload)


def _priority_of(md_text: str) -> str:
    """优先级在 MD front-matter（testcase 行无此列）；清单从正文取回。"""
    match = _FM_PRIORITY_RE.search(md_text)
    return match.group(1) if match else "-"


def _render_index(rows: list[tuple[int, CaseRow, str]]) -> str:
    """INDEX.md 用例清单表（dd §9.4：序号/标题/优先级/评审状态/溯源条款）。

    溯源条款取 trace_refs.clause_ids（dd §2.5）。
    """
    lines = [
        "# 用例导出清单",
        "",
        f"导出时间：{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        "",
        "| 序号 | 标题 | 优先级 | 评审状态 | 溯源条款 |",
        "|---|---|---|---|---|",
    ]
    for seq, row, priority in rows:
        try:
            refs = json.loads(row.trace_refs or "{}")
            clauses = refs.get("clause_ids") or []
        except Exception:  # noqa: BLE001 —— 解析失败降级空清单
            clauses = []
        clause_text = ", ".join(str(c) for c in clauses) or "-"
        lines.append(
            f"| {seq} | {row.title} | {priority} | {row.review_status} "
            f"| {clause_text} |"
        )
    lines.append("")
    return "\n".join(lines)
