"""维护性清理（dd §6.5 启动序列第 5 步 + WP-29 对账/幂等清理联动）。

:meth:`Maintenance.lazy_purge` 在每次启动时按 runtime_config 的 retention
（dd §13.2）清理超保留期数据：

- events_days（默认 7）→ EventDAO.purge_before；
- snapshots_days（默认 30）→ SnapshotDAO.purge_before；
- proposals_days（默认 14）→ ProposalDAO.purge_terminal_before（只清终态，
  pending 提案保留）；
- **WP-29 增量**：obsolete_cases_days/exports_days（默认 30）→ 文件侧
  清理（FileStore.soft_cleanup 清 .tmp 崩溃残留 + cleanup_exports 清导出
  zip）；idempotency_store.purge_expired 清过期幂等键。

"lazy" 语义：不做后台周期任务，仅在进程启动时跑一次；单 worker 模型下
足够（长期不重启时数据多留，不影响正确性）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..logging_config import get_logger
from ..store.db import iso_ago

if TYPE_CHECKING:
    from ..store.models import ConfigDAO
    from ..store.workspace_files import FileStore

logger = get_logger(__name__)

#: retention 默认值（dd §13.2；单位：天）
DEFAULT_RETENTION: dict[str, int] = {
    "events_days": 7,
    "snapshots_days": 30,
    "proposals_days": 14,
    "obsolete_cases_days": 30,
    "exports_days": 30,
}

_DAY_SEC = 86_400


class Maintenance:
    """保留期惰性清理器（dd §6.5）。

    :param retention: 显式保留期覆盖（测试注入）；None 时从 runtime_config 读。
    :param file_store: WP-29 注入，用于 .tmp/exports 文件侧清理；None 时跳过。
    :param idempotency_store: WP-29 注入，用于过期幂等键清理；None 时跳过。
    """

    def __init__(
        self,
        db: Any,
        config_dao: "ConfigDAO | None" = None,
        *,
        retention: dict[str, int] | None = None,
        file_store: "FileStore | None" = None,
        idempotency_store: Any = None,
    ) -> None:
        self._db = db
        self._config_dao = config_dao
        self._override_retention = retention
        self._file_store = file_store
        self._idem = idempotency_store

    async def _resolve_retention(self) -> dict[str, int]:
        """合并默认值与 runtime_config.retention（非法/负值退回默认）。"""
        merged = dict(DEFAULT_RETENTION)
        raw_retention: Any = None
        if self._override_retention is not None:
            raw_retention = self._override_retention
        elif self._config_dao is not None:
            cfg = await self._config_dao.get()
            raw_retention = cfg.runtime_dict().get("retention")

        if isinstance(raw_retention, dict):
            for key in DEFAULT_RETENTION:
                val = raw_retention.get(key)
                if isinstance(val, (int, float)) and val >= 0:
                    merged[key] = int(val)
        return merged

    async def lazy_purge(self) -> dict[str, int]:
        """执行清理，返回各类删除计数。"""
        from ..store.models import EventDAO, ProposalDAO, SnapshotDAO

        retention = await self._resolve_retention()
        counts: dict[str, int] = {
            "events": await EventDAO(self._db).purge_before(
                iso_ago(retention["events_days"] * _DAY_SEC)
            ),
            "snapshots": await SnapshotDAO(self._db).purge_before(
                iso_ago(retention["snapshots_days"] * _DAY_SEC)
            ),
            "proposals": await ProposalDAO(self._db).purge_terminal_before(
                iso_ago(retention["proposals_days"] * _DAY_SEC)
            ),
        }
        # WP-29：文件侧 .tmp 崩溃残留 + 导出 zip + 过期幂等键
        if self._file_store is not None:
            tmp_report = await self._file_store.soft_cleanup(
                retention["obsolete_cases_days"]
            )
            counts["tmp_files"] = tmp_report.tmp_files_removed
            exp_report = await self._file_store.cleanup_exports(
                retention["exports_days"]
            )
            counts["export_files"] = exp_report.tmp_files_removed
        if self._idem is not None:
            counts["idempotency_keys"] = self._idem.purge_expired()
        if any(counts.values()):
            logger.info("lazy purge", extra=counts)
        return counts
