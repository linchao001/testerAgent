"""Migration 004 + runtime config for Plan-Execute."""

from pathlib import Path

from tester_agent.store.db import DEFAULT_RUNTIME_CONFIG, _connect, run_migrations


def test_004_adds_plan_columns_and_subtask(tmp_path: Path):
    db = tmp_path / "app.db"
    run_migrations(db)
    conn = _connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task)").fetchall()}
    assert "current_plan_artifact_id" in cols
    kinds = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='subtask'"
    ).fetchone()
    assert kinds is not None
    art_cols = {r[1] for r in conn.execute("PRAGMA table_info(stage_artifact)").fetchall()}
    assert "kind" in art_cols
    conn.close()


def test_runtime_config_gate_defaults():
    assert DEFAULT_RUNTIME_CONFIG["human_gate_link"] is True
    assert DEFAULT_RUNTIME_CONFIG["human_gate_point"] is True
    assert DEFAULT_RUNTIME_CONFIG["human_gate_review"] is True
    assert DEFAULT_RUNTIME_CONFIG["reflect_max_per_step"] == 2
    assert DEFAULT_RUNTIME_CONFIG["replan_max"] == 3
    assert DEFAULT_RUNTIME_CONFIG["subtask_timeout_sec"] == 600
