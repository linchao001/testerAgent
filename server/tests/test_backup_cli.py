"""WP-X2 backup CLI 验收（dd §19.5）。

覆盖：SQLite online backup 一致性、workspaces/ 打包、缺 checkpoints 可跳过、
缺 app.db 失败、CLI 入口 JSON 输出。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.cli import main as cli_main
from tester_agent.store.backup import run_backup, sqlite_backup
from tester_agent.store.db import run_migrations, seed_defaults


def _seed_data_dir(data: Path) -> None:
    data.mkdir(parents=True)
    run_migrations(data / "app.db")
    seed_defaults(data / "app.db")
    # checkpoints.db 简易建表（模拟 langgraph 产物）
    conn = sqlite3.connect(str(data / "checkpoints.db"))
    conn.execute("CREATE TABLE ckpt (id INTEGER PRIMARY KEY, payload TEXT)")
    conn.execute("INSERT INTO ckpt(payload) VALUES ('hello')")
    conn.commit()
    conn.close()
    ws = data / "workspaces" / "ws-1" / "tasks" / "task-1"
    ws.mkdir(parents=True)
    (ws / "requirement.md").write_text("# req\n", encoding="utf-8")


def test_sqlite_backup_roundtrip(tmp_path):
    src = tmp_path / "src.db"
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()

    dst = tmp_path / "dst.db"
    sqlite_backup(src, dst)
    rows = sqlite3.connect(str(dst)).execute("SELECT x FROM t").fetchall()
    assert rows == [(42,)]


def test_run_backup_full(tmp_path):
    data = tmp_path / "data"
    out = tmp_path / "bak"
    _seed_data_dir(data)

    report = run_backup(data_dir=data, out_dir=out)
    assert report.ok
    assert set(report.backed_up) == {"app.db", "checkpoints.db", "workspaces/"}
    assert not report.skipped
    assert (out / "app.db").is_file()
    assert (out / "checkpoints.db").is_file()
    assert (out / "workspaces" / "ws-1" / "tasks" / "task-1" / "requirement.md").read_text(
        encoding="utf-8"
    ) == "# req\n"
    # 备份库可独立打开读
    n = sqlite3.connect(str(out / "checkpoints.db")).execute(
        "SELECT COUNT(*) FROM ckpt"
    ).fetchone()[0]
    assert n == 1


def test_run_backup_skips_missing_checkpoints(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    run_migrations(data / "app.db")
    seed_defaults(data / "app.db")
    out = tmp_path / "bak"

    report = run_backup(data_dir=data, out_dir=out)
    assert report.ok
    assert "app.db" in report.backed_up
    assert {"file": "checkpoints.db", "reason": "missing"} in report.skipped
    assert {"file": "workspaces/", "reason": "missing"} in report.skipped


def test_run_backup_fails_without_app_db(tmp_path):
    data = tmp_path / "empty"
    data.mkdir()
    report = run_backup(data_dir=data, out_dir=tmp_path / "bak")
    assert not report.ok
    assert {"file": "app.db", "reason": "missing"} in report.skipped


def test_cli_backup_entry(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    out = tmp_path / "out"
    _seed_data_dir(data)
    monkeypatch.setenv("TESTER_AGENT_DATA_DIR", str(data))

    rc = cli_main(["backup", str(out)])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True
    assert "app.db" in printed["backed_up"]
    assert (out / "app.db").is_file()
