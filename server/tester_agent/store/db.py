"""SQLite 访问层与 schema 迁移执行器（dd §3.1 §3.2）。

- 业务库 app.db：WAL、foreign_keys=ON、busy_timeout=5000；
- ``Database`` 为 asyncio 侧提供 ``aexecute``/``aquery`` 与 ``immediate_tx()``：
  所有阻塞调用经单线程 executor 串行落到同一连接（``check_same_thread=False``），
  与单 worker 进程模型匹配（dd §13.1）；
- ``run_migrations()`` 供启动时与 ``cli init-db`` 复用：按文件名顺序执行未应用迁移，
  每个迁移的 DDL 与 schema_meta 版本号在同一 ``BEGIN IMMEDIATE`` 事务提交；重复执行幂等。
- checkpoints.db 由 langgraph-checkpoint-sqlite 自行建表，不纳入业务迁移（dd §3.2）。
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..logging_config import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{3})_[A-Za-z0-9][A-Za-z0-9_-]*\.sql$")
# 这三条 PRAGMA 属连接级设置（journal_mode 不可在事务内切换），由连接层统一施加，
# 迁移文件中保留仅为事实声明，执行时跳过。
_CONNECTION_PRAGMA_RE = re.compile(
    r"^\s*PRAGMA\s+(journal_mode|foreign_keys|busy_timeout)\b", re.IGNORECASE
)

# ---------- §13.2 默认种子（init-db 幂等，dd §19.4） ----------
BUILTIN_AGENT_ID = "builtin-case-designer"
BUILTIN_AGENT_NAME = "用例设计智能体"
BUILTIN_AGENT_CONFIG = {
    "snapshot_level_default": "meta",
    "ambiguity_check": True,
    "prompts_dir": "server/prompts",
    "retrieval_overrides": {},
    "enable_tools_stages": [],
}
def _default_context_profiles() -> dict[str, dict[str, int]]:
    """与 context.budget.PROFILES 同源（懒导入，避免模块顶层环依赖）。"""
    from ..context.budget import PROFILES

    return {
        name.value: {"p0": b.p0, "p1": b.p1, "p2": b.p2}
        for name, b in PROFILES.items()
    }


DEFAULT_RUNTIME_CONFIG = {
    "llm_concurrency": 4,
    "llm_timeout_connect_sec": 10,
    "llm_timeout_read_sec": 120,
    "batch_size": 5,
    "index_inject_hard_limit": 200,
    "retrieval_token_budgets": {
        "link_identify": 12000,
        "point_write": 16000,
        "case_generate": 12000,
    },
    "heartbeat_interval_sec": 10,
    "heartbeat_stale_sec": 120,
    "suspend_stale_days": 7,
    "retention": {
        "events_days": 7,
        "snapshots_days": 30,
        "obsolete_cases_days": 30,
        "proposals_days": 14,
    },
    "index_mirror_ttl_min": 60,
    "coverage_max_rounds": 2,
    "export_sync_limits": {"cases": 200, "bytes": 20971520},
    "tool_bash_timeout_ms": 300000,
    "tool_max_output_chars": 16000,
    "tool_agent_max_steps": 12,
    "tool_shell_backend": "auto",
    "human_gate_link": True,
    "human_gate_point": True,
    "human_gate_review": True,
    "reflect_max_per_step": 2,
    "replan_max": 3,
    "subtask_timeout_sec": 600,
    # 上下文管理层（spec §8 / plan §10）；关闭时消费点退回旧路径
    "context.enabled": True,
    "context.policy_version": "cp-v1",
    "context.step_window": 1,
    "context.chat_recent_turns": 6,
    "context.goal_overlap_floor": 0.05,
    "context.goal_drift_window": 5,
    "context.case_index_digest": False,
    "context.intervention.enabled": True,
    "context.profiles": _default_context_profiles(),
}


class MigrationError(RuntimeError):
    """迁移文件非法或迁移执行失败。"""


@dataclass(frozen=True)
class ExecResult:
    """aexecute 返回：自增/最后插入主键与受影响行数。"""

    lastrowid: int | None
    rowcount: int


def utcnow_iso() -> str:
    """统一 UTC ISO-8601（毫秒，Z 后缀）时间戳。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso_ago(seconds: float) -> str:
    """距今 ``seconds`` 秒前的 UTC ISO-8601 时间戳（Reaper/保留期 cutoff 用）。"""
    past = datetime.now(timezone.utc) - timedelta(seconds=float(seconds))
    return past.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """业务库连接三件套（dd §3.1）：WAL 持久化于库文件，其余两条为连接级。"""
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":  # 连接处于自动提交态，切换应当成功；失败即显形
        raise MigrationError(f"PRAGMA journal_mode=WAL 设置失败，实际为 {mode!r}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def _connect(db_path: Path | str) -> sqlite3.Connection:
    """供迁移/种子等同步场景使用的业务连接（autocommit，显式事务由调用方管理）。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


def split_sql(script: str) -> list[str]:
    """把迁移脚本切成独立语句。

    轻量词法处理：识别 ``--`` 行注释、``/* */`` 块注释、单引号字符串（'' 转义）、
    双引号/反引号标识符，按分号切分。仅供我们自有的 DDL/DML 迁移使用
    （无触发器/存储过程）。
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        nxt = script[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = script.find("\n", i)
            i = n if end == -1 else end
            continue
        if ch == "/" and nxt == "*":
            end = script.find("*/", i + 2)
            if end == -1:
                raise MigrationError("SQL 脚本存在未闭合的块注释")
            i = end + 2
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            while i < n:
                c = script[i]
                buf.append(c)
                i += 1
                if c == quote:
                    if i < n and script[i] == quote:  # 引号转义 '' / ""
                        buf.append(script[i])
                        i += 1
                        continue
                    break
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def discover_migrations() -> list[tuple[int, Path]]:
    """发现 ``migrations/NNN_xxx.sql``，按版本号升序返回；重号/非法名直接报错。"""
    found: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(path.name)
        if not m:
            raise MigrationError(
                f"迁移文件名不合法：{path.name}（需形如 001_init.sql）"
            )
        found.append((int(m.group(1)), path))
    versions = [v for v, _ in found]
    dupes = {v for v in versions if versions.count(v) > 1}
    if dupes:
        raise MigrationError(f"迁移版本号重复：{sorted(dupes)}")
    return found


def run_migrations(db_path: Path | str) -> list[int]:
    """执行所有未应用迁移，返回本次新应用的版本号列表（幂等：再跑返回 []）。

    每个迁移：``BEGIN IMMEDIATE`` → 顺序执行语句 → 写 schema_meta → ``COMMIT``，
    任一步失败整体 ROLLBACK（dd §3.2）。
    """
    conn = _connect(db_path)
    newly_applied: list[int] = []
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            "schema_version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            row[0] for row in conn.execute("SELECT schema_version FROM schema_meta")
        }
        for version, path in discover_migrations():
            if version in applied:
                continue
            statements = [
                s
                for s in split_sql(path.read_text(encoding="utf-8"))
                if not _CONNECTION_PRAGMA_RE.match(s)
            ]
            logger.info("db migration applying", extra={"version": version, "file": path.name})
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in statements:
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_meta (schema_version, applied_at) VALUES (?, ?)",
                    (version, utcnow_iso()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                logger.exception("db migration failed", extra={"version": version})
                raise
            newly_applied.append(version)
        return newly_applied
    finally:
        conn.close()


def seed_defaults(db_path: Path | str) -> dict[str, bool]:
    """init-db 幂等种子（dd §19.4）：config 单行 + 内置用例智能体；不创建默认工作区。

    不覆盖任何用户改动：
    - config：行不存在则插入默认值；001 迁移内置的引导行为两个 '{}'，
      视为"未初始化"，补齐 §13.2 默认 runtime_config（model_config 仍保持空，
      首启引导页要求填写 api_key）；任一列已被改动则不动。
    """
    conn = _connect(db_path)
    try:
        runtime_json = json.dumps(DEFAULT_RUNTIME_CONFIG, ensure_ascii=False)
        cur = conn.execute(
            "INSERT OR IGNORE INTO config (id, model_config, runtime_config) "
            "VALUES (1, '{}', ?)",
            (runtime_json,),
        )
        seeded = cur.rowcount > 0
        if not seeded:
            cur = conn.execute(
                "UPDATE config SET runtime_config = ? "
                "WHERE id = 1 AND model_config = '{}' AND runtime_config = '{}'",
                (runtime_json,),
            )
            seeded = cur.rowcount > 0
        cur = conn.execute(
            "INSERT OR IGNORE INTO agent "
            "(id, name, agent_type, config, builtin, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (
                BUILTIN_AGENT_ID,
                BUILTIN_AGENT_NAME,
                "case_designer",
                json.dumps(BUILTIN_AGENT_CONFIG, ensure_ascii=False),
                utcnow_iso(),
            ),
        )
        agent_inserted = cur.rowcount > 0
        return {"config_seeded": seeded, "agent_inserted": agent_inserted}
    finally:
        conn.close()


class Database:
    """业务库 async 访问句柄。

    单连接 + 单线程 executor 串行化全部阻塞调用；``immediate_tx()`` 额外用
    asyncio 互斥锁保证写事务不被协程交错。DAO（WP-03 起）构造时注入本对象。
    """

    def __init__(self, db_path: Path | str):
        self._path = Path(db_path)
        self._conn = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        _apply_pragmas(self._conn)
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="tester-sqlite"
        )
        self._tx_lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _execute(self, sql: str, params: tuple) -> ExecResult:
        cur = self._conn.execute(sql, params)
        return ExecResult(lastrowid=cur.lastrowid, rowcount=cur.rowcount)

    async def aexecute(self, sql: str, params: tuple | list | dict = ()) -> ExecResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._execute, sql, tuple(params))

    def _executemany(self, sql: str, seq_of_params: list[tuple]) -> ExecResult:
        cur = self._conn.executemany(sql, seq_of_params)
        return ExecResult(lastrowid=cur.lastrowid, rowcount=cur.rowcount)

    async def aexecutemany(
        self, sql: str, seq_of_params: list[tuple] | tuple[tuple, ...]
    ) -> ExecResult:
        """批量执行（如 put_batch 的 INSERT OR IGNORE）；串行落到同一连接。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, self._executemany, sql, [tuple(p) for p in seq_of_params]
        )

    def _query(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    async def aquery(
        self, sql: str, params: tuple | list | dict = ()
    ) -> list[sqlite3.Row]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._query, sql, tuple(params))

    async def aquery_one(
        self, sql: str, params: tuple | list | dict = ()
    ) -> sqlite3.Row | None:
        rows = await self.aquery(sql, params)
        return rows[0] if rows else None

    @asynccontextmanager
    async def immediate_tx(self):
        """立即写事务（BEGIN IMMEDIATE；dd §3.3 get_for_update 等场景使用）。

        异常自动 ROLLBACK；调用方在 ``async with`` 内复用本对象执行语句。
        """
        async with self._tx_lock:
            await self.aexecute("BEGIN IMMEDIATE")
            try:
                yield self
                await self.aexecute("COMMIT")
            except BaseException:
                try:
                    await self.aexecute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def close(self) -> None:
        self._executor.shutdown(wait=True)
        self._conn.close()
