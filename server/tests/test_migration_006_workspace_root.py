"""Migration 006：workspace.root_dir。"""

from pathlib import Path

from tester_agent.store.db import _connect, run_migrations


def test_006_adds_workspace_root_dir(tmp_path: Path):
    db = tmp_path / "app.db"
    applied = run_migrations(db)
    assert 6 in applied
    assert run_migrations(db) == []

    conn = _connect(db)
    cols = {r[1]: r for r in conn.execute("PRAGMA table_info(workspace)").fetchall()}
    assert "root_dir" in cols
    # SQLite stores DEFAULT '' as "''"
    assert cols["root_dir"][4] in ("''", '""', "")
    conn.close()
