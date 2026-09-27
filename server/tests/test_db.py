"""WP-02 验收：DDL 迁移（全新/幂等）、PRAGMA、async 访问层、immediate_tx、init-db 种子。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.cli import main as cli_main
from tester_agent.store.db import (
    BUILTIN_AGENT_ID,
    MIGRATIONS_DIR,
    _CONNECTION_PRAGMA_RE,
    Database,
    discover_migrations,
    run_migrations,
    seed_defaults,
    split_sql,
)

# dd §3.1 冻结的全部业务表/索引（顺序无关，仅断言存在性）
EXPECTED_TABLES = {
    "schema_meta",
    "workspace",
    "agent",
    "agent_workspace",
    "conversation",
    "message",
    "task",
    "stage_artifact",
    "testcase",
    "retrieval_trace",
    "context_snapshot",
    "task_event",
    "review_record",
    "kb_proposal",
    "config",
}
EXPECTED_INDEXES = {
    "idx_message_conv",
    "idx_message_task",
    "idx_task_ws_status",
    "idx_task_conv",
    "idx_task_heartbeat",
    "idx_artifact_active",
    "idx_case_task_review",
    "idx_case_point",
    "idx_trace_task",
    "idx_snapshot_task",
    "idx_event_task",
    "idx_review_task",
    "idx_proposal_ws",
}


@contextlib.contextmanager
def _open_raw(db_path: Path):
    """打开裸连接并保证关闭（sqlite3.Connection 的 with 只提交事务，不关连接）。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()  # 与原 sqlite3 CM 语义对齐：无异常退出时提交
    finally:
        conn.close()


# ---------- 迁移：全新库 / 幂等 / DDL 完整性 ----------


class TestMigrations:
    def test_discover_finds_all_migrations(self):
        migrations = discover_migrations()
        assert [v for v, _ in migrations] == [1, 2]
        assert migrations[0][1].name == "001_init.sql"
        assert migrations[1][1].name == "002_testcase_created_at.sql"

    def test_fresh_migration_creates_everything(self, tmp_path):
        db_path = tmp_path / "data" / "app.db"

        applied = run_migrations(db_path)

        assert applied == [1, 2]
        assert db_path.is_file()
        with _open_raw(db_path) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert EXPECTED_TABLES <= tables
            indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
                )
            }
            assert EXPECTED_INDEXES <= indexes
            # schema_meta 落账
            rows = conn.execute(
                "SELECT schema_version, applied_at FROM schema_meta ORDER BY schema_version"
            ).fetchall()
            assert [r["schema_version"] for r in rows] == [1, 2]
            assert rows[0]["applied_at"]
            # 002：testcase.created_at 为 NOT NULL
            case_cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(testcase)")}
            assert "created_at" in case_cols
            assert case_cols["created_at"]["notnull"] == 1
            # config 单行由 DDL 内置插入
            cfg = conn.execute("SELECT id FROM config").fetchall()
            assert [r["id"] for r in cfg] == [1]

    def test_repeat_migration_idempotent(self, tmp_path):
        db_path = tmp_path / "app.db"
        assert run_migrations(db_path) == [1, 2]

        # 重复执行：不重复应用、不重复插版本行、表完好
        assert run_migrations(db_path) == []
        with _open_raw(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM config").fetchone()[0] == 1

    def test_wal_persisted_on_file(self, tmp_path):
        # journal_mode 是库级持久属性：用不带任何 PRAGMA 的裸连接验证
        db_path = tmp_path / "app.db"
        run_migrations(db_path)
        with _open_raw(db_path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_split_sql_handles_comments_and_semicolons_in_strings(self):
        script = """
        -- 行注释；这里的分号不应切分
        CREATE TABLE t (a TEXT, b TEXT DEFAULT ';' ); -- 尾注释
        /* 块注释 ; ; */
        INSERT INTO t (a, b) VALUES ('it''s ok;', "ident;name");
        """
        stmts = split_sql(script)
        assert len(stmts) == 2
        assert stmts[0].startswith("CREATE TABLE t")
        assert "'it''s ok;'" in stmts[1]
        assert '"ident;name"' in stmts[1]

    def test_002_upgrades_001_db_and_backfills_created_at(self, tmp_path):
        """存量库升级路径：仅应用 001 → 插旧表数据 → run_migrations 补 002 →
        created_at 由 updated_at 回填；再跑幂等。"""
        db_path = tmp_path / "app.db"
        old_ts = "2026-09-20T08:00:00.000Z"

        # 手工构造"只到 001"的存量库（裸连接 FK 默认关闭，免造父行）
        conn = sqlite3.connect(str(db_path))
        try:
            ddl = (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
            for stmt in split_sql(ddl):
                if _CONNECTION_PRAGMA_RE.match(stmt):
                    continue
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_meta (schema_version, applied_at) VALUES (1, ?)",
                (old_ts,),
            )
            # 旧 testcase 表无 created_at 列
            cols = [r[1] for r in conn.execute("PRAGMA table_info(testcase)")]
            assert "created_at" not in cols
            conn.execute(
                "INSERT INTO testcase "
                "(id, task_id, point_id, stage_version, lineage, status, "
                "review_status, file_path, content_hash, title, trace_refs, updated_at) "
                "VALUES ('old-1','t1','pt-1',1,'{}','active','pending',"
                "'p.md','h','标题','{}',?)",
                (old_ts,),
            )
            conn.commit()
        finally:
            conn.close()

        assert run_migrations(db_path) == [2]

        with _open_raw(db_path) as conn:
            row = conn.execute(
                "SELECT created_at, updated_at FROM testcase WHERE id = 'old-1'"
            ).fetchone()
            # 存量行回填 created_at = updated_at
            assert row["created_at"] == old_ts
            assert row["updated_at"] == old_ts
            versions = [
                r[0]
                for r in conn.execute(
                    "SELECT schema_version FROM schema_meta ORDER BY schema_version"
                )
            ]
            assert versions == [1, 2]
        assert run_migrations(db_path) == []


# ---------- Database 访问层：PRAGMA / aexecute / aquery / immediate_tx ----------


class TestDatabase:
    @pytest.fixture()
    def db(self, tmp_path):
        db_path = tmp_path / "app.db"
        run_migrations(db_path)
        handle = Database(db_path)
        yield handle
        handle.close()

    async def test_connection_pragmas(self, db):
        assert (await db.aquery_one("PRAGMA journal_mode"))[0].lower() == "wal"
        assert (await db.aquery_one("PRAGMA busy_timeout"))[0] == 5000
        assert (await db.aquery_one("PRAGMA foreign_keys"))[0] == 1

    async def test_aexecute_aquery_roundtrip(self, db):
        res = await db.aexecute(
            "INSERT INTO workspace (id, name, description, created_at) VALUES (?, ?, ?, ?)",
            ("w1", "工作区", "", "2026-09-26T00:00:00Z"),
        )
        assert res.rowcount == 1

        rows = await db.aquery("SELECT id, name FROM workspace ORDER BY id")
        assert len(rows) == 1
        assert rows[0]["name"] == "工作区"
        assert (await db.aquery_one("SELECT id FROM workspace WHERE id=?", ("w1",)))[
            "id"
        ] == "w1"
        assert await db.aquery_one("SELECT id FROM workspace WHERE id=?", ("nope",)) is None

    async def test_foreign_keys_enforced(self, db):
        # agent_workspace 双外键：引用不存在的 agent 必须被拒
        with pytest.raises(sqlite3.IntegrityError):
            await db.aexecute(
                "INSERT INTO agent_workspace (agent_id, workspace_id) VALUES (?, ?)",
                ("ghost", "ghost"),
            )

    async def test_immediate_tx_commit(self, db):
        async with db.immediate_tx() as tx:
            await tx.aexecute(
                "INSERT INTO workspace (id, name, created_at) VALUES (?, ?, ?)",
                ("w2", "事务内", "2026-09-26T00:00:00Z"),
            )
        rows = await db.aquery("SELECT id FROM workspace")
        assert [r["id"] for r in rows] == ["w2"]

    async def test_immediate_tx_rollback_on_error(self, db):
        with pytest.raises(RuntimeError, match="boom"):
            async with db.immediate_tx() as tx:
                await tx.aexecute(
                    "INSERT INTO workspace (id, name, created_at) VALUES (?, ?, ?)",
                    ("w3", "将回滚", "2026-09-26T00:00:00Z"),
                )
                raise RuntimeError("boom")
        assert await db.aquery_one("SELECT id FROM workspace WHERE id=?", ("w3",)) is None
        # 回滚后连接可继续开启新事务
        async with db.immediate_tx() as tx:
            await tx.aexecute(
                "INSERT INTO workspace (id, name, created_at) VALUES (?, ?, ?)",
                ("w4", "新事务", "2026-09-26T00:00:00Z"),
            )
        assert (await db.aquery_one("SELECT COUNT(*) AS c FROM workspace"))["c"] == 1

    async def test_concurrent_access_serialized(self, db):
        async def insert(i: int) -> None:
            await db.aexecute(
                "INSERT INTO workspace (id, name, created_at) VALUES (?, ?, ?)",
                (f"w{i}", f"name{i}", "2026-09-26T00:00:00Z"),
            )
            # 每个任务穿插读，验证单连接串行下无 "Recursive use" 类异常
            await db.aquery_one("SELECT COUNT(*) AS c FROM workspace")

        await asyncio.gather(*(insert(i) for i in range(50)))
        assert (await db.aquery_one("SELECT COUNT(*) AS c FROM workspace"))["c"] == 50


# ---------- init-db 种子（dd §19.4） ----------


class TestSeed:
    def test_seed_idempotent(self, tmp_path):
        db_path = tmp_path / "app.db"
        run_migrations(db_path)

        first = seed_defaults(db_path)
        assert first["config_seeded"] is True  # 全新库：默认 runtime 落位
        assert first["agent_inserted"] is True  # 全新库：内置 agent 被插入
        second = seed_defaults(db_path)
        assert second["config_seeded"] is False  # 再跑不重复、不覆盖
        assert second["agent_inserted"] is False

        with _open_raw(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM agent").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM config").fetchone()[0] == 1
            row = conn.execute(
                "SELECT id, name, agent_type, builtin, config FROM agent WHERE id=?",
                (BUILTIN_AGENT_ID,),
            ).fetchone()
            assert row["agent_type"] == "case_designer"
            assert row["builtin"] == 1
            cfg = json.loads(row["config"])
            assert cfg["snapshot_level_default"] == "meta"
            assert cfg["prompts_dir"] == "server/prompts"
            runtime = json.loads(
                conn.execute("SELECT runtime_config FROM config WHERE id=1").fetchone()[
                    "runtime_config"
                ]
            )
            assert runtime["batch_size"] == 5
            assert runtime["retrieval_token_budgets"]["point_write"] == 16000
            # model_config 保持空（首启引导页要求填写 api_key，dd §19.4）
            assert (
                conn.execute("SELECT model_config FROM config WHERE id=1").fetchone()[
                    "model_config"
                ]
                == "{}"
            )

    def test_seed_does_not_overwrite_user_config(self, tmp_path):
        db_path = tmp_path / "app.db"
        run_migrations(db_path)
        seed_defaults(db_path)
        with _open_raw(db_path) as conn:
            conn.execute(
                "UPDATE agent SET name=? WHERE id=?", ("自定义名称", BUILTIN_AGENT_ID)
            )
            conn.execute(
                "UPDATE config SET runtime_config=? WHERE id=1",
                (json.dumps({"batch_size": 99}),),
            )
        seed_defaults(db_path)  # 重复种子
        with _open_raw(db_path) as conn:
            assert conn.execute(
                "SELECT name FROM agent WHERE id=?", (BUILTIN_AGENT_ID,)
            ).fetchone()["name"] == "自定义名称"
            runtime = json.loads(
                conn.execute(
                    "SELECT runtime_config FROM config WHERE id=1"
                ).fetchone()["runtime_config"]
            )
            assert runtime == {"batch_size": 99}

    def test_cli_init_db_fresh_and_repeat(self, tmp_path, capsys):
        env = {
            "TESTER_AGENT_DATA_DIR": str(tmp_path / "data"),
            "TESTER_AGENT_SINGLE_WORKER": "1",
        }
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            assert cli_main(["init-db"]) == 0
            out_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
            out1 = json.loads(out_lines[-1])
            assert out1["ok"] is True
            assert out1["migrated"] == [1, 2]
            assert out1["seeded"]["agent_inserted"] is True

            assert cli_main(["init-db"]) == 0
            out_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
            out2 = json.loads(out_lines[-1])
            assert out2["migrated"] == []
            assert out2["seeded"]["agent_inserted"] is False
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        assert (tmp_path / "data" / "app.db").is_file()
