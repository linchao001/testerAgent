"""WP-32 Task 12：005_context_journal 迁移 + ContextJournalDAO。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.store.db import (
    DEFAULT_RUNTIME_CONFIG,
    Database,
    MigrationError,
    discover_migrations,
    run_migrations,
)
from tester_agent.store.models import ContextJournalDAO, ContextJournalRow


TS0 = "2026-09-29T00:00:00.000Z"
TS1 = "2026-09-29T00:00:01.000Z"
TS2 = "2026-09-29T00:00:02.000Z"


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "app.db"
    run_migrations(path)
    handle = Database(path)
    yield handle
    handle.close()


def test_005_creates_table_and_indexes(tmp_path: Path):
    path = tmp_path / "app.db"
    applied = run_migrations(path)
    assert 5 in applied
    assert run_migrations(path) == []  # 幂等

    conn = __import__("tester_agent.store.db", fromlist=["_connect"])._connect(path)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "context_journal" in tables
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(context_journal)").fetchall()
        }
        for required in (
            "id",
            "workspace_id",
            "owner_type",
            "owner_id",
            "partition",
            "entry_id",
            "entry_kind",
            "action",
            "reason",
            "policy_version",
            "created_at",
            "digest",
            "refs",
            "scope_level",
            "phase",
        ):
            assert required in cols
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_ctx_journal_owner" in indexes
        assert "idx_ctx_journal_ws" in indexes
        assert "idx_ctx_journal_entry" in indexes
    finally:
        conn.close()


def test_discover_rejects_illegal_migration_name(tmp_path: Path, monkeypatch):
    from tester_agent.store import db as db_mod

    bad = tmp_path / "migrations"
    bad.mkdir()
    (bad / "not_a_migration.sql").write_text("SELECT 1;", encoding="utf-8")
    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", bad)
    with pytest.raises(MigrationError, match="不合法"):
        discover_migrations()


def test_context_runtime_keys_present():
    assert DEFAULT_RUNTIME_CONFIG["context.enabled"] is True
    assert DEFAULT_RUNTIME_CONFIG["context.policy_version"] == "cp-v1"


def _row(
    id_: str,
    *,
    ws: str = "ws1",
    owner_type: str = "task",
    owner_id: str = "t1",
    action: str = "append",
    partition: str = "P2",
    created_at: str = TS0,
    **extra,
) -> ContextJournalRow:
    base = dict(
        id=id_,
        workspace_id=ws,
        owner_type=owner_type,
        owner_id=owner_id,
        task_id=owner_id if owner_type == "task" else None,
        conversation_id=owner_id if owner_type == "conversation" else None,
        partition=partition,
        entry_id=extra.pop("entry_id", f"e-{id_}"),
        entry_kind=extra.pop("entry_kind", "reflection"),
        action=action,
        reason=extra.pop("reason", "append"),
        policy_version="cp-v1",
        tokens_est=0,
        digest="",
        refs="{}",
        scope_level="task",
        phase="shared",
        step_id=None,
        batch_id=None,
        item_key=None,
        created_at=created_at,
    )
    base.update(extra)
    return ContextJournalRow(**base)


async def test_put_batch_insert_or_ignore_preserves_created_at(db: Database):
    dao = ContextJournalDAO(db)
    await dao.put_batch([_row("j1", created_at=TS0)])
    await dao.put_batch(
        [_row("j1", created_at=TS2, reason="should-not-overwrite")]
    )
    rows = await dao.list_actions(
        workspace_id="ws1", owner_type="task", owner_id="t1"
    )
    assert len(rows) == 1
    assert rows[0].created_at == TS0
    assert rows[0].reason == "append"


async def test_list_by_owner_keyset_no_dup_no_gap(db: Database):
    dao = ContextJournalDAO(db)
    await dao.put_batch(
        [
            _row("a", created_at=TS0),
            _row("b", created_at=TS1),
            _row("c", created_at=TS2),
            _row("d", created_at=TS2),  # 同秒，靠 id tiebreak
        ]
    )
    page1 = await dao.list_by_owner(
        workspace_id="ws1",
        owner_type="task",
        owner_id="t1",
        cursor=None,
        limit=2,
    )
    assert [r.id for r in page1.items] == ["d", "c"]
    assert page1.next_cursor is not None

    page2 = await dao.list_by_owner(
        workspace_id="ws1",
        owner_type="task",
        owner_id="t1",
        cursor=page1.next_cursor,
        limit=2,
    )
    assert [r.id for r in page2.items] == ["b", "a"]
    assert page2.next_cursor is None

    all_ids = [r.id for r in page1.items] + [r.id for r in page2.items]
    assert sorted(all_ids) == ["a", "b", "c", "d"]
    assert len(set(all_ids)) == 4


async def test_list_by_owner_partition_filter(db: Database):
    dao = ContextJournalDAO(db)
    await dao.put_batch(
        [
            _row("p1", partition="P1", created_at=TS0),
            _row("p2", partition="P2", created_at=TS1),
        ]
    )
    page = await dao.list_by_owner(
        workspace_id="ws1",
        owner_type="task",
        owner_id="t1",
        cursor=None,
        limit=10,
        partition="P1",
    )
    assert [r.id for r in page.items] == ["p1"]


async def test_workspace_isolation(db: Database):
    dao = ContextJournalDAO(db)
    await dao.put_batch([_row("x", ws="ws1")])
    page = await dao.list_by_owner(
        workspace_id="ws-other",
        owner_type="task",
        owner_id="t1",
        cursor=None,
        limit=10,
    )
    assert page.items == []
    actions = await dao.list_actions(
        workspace_id="ws-other", owner_type="task", owner_id="t1"
    )
    assert actions == []


async def test_list_actions_ascending(db: Database):
    dao = ContextJournalDAO(db)
    await dao.put_batch(
        [
            _row("z", created_at=TS2, action="evict", reason="budget_cut"),
            _row("x", created_at=TS0, action="append", reason="append"),
            _row("y", created_at=TS1, action="demote", reason="step_window"),
        ]
    )
    rows = await dao.list_actions(
        workspace_id="ws1", owner_type="task", owner_id="t1"
    )
    assert [r.id for r in rows] == ["x", "y", "z"]
    assert [r.action for r in rows] == ["append", "demote", "evict"]
