"""WP-29 维护清理验收测试：.tmp 崩溃残留清理 + exports 导出 zip 保留期清理
（dd §11.1 崩溃矩阵 / §6.5 惰性清理 / tech-design §3.3③）。

直接测 FileStore.soft_cleanup / cleanup_exports 行为，再通过
Maintenance.lazy_purge 验证联动（file_store 注入后惰性执行）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.store.workspace_files import FileStore


def _run(coro):
    return asyncio.run(coro)


def _set_old(path: Path, days: int) -> None:
    """把文件/目录 mtime 设为 days 天前。"""
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_soft_cleanup_removes_stale_tmp(tmp_path):
    store = FileStore(tmp_path / "fs")
    task_dir = store._root / "workspaces" / "ws-1" / "tasks" / "task-1" / "cases" / "v1"
    task_dir.mkdir(parents=True)
    # 过期 .tmp → 应删
    stale = task_dir / "case-xxx.md.tmp.abc"
    stale.write_bytes(b"x" * 100)
    _set_old(stale, 40)
    # 新鲜 .tmp → 保留
    fresh = task_dir / "case-yyy.md.tmp.def"
    fresh.write_bytes(b"y" * 50)
    # 普通 md → 不动
    normal = task_dir / "case-zzz.md"
    normal.write_text("# z\n")

    report = _run(store.soft_cleanup(retention_days=30))
    assert report.tmp_files_removed == 1
    assert report.bytes_freed == 100
    assert not stale.exists()
    assert fresh.exists()
    assert normal.exists()


def test_soft_cleanup_no_workspaces_dir(tmp_path):
    store = FileStore(tmp_path / "fs")
    report = _run(store.soft_cleanup(retention_days=30))
    assert report.tmp_files_removed == 0
    assert report.bytes_freed == 0


def test_cleanup_exports_removes_stale_job_dir(tmp_path):
    store = FileStore(tmp_path / "fs")
    exports = (
        store._root / "workspaces" / "ws-1" / "tasks" / "task-1" / "exports"
    )
    # 过期 job 目录 → 整目录删
    stale_job = exports / "job-old"
    stale_job.mkdir(parents=True)
    (stale_job / "export.zip").write_bytes(b"z" * 200)
    _set_old(stale_job, 60)
    # 新鲜 job → 保留
    fresh_job = exports / "job-new"
    fresh_job.mkdir(parents=True)
    (fresh_job / "export.zip").write_bytes(b"z" * 80)

    report = _run(store.cleanup_exports(retention_days=30))
    assert report.tmp_files_removed == 1
    assert report.bytes_freed == 200
    assert not stale_job.exists()
    assert fresh_job.exists()


def test_cleanup_exports_keeps_cases_and_snapshots(tmp_path):
    store = FileStore(tmp_path / "fs")
    task_dir = store._root / "workspaces" / "ws-1" / "tasks" / "task-1"
    cases = task_dir / "cases" / "v1"
    cases.mkdir(parents=True)
    (cases / "case.md").write_text("# c\n")
    _set_old(cases / "case.md", 90)
    snap = task_dir / "snapshots" / "v1"
    snap.mkdir(parents=True)
    (snap / "snap.md").write_text("# s\n")
    _set_old(snap / "snap.md", 90)

    report = _run(store.cleanup_exports(retention_days=1))
    assert report.tmp_files_removed == 0
    assert (cases / "case.md").exists()
    assert (snap / "snap.md").exists()


def test_maintenance_lazy_purge_invokes_file_cleanup(tmp_path):
    """Maintenance(file_store=...) 在 lazy_purge 中联动清理 .tmp/exports。"""
    from tester_agent.runtime.maintenance import Maintenance
    from tester_agent.store.db import Database, run_migrations
    from tester_agent.store.models import ConfigDAO

    db_path = tmp_path / "m.db"
    run_migrations(db_path)
    db = Database(db_path)

    store = FileStore(tmp_path / "fs2")
    task_dir = store._root / "workspaces" / "ws-1" / "tasks" / "task-1"
    cases = task_dir / "cases" / "v1"
    cases.mkdir(parents=True)
    stale_tmp = cases / "x.md.tmp.1"
    stale_tmp.write_bytes(b"a" * 10)
    _set_old(stale_tmp, 40)

    from tester_agent.runtime.idempotency import IdempotencyStore

    idem = IdempotencyStore(ttl_sec=3600)
    idem.record("e", "k", idem.request_hash("x"), 200, {})
    idem._data[("e", "k")].expires_at = time.monotonic() - 1

    counts = _run(
        Maintenance(db, ConfigDAO(db), file_store=store,
                    idempotency_store=idem).lazy_purge()
    )
    assert counts["tmp_files"] == 1
    assert counts["idempotency_keys"] == 1
    db.close()
