"""数据备份（dd §19.5 / WP-X2）。

对 ``app.db`` / ``checkpoints.db`` 使用 SQLite online backup API（可在
服务运行时热备），再连同 ``workspaces/`` 目录复制到目标目录。不做自动
调度；由 ``cli backup <out_dir>`` 显式触发。
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BackupReport:
    """备份结果（CLI JSON 输出）。"""

    ok: bool
    out_dir: str
    backed_up: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {
            "ok": self.ok,
            "out_dir": self.out_dir,
            "backed_up": list(self.backed_up),
        }
        if self.skipped:
            d["skipped"] = list(self.skipped)
        return d


def sqlite_backup(src: Path, dst: Path) -> None:
    """用 SQLite backup API 将 src 在线备份到 dst（覆盖已有文件）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def run_backup(
    *,
    data_dir: Path,
    out_dir: Path,
    app_db_name: str = "app.db",
    checkpoints_db_name: str = "checkpoints.db",
    workspaces_name: str = "workspaces",
) -> BackupReport:
    """执行一次完整备份：两 DB + workspaces/ → out_dir。

    源文件缺失时记入 skipped，不失败（checkpoints.db 可能尚未创建）。
    目标目录已存在同名项时：DB 覆盖写；workspaces/ 先删后拷。
    """
    data_dir = data_dir.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    report = BackupReport(ok=True, out_dir=str(out_dir))

    for name in (app_db_name, checkpoints_db_name):
        src = data_dir / name
        dst = out_dir / name
        if not src.is_file():
            report.skipped.append({"file": name, "reason": "missing"})
            continue
        sqlite_backup(src, dst)
        report.backed_up.append(name)

    ws_src = data_dir / workspaces_name
    ws_dst = out_dir / workspaces_name
    if not ws_src.is_dir():
        report.skipped.append({"file": f"{workspaces_name}/", "reason": "missing"})
    else:
        if ws_dst.exists():
            shutil.rmtree(ws_dst)
        shutil.copytree(ws_src, ws_dst)
        report.backed_up.append(f"{workspaces_name}/")

    # app.db 缺失视为失败（业务库是必备）；仅 checkpoints/workspaces 可缺
    if app_db_name not in report.backed_up:
        report.ok = False
    return report
