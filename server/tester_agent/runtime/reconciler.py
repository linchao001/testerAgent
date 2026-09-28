"""DB↔文件对账器（WP-29；dd §11.3 / tech-design §3.3③）。

三态检测（一个工作区内全部任务的 testcase 行 ↔ cases/ 文件）：

1. **file_missing**：DB 有行、盘上文件缺失 → ``TestcaseDAO.mark_error``
   置 ``error_info={"code":"file_missing"}``（UI 标红，提供"标记作废/找回"）；
2. **hash_conflict**：DB 行 hash ≠ 盘上文件 hash（外部改动）→
   ``mark_error("hash_conflict")``（用户二选一：以文件为准重新入库 /
   以库为准覆盖，走普通 PUT /cases/{id}，不特殊后门，dd §11.3）；
3. **orphan**：盘上文件存在、DB 无对应行 → 记入报告的
   ``orphan_paths``，不自动挂接（tech-design §3.3③"登记隔离目录清单"）。

触发时机：进程启动全量（main.py lifespan）、用户手动点
``POST /workspaces/{id}/reconcile``。已带 error_info 的行不参与 hash
比对（等用户消解后 error_info 被清空再重检）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..logging_config import get_logger

if TYPE_CHECKING:
    from ..store.models import CaseRow
    from ..store.workspace_files import FileStore

logger = get_logger(__name__)


@dataclass
class TaskReconcileReport:
    task_id: str
    file_missing: list[str] = field(default_factory=list)
    hash_conflict: list[str] = field(default_factory=list)
    orphan_paths: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(self.file_missing or self.hash_conflict or self.orphan_paths)


@dataclass
class ReconcileReport:
    workspace_id: str
    tasks: list[TaskReconcileReport] = field(default_factory=list)

    @property
    def file_missing_count(self) -> int:
        return sum(len(t.file_missing) for t in self.tasks)

    @property
    def hash_conflict_count(self) -> int:
        return sum(len(t.hash_conflict) for t in self.tasks)

    @property
    def orphan_count(self) -> int:
        return sum(len(t.orphan_paths) for t in self.tasks)

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "file_missing_count": self.file_missing_count,
            "hash_conflict_count": self.hash_conflict_count,
            "orphan_count": self.orphan_count,
            "tasks": [
                {
                    "task_id": t.task_id,
                    "file_missing": t.file_missing,
                    "hash_conflict": t.hash_conflict,
                    "orphan_paths": t.orphan_paths,
                }
                for t in self.tasks
            ],
        }


class Reconciler:
    """DB↔文件对账器（dd §11.3）。

    依赖通过构造函数注入（便于测试）：DAO 由调用方用 db 构造后传入，
    FileStore 从 app_ctx/file_store 取。单 worker 下对账与节点写入不
    并发（节点写文件后才写 DB，对账只读不改文件、仅 mark_error）。
    """

    def __init__(self, db: Any, file_store: "FileStore") -> None:
        self._db = db
        self._store = file_store

    async def reconcile_workspace(self, workspace_id: str) -> ReconcileReport:
        """对单个工作区全部任务做对账，返回报告（mark_error 副作用已落库）。"""
        from ..store.models import TaskDAO, TestcaseDAO, WorkspaceDAO

        # 工作区存在性守卫（不存在 → NotFound，不暴露存在性）
        await WorkspaceDAO(self._db).get(workspace_id)

        report = ReconcileReport(workspace_id=workspace_id)
        tasks = await TaskDAO(self._db).list_all_by_workspace(workspace_id)
        for task in tasks:
            task_report = await self._reconcile_task(workspace_id, task.id)
            report.tasks.append(task_report)
        logger.info(
            "reconcile workspace",
            extra={
                "workspace_id": workspace_id,
                "tasks": len(tasks),
                "file_missing": report.file_missing_count,
                "hash_conflict": report.hash_conflict_count,
                "orphan": report.orphan_count,
            },
        )
        return report

    async def _reconcile_task(
        self, workspace_id: str, task_id: str
    ) -> TaskReconcileReport:
        from ..store.models import TestcaseDAO

        dao = TestcaseDAO(self._db)
        cases: list[CaseRow] = await dao.list_all_for_task(task_id)
        file_infos = await self._store.list_case_files(workspace_id, task_id)
        files = {p.rel_path for p in file_infos}

        # by_path：仅 error_info 为空的行参与 hash 比对（dd §11.3 伪码）
        by_path = {c.file_path: c for c in cases if c.error_info is None}

        task_report = TaskReconcileReport(task_id=task_id)

        # ① DB 有行、文件缺失 → file_missing
        for c in cases:
            if c.file_path not in files:
                await dao.mark_error(c.id, "file_missing")
                task_report.file_missing.append(c.id)

        # ② 文件存在、DB 无行 → orphan
        for rel in sorted(files - set(by_path)):
            task_report.orphan_paths.append(rel)

        # ③ hash 不一致 → hash_conflict（仅文件存在且未带 error_info 的行；
        #    步骤①已判定 file_missing 的行不再进入本步，避免重复计数）
        for path, c in by_path.items():
            if path not in files:
                continue
            try:
                actual = await self._store.hash_of(workspace_id, task_id, path)
            except Exception:  # noqa: BLE001 — 读失败按文件缺失处理
                await dao.mark_error(c.id, "file_missing")
                task_report.file_missing.append(c.id)
                continue
            if actual != c.content_hash:
                await dao.mark_error(c.id, "hash_conflict")
                task_report.hash_conflict.append(c.id)

        return task_report

    async def reconcile_all(self) -> list[ReconcileReport]:
        """启动全量对账（遍历全部未软删工作区；dd §11.3 触发时机）。"""
        from ..store.models import WorkspaceDAO

        reports: list[ReconcileReport] = []
        for ws in await WorkspaceDAO(self._db).list_all():
            reports.append(await self.reconcile_workspace(ws.id))
        return reports
