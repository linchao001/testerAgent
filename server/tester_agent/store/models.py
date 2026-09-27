"""DAO 与 DB 行类型（dd §2.9 §3.3）。

- ``*Row`` 为贴近表结构的 dataclass：JSON 列保持原始字符串，业务层不直接接触；
- Row 工厂/读取方法负责 ``Row ↔ Pydantic``（``tester_agent.domain``）转换；
- DAO 构造时注入 ``store.db.Database``（aexecute/aquery/immediate_tx）；
- 除按主键查单行外，业务查询强制首参 ``workspace_id`` / ``task_id`` 隔离。
分页统一 ``Page[T]``，游标 base64(``{last_id}|{last_ts}``)，按时间倒序键集翻页。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass, fields
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from .. import domain
from ..domain import (
    Candidate,
    ClauseRef,
    DegradedStep,
    ErrorInfo,
    InjectedItem,
    QueryVariant,
    RequirementRef,
    ReviewStatus,
)
from ..errors import NotFoundError, VersionConflict
from .db import Database, utcnow_iso

T = TypeVar("T")

_UNSET = object()
"""update_* 可选字段的"不修改"哨兵：显式传 None 表示写 NULL。"""


# ---------- 分页（dd §3.3） ----------


@dataclass
class Page(Generic[T]):
    items: list[T]
    next_cursor: str | None


def encode_cursor(last_id: str, last_ts: str) -> str:
    return base64.urlsafe_b64encode(f"{last_id}|{last_ts}".encode("utf-8")).decode(
        "ascii"
    ).rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    """解析游标；非法形态抛 ValueError（由 API 层翻译为 400，WP-25+）。"""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - 任何解码失败都视为非法游标
        raise ValueError("非法分页游标") from exc
    last_id, sep, last_ts = raw.partition("|")
    if not sep or not last_id:
        raise ValueError("非法分页游标")
    return last_id, last_ts


async def _fetch_page(
    db: Database,
    base_sql: str,
    params: list[Any],
    *,
    ts_col: str,
    limit: int,
    cursor: str | None,
    row_cls: type,
    qualify: str = "",
) -> Page:
    """键集分页：(ts_col, id) 倒序，多取一行判断是否有下一页。

    base_sql 必须已含 WHERE；params 与其占位符一一对应。
    JOIN 查询用 ``qualify``（如 ``"a."``）消歧排序列。
    """
    prefix = qualify
    sql = base_sql
    query_params = list(params)
    if cursor:
        last_id, last_ts = decode_cursor(cursor)
        sql += f" AND ({prefix}{ts_col}, {prefix}id) < (?, ?)"
        query_params.extend([last_ts, last_id])
    sql += f" ORDER BY {prefix}{ts_col} DESC, {prefix}id DESC LIMIT ?"
    query_params.append(limit + 1)
    rows = await db.aquery(sql, query_params)
    has_more = len(rows) > limit
    items = [row_cls.from_row(r) for r in rows[:limit]]
    next_cursor = (
        encode_cursor(items[-1].id, getattr(items[-1], ts_col))
        if has_more and items
        else None
    )
    return Page(items=items, next_cursor=next_cursor)


def _loads(raw: str | None, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    return json.loads(raw)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _enum_value(value: Any) -> str:
    return value.value if isinstance(value, ReviewStatus) else str(value)


# ---------- Row dataclass（dd §2.9：JSON 列保持原始字符串） ----------


@dataclass
class TaskRow:
    id: str
    conversation_id: str
    workspace_id: str
    status: str
    current_stage: str
    langgraph_thread_id: str
    graph_run_id: str
    requirement_ref: str = "{}"
    clauses: str = "[]"
    runner_heartbeat: str | None = None
    cancel_requested: int = 0
    snapshot_level: str = "meta"
    error_info: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TaskRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def create(
        cls,
        *,
        id: str,
        conversation_id: str,
        workspace_id: str,
        status: str,
        current_stage: str,
        langgraph_thread_id: str,
        graph_run_id: str,
        requirement: RequirementRef | None = None,
        clauses: list[ClauseRef] | None = None,
        error: ErrorInfo | None = None,
        snapshot_level: str = "meta",
        now: str | None = None,
    ) -> "TaskRow":
        ts = now or utcnow_iso()
        return cls(
            id=id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            status=str(status),
            current_stage=current_stage,
            langgraph_thread_id=langgraph_thread_id,
            graph_run_id=graph_run_id,
            requirement_ref=(
                _dumps(requirement.model_dump()) if requirement is not None else "{}"
            ),
            clauses=_dumps([c.model_dump() for c in (clauses or [])]),
            error_info=_dumps(error.model_dump()) if error is not None else None,
            snapshot_level=snapshot_level,
            created_at=ts,
            updated_at=ts,
        )

    def requirement_obj(self) -> RequirementRef | None:
        data = _loads(self.requirement_ref, {})
        return RequirementRef.model_validate(data) if data else None

    def clauses_obj(self) -> list[ClauseRef]:
        return [ClauseRef.model_validate(d) for d in _loads(self.clauses, [])]

    def error_obj(self) -> ErrorInfo | None:
        data = _loads(self.error_info, None)
        return ErrorInfo.model_validate(data) if data else None


@dataclass
class ArtifactRow:
    id: str
    task_id: str
    stage: str
    graph_run_id: str
    stage_version: int
    origin: str = "system"
    status: str = "active"
    payload: str = "{}"
    progress: str | None = None
    confirmed_by: str | None = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ArtifactRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def create(
        cls,
        *,
        id: str,
        task_id: str,
        stage: str,
        graph_run_id: str,
        stage_version: int,
        payload: dict | BaseModel | str | None = None,
        origin: str = "system",
        status: str = "active",
        progress: list[dict] | None = None,
        confirmed_by: str | None = None,
        now: str | None = None,
    ) -> "ArtifactRow":
        if payload is None:
            payload_json = "{}"
        elif isinstance(payload, str):
            payload_json = payload
        elif isinstance(payload, BaseModel):
            payload_json = _dumps(payload.model_dump())
        else:
            payload_json = _dumps(payload)
        return cls(
            id=id,
            task_id=task_id,
            stage=stage,
            graph_run_id=graph_run_id,
            stage_version=stage_version,
            origin=str(origin),
            status=str(status),
            payload=payload_json,
            progress=_dumps(progress) if progress is not None else None,
            confirmed_by=confirmed_by,
            created_at=now or utcnow_iso(),
        )

    def payload_dict(self) -> dict:
        return _loads(self.payload, {})

    def progress_list(self) -> list[dict]:
        return _loads(self.progress, [])


@dataclass
class CaseRow:
    id: str
    task_id: str
    point_id: str
    stage_version: int
    file_path: str
    content_hash: str
    title: str
    lineage: str = "{}"
    status: str = "active"
    review_status: str = "pending"
    trace_refs: str = "{}"
    error_info: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CaseRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def from_record(
        cls,
        task_id: str,
        record: domain.CaseRecord,
        *,
        error_code: str | None = None,
        now: str | None = None,
    ) -> "CaseRow":
        return cls(
            id=record.case_id,
            task_id=task_id,
            point_id=record.point_id,
            stage_version=record.stage_version,
            lineage=_dumps(record.lineage.model_dump()),
            status=record.status.value,
            review_status=record.review_status.value,
            file_path=record.file_path,
            content_hash=record.content_hash,
            title=record.title,
            trace_refs=_dumps(record.trace_refs.model_dump()),
            error_info=_dumps({"code": error_code}) if error_code is not None else None,
            created_at=now or utcnow_iso(),
            updated_at=now or utcnow_iso(),
        )

    def to_record(self) -> domain.CaseRecord:
        return domain.CaseRecord.model_validate(
            {
                "case_id": self.id,
                "point_id": self.point_id,
                "stage_version": self.stage_version,
                "lineage": _loads(self.lineage, {}),
                "status": self.status,
                "review_status": self.review_status,
                "file_path": self.file_path,
                "content_hash": self.content_hash,
                "title": self.title,
                "trace_refs": _loads(self.trace_refs, {}),
            }
        )

    def lineage_obj(self) -> domain.Lineage:
        return domain.Lineage.model_validate(_loads(self.lineage, {}))

    def trace_refs_obj(self) -> domain.TraceRefs:
        return domain.TraceRefs.model_validate(_loads(self.trace_refs, {}))

    def error_code(self) -> str | None:
        """对账标记 code（file_missing/hash_conflict 等，dd §3.1；标记形态区别于
        task.error_info 的 ErrorInfo，具体对账语义属 WP-29）。"""
        data = _loads(self.error_info, None)
        return data.get("code") if isinstance(data, dict) else None


# ---------- TaskDAO ----------


class TaskDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = (
        "id, conversation_id, workspace_id, status, current_stage, "
        "requirement_ref, clauses, langgraph_thread_id, graph_run_id, "
        "runner_heartbeat, cancel_requested, snapshot_level, error_info, "
        "created_at, updated_at"
    )

    async def create(self, task: TaskRow) -> None:
        if not task.created_at:
            task.created_at = task.updated_at or utcnow_iso()
        if not task.updated_at:
            task.updated_at = task.created_at
        await self._db.aexecute(
            "INSERT INTO task (" + self._COLUMNS + ") "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task.id,
                task.conversation_id,
                task.workspace_id,
                task.status,
                task.current_stage,
                task.requirement_ref,
                task.clauses,
                task.langgraph_thread_id,
                task.graph_run_id,
                task.runner_heartbeat,
                task.cancel_requested,
                task.snapshot_level,
                task.error_info,
                task.created_at,
                task.updated_at,
            ),
        )

    async def get(self, task_id: str) -> TaskRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM task WHERE id = ?", (task_id,)
        )
        if row is None:
            raise NotFoundError(f"任务不存在：{task_id}")
        return TaskRow.from_row(row)

    async def get_for_update(self, task_id: str) -> TaskRow:
        """行加锁语义读取：**必须在调用方的 ``db.immediate_tx()`` 事务内调用**
        （dd §3.3；回退落账见 §11.2）。"""
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM task WHERE id = ?", (task_id,)
        )
        if row is None:
            raise NotFoundError(f"任务不存在：{task_id}")
        return TaskRow.from_row(row)

    async def list_by_workspace(
        self,
        workspace_id: str,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> Page[TaskRow]:
        base = "SELECT " + self._COLUMNS + " FROM task WHERE workspace_id = ?"
        params: list[Any] = [workspace_id]
        if status is not None:
            base += " AND status = ?"
            params.append(str(status))
        return await _fetch_page(
            self._db,
            base,
            params,
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=TaskRow,
        )

    async def update_status(
        self,
        task_id: str,
        *,
        status: str,
        current_stage: str | None | object = _UNSET,
        error_info: dict | None | object = _UNSET,
        heartbeat: bool = False,
    ) -> None:
        """更新任务状态并刷新 updated_at。

        - current_stage/error_info 缺省（_UNSET）= 不改列；显式传 None = 置 NULL；
        - heartbeat=True 同时打 runner_heartbeat（dd §6.4 心跳循环）。
        """
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [str(status), utcnow_iso()]
        if current_stage is not _UNSET:
            sets.append("current_stage = ?")
            params.append(current_stage)
        if error_info is not _UNSET:
            sets.append("error_info = ?")
            params.append(_dumps(error_info) if error_info is not None else None)
        if heartbeat:
            sets.append("runner_heartbeat = ?")
            params.append(utcnow_iso())
        params.append(task_id)
        res = await self._db.aexecute(
            f"UPDATE task SET {', '.join(sets)} WHERE id = ?", params
        )
        if res.rowcount == 0:
            raise NotFoundError(f"任务不存在：{task_id}")

    async def request_cancel(self, task_id: str) -> None:
        res = await self._db.aexecute(
            "UPDATE task SET cancel_requested = 1, updated_at = ? WHERE id = ?",
            (utcnow_iso(), task_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"任务不存在：{task_id}")

    async def update_clauses(self, task_id: str, clauses: list[dict]) -> None:
        """intake 写条款索引（WP-16 增量方法，非 dd §3.3 冻结签名）。

        幂等重写：同输入同内容；clauses 为 ClauseRef 序列化 dict 列表（不含偏移）。
        """
        res = await self._db.aexecute(
            "UPDATE task SET clauses = ?, updated_at = ? WHERE id = ?",
            (_dumps(clauses), utcnow_iso(), task_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"任务不存在：{task_id}")

    async def is_cancel_requested(self, task_id: str) -> bool:
        row = await self._db.aquery_one(
            "SELECT cancel_requested FROM task WHERE id = ?", (task_id,)
        )
        if row is None:
            raise NotFoundError(f"任务不存在：{task_id}")
        return bool(row["cancel_requested"])

    async def heartbeat(self, task_id: str, graph_run_id: str) -> int:
        """心跳续期，返回受影响行数（dd §6.4：0 行表示已被 Reaper/取消改判，
        Runner 应在下个批次边界自杀）。"""
        now = utcnow_iso()
        res = await self._db.aexecute(
            "UPDATE task SET runner_heartbeat = ?, updated_at = ? "
            "WHERE id = ? AND graph_run_id = ?",
            (now, now, task_id, graph_run_id),
        )
        return res.rowcount

    async def list_stale_running(self, before: str) -> list[TaskRow]:
        """Reaper 候选：running/cancelling 且心跳为空或早于 ``before``（dd §6.5）。"""
        rows = await self._db.aquery(
            f"SELECT {self._COLUMNS} FROM task "
            "WHERE status IN ('running', 'cancelling') "
            "AND (runner_heartbeat IS NULL OR runner_heartbeat < ?) "
            "ORDER BY id",
            (before,),
        )
        return [TaskRow.from_row(r) for r in rows]

    async def start_new_run(
        self, task_id: str, *, graph_run_id: str, thread_id: str, stage: str
    ) -> None:
        """回退落账事务内调用（dd §11.2）：切换 run/thread、回退入口阶段、清取消标志。
        状态迁移（→running）由调用方经 update_status 在同事务完成。"""
        res = await self._db.aexecute(
            "UPDATE task SET graph_run_id = ?, langgraph_thread_id = ?, "
            "current_stage = ?, cancel_requested = 0, updated_at = ? WHERE id = ?",
            (graph_run_id, thread_id, stage, utcnow_iso(), task_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"任务不存在：{task_id}")


# ---------- ArtifactDAO ----------


class ArtifactDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = (
        "id, task_id, stage, graph_run_id, stage_version, origin, status, "
        "payload, progress, confirmed_by, created_at"
    )

    async def put(self, a: ArtifactRow) -> None:
        if not a.created_at:
            a.created_at = utcnow_iso()
        try:
            await self._db.aexecute(
                "INSERT INTO stage_artifact (" + self._COLUMNS + ") "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    a.id,
                    a.task_id,
                    a.stage,
                    a.graph_run_id,
                    a.stage_version,
                    a.origin,
                    a.status,
                    a.payload,
                    a.progress,
                    a.confirmed_by,
                    a.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # UNIQUE(task_id, stage, stage_version) 冲突 → 版本冲突（dd §17）
            if "UNIQUE" in str(exc).upper():
                raise VersionConflict(
                    f"阶段产物版本已存在：task={a.task_id} "
                    f"stage={a.stage} v{a.stage_version}",
                    details={"sql_error": str(exc)},
                ) from exc
            raise

    async def get_active(self, task_id: str, stage: str) -> ArtifactRow | None:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM stage_artifact "
            "WHERE task_id = ? AND stage = ? AND status = 'active' "
            "ORDER BY stage_version DESC LIMIT 1",
            (task_id, stage),
        )
        return ArtifactRow.from_row(row) if row else None

    async def get(self, artifact_id: str) -> ArtifactRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM stage_artifact WHERE id = ?",
            (artifact_id,),
        )
        if row is None:
            raise NotFoundError(f"阶段产物不存在：{artifact_id}")
        return ArtifactRow.from_row(row)

    async def list_active_chain(self, task_id: str) -> list[ArtifactRow]:
        """任务当前 active 产物链，按产生先后（created_at, id）升序。"""
        rows = await self._db.aquery(
            f"SELECT {self._COLUMNS} FROM stage_artifact "
            "WHERE task_id = ? AND status = 'active' "
            "ORDER BY created_at ASC, id ASC",
            (task_id,),
        )
        return [ArtifactRow.from_row(r) for r in rows]

    async def supersede(self, artifact_id: str) -> None:
        res = await self._db.aexecute(
            "UPDATE stage_artifact SET status = 'superseded' WHERE id = ?",
            (artifact_id,),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"阶段产物不存在：{artifact_id}")

    async def mark_obsolete(
        self, task_id: str, stages: list[str], graph_run_id: str
    ) -> int:
        """把指定 run 内、指定阶段的 active 产物批量置 obsolete，返回受影响行数。"""
        if not stages:
            return 0
        placeholders = ",".join("?" for _ in stages)
        res = await self._db.aexecute(
            f"UPDATE stage_artifact SET status = 'obsolete' "
            f"WHERE task_id = ? AND status = 'active' "
            f"AND graph_run_id = ? AND stage IN ({placeholders})",
            [task_id, graph_run_id, *stages],
        )
        return res.rowcount

    async def next_version(self, task_id: str, stage: str) -> int:
        """下一版本号 = MAX(stage_version)+1（含 superseded/obsolete，版本单调不复用）。

        与 put 组成 read-then-write，调用方必须持 ``immediate_tx`` 防并发交错。
        """
        row = await self._db.aquery_one(
            "SELECT COALESCE(MAX(stage_version), 0) + 1 AS v "
            "FROM stage_artifact WHERE task_id = ? AND stage = ?",
            (task_id, stage),
        )
        return int(row["v"])

    async def write_progress(self, artifact_id: str, batches: list[dict]) -> None:
        res = await self._db.aexecute(
            "UPDATE stage_artifact SET progress = ? WHERE id = ?",
            (_dumps(batches), artifact_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"阶段产物不存在：{artifact_id}")


# ---------- TestcaseDAO ----------


class TestcaseDAO:
    # 类名以 Test 开头，避免被 pytest 当作测试类收集
    __test__ = False

    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = (
        "id, task_id, point_id, stage_version, lineage, status, review_status, "
        "file_path, content_hash, title, trace_refs, error_info, created_at, updated_at"
    )

    async def put_batch(self, rows: list[CaseRow]) -> None:
        """批次幂等写入（INSERT OR IGNORE，dd §11.1）：重复 case_id 静默跳过，
        不产生重复行/异常。调用方通常包在 immediate_tx 内。"""
        if not rows:
            return
        params = []
        for r in rows:
            ts = utcnow_iso()
            # created_at 仅首次落库赋值；INSERT OR IGNORE 重放时旧行保留原值
            if not r.created_at:
                r.created_at = ts
            if not r.updated_at:
                r.updated_at = r.created_at
            params.append(
                (
                    r.id,
                    r.task_id,
                    r.point_id,
                    r.stage_version,
                    r.lineage,
                    r.status,
                    r.review_status,
                    r.file_path,
                    r.content_hash,
                    r.title,
                    r.trace_refs,
                    r.error_info,
                    r.created_at,
                    r.updated_at,
                )
            )
        await self._db.aexecutemany(
            "INSERT OR IGNORE INTO testcase (" + self._COLUMNS + ") "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            params,
        )

    async def get(self, case_id: str) -> CaseRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM testcase WHERE id = ?", (case_id,)
        )
        if row is None:
            raise NotFoundError(f"测试用例不存在：{case_id}")
        return CaseRow.from_row(row)

    async def list_by_task(
        self,
        task_id: str,
        *,
        status: str | None,
        review: str | None,
        version: int | None,
        cursor: str | None,
        limit: int,
    ) -> Page[CaseRow]:
        base = "SELECT " + self._COLUMNS + " FROM testcase WHERE task_id = ?"
        params: list[Any] = []
        if status is not None:
            base += " AND status = ?"
            params.append(str(status))
        if review is not None:
            base += " AND review_status = ?"
            params.append(str(review))
        if version is not None:
            base += " AND stage_version = ?"
            params.append(int(version))
        return await _fetch_page(
            self._db,
            base,
            [task_id, *params],
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=CaseRow,
        )

    async def update_content(
        self, case_id: str, *, file_path: str, content_hash: str, title: str
    ) -> None:
        res = await self._db.aexecute(
            "UPDATE testcase SET file_path = ?, content_hash = ?, title = ?, "
            "updated_at = ? WHERE id = ?",
            (file_path, content_hash, title, utcnow_iso(), case_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"测试用例不存在：{case_id}")

    async def update_review(self, case_id: str, status: ReviewStatus | str) -> None:
        res = await self._db.aexecute(
            "UPDATE testcase SET review_status = ?, updated_at = ? WHERE id = ?",
            (_enum_value(status), utcnow_iso(), case_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"测试用例不存在：{case_id}")

    async def mark_obsolete_by_version(
        self, task_id: str, versions: list[int]
    ) -> int:
        """把指定 stage_version 的 active 用例置 obsolete，返回受影响行数。"""
        if not versions:
            return 0
        placeholders = ",".join("?" for _ in versions)
        res = await self._db.aexecute(
            f"UPDATE testcase SET status = 'obsolete', updated_at = ? "
            f"WHERE task_id = ? AND status = 'active' "
            f"AND stage_version IN ({placeholders})",
            [utcnow_iso(), task_id, *(int(v) for v in versions)],
        )
        return res.rowcount

    async def mark_error(self, case_id: str, code: str) -> None:
        """对账标记（file_missing / hash_conflict 等，dd §3.1；Reconciler 见 WP-29）。"""
        res = await self._db.aexecute(
            "UPDATE testcase SET error_info = ?, updated_at = ? WHERE id = ?",
            (_dumps({"code": code}), utcnow_iso(), case_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"测试用例不存在：{case_id}")


# ======================================================================
# WP-04：其余行类型与 DAO（dd §3.1 §3.3；签名 §3.3 已冻结者严格照签）
# ======================================================================


# ---------- workspace ----------


@dataclass
class WorkspaceRow:
    id: str
    name: str
    description: str = ""
    kb_config: str = "{}"
    created_at: str = ""
    deleted_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WorkspaceRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def create(
        cls,
        *,
        id: str,
        name: str,
        description: str = "",
        kb_config: dict | None = None,
        now: str | None = None,
    ) -> "WorkspaceRow":
        return cls(
            id=id,
            name=name,
            description=description,
            kb_config=_dumps(kb_config or {}),
            created_at=now or utcnow_iso(),
        )

    def kb_config_obj(self) -> dict:
        return _loads(self.kb_config, {})


class WorkspaceDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = "id, name, description, kb_config, created_at, deleted_at"

    async def create(self, ws: WorkspaceRow) -> None:
        if not ws.created_at:
            ws.created_at = utcnow_iso()
        await self._db.aexecute(
            "INSERT INTO workspace (id, name, description, kb_config, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ws.id, ws.name, ws.description, ws.kb_config, ws.created_at),
        )

    async def get(self, workspace_id: str, *, include_deleted: bool = False) -> WorkspaceRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM workspace WHERE id = ?", (workspace_id,)
        )
        if row is None or (row["deleted_at"] is not None and not include_deleted):
            # 已软删与不存在同等待遇：不暴露存在性（dd §5.1，API 层映射 404）
            raise NotFoundError(f"工作区不存在：{workspace_id}")
        return WorkspaceRow.from_row(row)

    async def list(self, *, cursor: str | None, limit: int) -> Page[WorkspaceRow]:
        return await _fetch_page(
            self._db,
            "SELECT " + self._COLUMNS + " FROM workspace WHERE deleted_at IS NULL",
            [],
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=WorkspaceRow,
        )

    async def update(
        self,
        workspace_id: str,
        *,
        name: str | object = _UNSET,
        description: str | object = _UNSET,
        kb_config: dict | object = _UNSET,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not _UNSET:
            sets.append("name = ?")
            params.append(name)
        if description is not _UNSET:
            sets.append("description = ?")
            params.append(description)
        if kb_config is not _UNSET:
            sets.append("kb_config = ?")
            params.append(_dumps(kb_config))
        if not sets:
            return
        params.append(workspace_id)
        res = await self._db.aexecute(
            f"UPDATE workspace SET {', '.join(sets)} WHERE id = ? AND deleted_at IS NULL",
            params,
        )
        if res.rowcount == 0:
            raise NotFoundError(f"工作区不存在：{workspace_id}")

    async def soft_delete(self, workspace_id: str) -> None:
        res = await self._db.aexecute(
            "UPDATE workspace SET deleted_at = ? "
            "WHERE id = ? AND deleted_at IS NULL",
            (utcnow_iso(), workspace_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"工作区不存在或已删除：{workspace_id}")


# ---------- agent ----------


@dataclass
class AgentRow:
    id: str
    name: str
    agent_type: str
    config: str = "{}"
    builtin: int = 0
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "AgentRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def create(
        cls,
        *,
        id: str,
        name: str,
        agent_type: str = "case_designer",
        config: dict | None = None,
        builtin: bool = False,
        now: str | None = None,
    ) -> "AgentRow":
        return cls(
            id=id,
            name=name,
            agent_type=agent_type,
            config=_dumps(config or {}),
            builtin=1 if builtin else 0,
            created_at=now or utcnow_iso(),
        )

    def config_obj(self) -> dict:
        return _loads(self.config, {})


class AgentDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = "id, name, agent_type, config, builtin, created_at"

    async def create(self, agent: AgentRow) -> None:
        if not agent.created_at:
            agent.created_at = utcnow_iso()
        await self._db.aexecute(
            f"INSERT INTO agent ({self._COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            (
                agent.id,
                agent.name,
                agent.agent_type,
                agent.config,
                agent.builtin,
                agent.created_at,
            ),
        )

    async def get(self, agent_id: str) -> AgentRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM agent WHERE id = ?", (agent_id,)
        )
        if row is None:
            raise NotFoundError(f"智能体不存在：{agent_id}")
        return AgentRow.from_row(row)

    async def list(self, *, cursor: str | None, limit: int) -> Page[AgentRow]:
        return await _fetch_page(
            self._db,
            "SELECT " + self._COLUMNS + " FROM agent",
            [],
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=AgentRow,
        )

    async def update(
        self,
        agent_id: str,
        *,
        name: str | object = _UNSET,
        config: dict | object = _UNSET,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not _UNSET:
            sets.append("name = ?")
            params.append(name)
        if config is not _UNSET:
            sets.append("config = ?")
            params.append(_dumps(config))
        if not sets:
            return
        params.append(agent_id)
        res = await self._db.aexecute(
            f"UPDATE agent SET {', '.join(sets)} WHERE id = ?", params
        )
        if res.rowcount == 0:
            raise NotFoundError(f"智能体不存在：{agent_id}")

    async def delete(self, agent_id: str) -> None:
        """硬删（agent_workspace ON DELETE CASCADE 随之外键级联）。"""
        res = await self._db.aexecute("DELETE FROM agent WHERE id = ?", (agent_id,))
        if res.rowcount == 0:
            raise NotFoundError(f"智能体不存在：{agent_id}")

    async def bind(self, agent_id: str, workspace_id: str) -> None:
        # 幂等绑定：重复绑定静默成功
        await self._db.aexecute(
            "INSERT OR IGNORE INTO agent_workspace (agent_id, workspace_id) "
            "VALUES (?, ?)",
            (agent_id, workspace_id),
        )

    async def unbind(self, agent_id: str, workspace_id: str) -> None:
        await self._db.aexecute(
            "DELETE FROM agent_workspace WHERE agent_id = ? AND workspace_id = ?",
            (agent_id, workspace_id),
        )

    async def list_for_workspace(
        self, workspace_id: str, *, cursor: str | None, limit: int
    ) -> Page[AgentRow]:
        return await _fetch_page(
            self._db,
            "SELECT a.id, a.name, a.agent_type, a.config, a.builtin, a.created_at "
            "FROM agent a JOIN agent_workspace aw ON aw.agent_id = a.id "
            "WHERE aw.workspace_id = ?",
            [workspace_id],
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=AgentRow,
            qualify="a.",
        )


# ---------- conversation / message ----------


@dataclass
class ConversationRow:
    id: str
    workspace_id: str
    title: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ConversationRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def create(
        cls, *, id: str, workspace_id: str, title: str = "", now: str | None = None
    ) -> "ConversationRow":
        ts = now or utcnow_iso()
        return cls(id=id, workspace_id=workspace_id, title=title,
                   created_at=ts, updated_at=ts)


class ConversationDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = "id, workspace_id, title, created_at, updated_at"

    async def create(self, conv: ConversationRow) -> None:
        if not conv.created_at:
            conv.created_at = conv.updated_at or utcnow_iso()
        if not conv.updated_at:
            conv.updated_at = conv.created_at
        await self._db.aexecute(
            f"INSERT INTO conversation ({self._COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            (conv.id, conv.workspace_id, conv.title, conv.created_at, conv.updated_at),
        )

    async def get(self, conversation_id: str) -> ConversationRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM conversation WHERE id = ?",
            (conversation_id,),
        )
        if row is None:
            raise NotFoundError(f"会话不存在：{conversation_id}")
        return ConversationRow.from_row(row)

    async def list_by_workspace(
        self, workspace_id: str, *, cursor: str | None, limit: int
    ) -> Page[ConversationRow]:
        return await _fetch_page(
            self._db,
            "SELECT " + self._COLUMNS + " FROM conversation WHERE workspace_id = ?",
            [workspace_id],
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=ConversationRow,
        )

    async def touch(self, conversation_id: str) -> None:
        """有新消息时刷新 updated_at（dd §5.2 发送消息路径）。"""
        res = await self._db.aexecute(
            "UPDATE conversation SET updated_at = ? WHERE id = ?",
            (utcnow_iso(), conversation_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"会话不存在：{conversation_id}")


@dataclass
class MessageRow:
    id: str
    conversation_id: str
    role: str
    kind: str
    content: str
    task_id: str | None = None
    ref_artifact_id: str | None = None
    payload: str = "{}"
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MessageRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def create(
        cls,
        *,
        id: str,
        conversation_id: str,
        role: str,
        kind: str,
        content: str,
        task_id: str | None = None,
        ref_artifact_id: str | None = None,
        payload: dict | None = None,
        now: str | None = None,
    ) -> "MessageRow":
        return cls(
            id=id,
            conversation_id=conversation_id,
            task_id=task_id,
            role=str(role),
            kind=str(kind),
            content=content,
            ref_artifact_id=ref_artifact_id,
            payload=_dumps(payload or {}),
            created_at=now or utcnow_iso(),
        )

    def payload_dict(self) -> dict:
        return _loads(self.payload, {})


class MessageDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = (
        "id, conversation_id, task_id, role, kind, content, "
        "ref_artifact_id, payload, created_at"
    )

    async def put(self, msg: MessageRow) -> None:
        if not msg.created_at:
            msg.created_at = utcnow_iso()
        await self._db.aexecute(
            f"INSERT INTO message ({self._COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg.id,
                msg.conversation_id,
                msg.task_id,
                msg.role,
                msg.kind,
                msg.content,
                msg.ref_artifact_id,
                msg.payload,
                msg.created_at,
            ),
        )

    async def get(self, message_id: str) -> MessageRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM message WHERE id = ?", (message_id,)
        )
        if row is None:
            raise NotFoundError(f"消息不存在：{message_id}")
        return MessageRow.from_row(row)

    async def list_by_conversation(
        self, conversation_id: str, *, cursor: str | None, limit: int
    ) -> Page[MessageRow]:
        return await _fetch_page(
            self._db,
            "SELECT " + self._COLUMNS + " FROM message WHERE conversation_id = ?",
            [conversation_id],
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=MessageRow,
        )

    async def list_by_task(
        self, task_id: str, *, kind: str | None = None
    ) -> list[MessageRow]:
        """任务维度消息查询（WP-16 增量方法，非 dd §3.3 冻结签名）。

        供节点侧恢复判定等少量消费（澄清消息量小，不分页）；created_at 升序。
        """
        sql = "SELECT " + self._COLUMNS + " FROM message WHERE task_id = ?"
        params: list[Any] = [task_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at ASC, id ASC"
        rows = await self._db.aquery(sql, params)
        return [MessageRow.from_row(r) for r in rows]

    async def update_payload(self, message_id: str, payload: dict) -> None:
        """整写 payload JSON（WP-16 增量方法：澄清问答回填用）。"""
        res = await self._db.aexecute(
            "UPDATE message SET payload = ? WHERE id = ?",
            (_dumps(payload), message_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"消息不存在：{message_id}")


# ---------- retrieval_trace ----------


@dataclass
class TraceRow:
    id: str
    task_id: str
    graph_run_id: str
    stage: str
    stage_version: int
    node: str
    query_variant: str = "{}"
    candidates: str = "[]"
    injected_ids: str = "[]"
    referenced_ids: str = "[]"
    hallucinated_ids: str = "[]"
    weak_ref_ids: str = "[]"
    degraded: str = "[]"
    batch_id: str | None = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TraceRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def create(
        cls,
        *,
        id: str,
        task_id: str,
        graph_run_id: str,
        stage: str,
        stage_version: int,
        node: str,
        query_variant: QueryVariant | dict | None = None,
        candidates: list[Candidate] | None = None,
        injected_ids: list[str] | None = None,
        batch_id: str | None = None,
        degraded: list[DegradedStep] | None = None,
        now: str | None = None,
    ) -> "TraceRow":
        return cls(
            id=id,
            task_id=task_id,
            graph_run_id=graph_run_id,
            stage=stage,
            stage_version=stage_version,
            node=node,
            batch_id=batch_id,
            query_variant=_dumps(
                query_variant.model_dump()
                if isinstance(query_variant, QueryVariant)
                else (query_variant or {})
            ),
            candidates=_dumps([c.model_dump() for c in (candidates or [])]),
            injected_ids=_dumps(injected_ids or []),
            degraded=_dumps([d.model_dump() for d in (degraded or [])]),
            created_at=now or utcnow_iso(),
        )

    def query_obj(self) -> QueryVariant | None:
        data = _loads(self.query_variant, {})
        return QueryVariant.model_validate(data) if data else None

    def candidates_obj(self) -> list[Candidate]:
        return [Candidate.model_validate(d) for d in _loads(self.candidates, [])]

    def injected_ids_obj(self) -> list[str]:
        return _loads(self.injected_ids, [])

    def referenced_ids_obj(self) -> list[str]:
        return _loads(self.referenced_ids, [])

    def hallucinated_ids_obj(self) -> list[str]:
        return _loads(self.hallucinated_ids, [])

    def weak_ref_ids_obj(self) -> list[str]:
        return _loads(self.weak_ref_ids, [])

    def degraded_obj(self) -> list[DegradedStep]:
        return [DegradedStep.model_validate(d) for d in _loads(self.degraded, [])]


class TraceDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = (
        "id, task_id, graph_run_id, stage, stage_version, node, batch_id, "
        "query_variant, candidates, injected_ids, referenced_ids, "
        "hallucinated_ids, weak_ref_ids, degraded, created_at"
    )

    async def append(self, row: TraceRow) -> str:
        if not row.created_at:
            row.created_at = utcnow_iso()
        await self._db.aexecute(
            f"INSERT INTO retrieval_trace ({self._COLUMNS}) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.id, row.task_id, row.graph_run_id, row.stage,
                row.stage_version, row.node, row.batch_id, row.query_variant,
                row.candidates, row.injected_ids, row.referenced_ids,
                row.hallucinated_ids, row.weak_ref_ids, row.degraded, row.created_at,
            ),
        )
        return row.id

    async def update_referenced(
        self,
        trace_id: str,
        referenced: list[str],
        weak: list[str],
        hallucinated: list[str],
    ) -> None:
        """生成后引用闭环回填（dd §3.3/§8.6）。"""
        res = await self._db.aexecute(
            "UPDATE retrieval_trace SET referenced_ids = ?, weak_ref_ids = ?, "
            "hallucinated_ids = ? WHERE id = ?",
            (_dumps(referenced), _dumps(weak), _dumps(hallucinated), trace_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"检索轨迹不存在：{trace_id}")

    async def append_degraded(
        self, trace_id: str, steps: list[DegradedStep | dict]
    ) -> None:
        """把生成侧降级步骤并入 trace.degraded（WP-17 新增契约面）。

        dd §3.3 冻结三方法之外的增量方法（WP-16 update_clauses/list_by_task
        增量先例）：白名单降级（§7.5②）发生在管线 append 之后，只能 read-
        merge-write 同一行；单 worker + Runner 重入锁（WP-22）下无并发写。
        trace 不存在 → NotFoundError；空列表 no-op。
        """
        if not steps:
            return
        row = await self._db.aquery_one(
            "SELECT degraded FROM retrieval_trace WHERE id = ?", (trace_id,)
        )
        if row is None:
            raise NotFoundError(f"检索轨迹不存在：{trace_id}")
        current: list = _loads(row["degraded"], [])
        for s in steps:
            current.append(s.model_dump() if isinstance(s, BaseModel) else s)
        await self._db.aexecute(
            "UPDATE retrieval_trace SET degraded = ? WHERE id = ?",
            (_dumps(current), trace_id),
        )

    async def list_by_task(
        self,
        task_id: str,
        *,
        stage: str | None,
        version: int | None,
        cursor: str | None,
        limit: int,
    ) -> Page[TraceRow]:
        base = (
            "SELECT " + self._COLUMNS + " FROM retrieval_trace WHERE task_id = ?"
        )
        params: list[Any] = []
        if stage is not None:
            base += " AND stage = ?"
            params.append(stage)
        if version is not None:
            base += " AND stage_version = ?"
            params.append(int(version))
        return await _fetch_page(
            self._db,
            base,
            [task_id, *params],
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=TraceRow,
        )

    async def get(self, trace_id: str) -> TraceRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM retrieval_trace WHERE id = ?", (trace_id,)
        )
        if row is None:
            raise NotFoundError(f"检索轨迹不存在：{trace_id}")
        return TraceRow.from_row(row)


# ---------- context_snapshot ----------


@dataclass
class SnapshotRow:
    id: str
    task_id: str
    graph_run_id: str
    stage: str
    stage_version: int
    node: str
    prompt_template_ver: str
    items: str = "[]"
    snapshot_path: str | None = None
    total_tokens_est: int = 0
    budget: int | None = None
    truncated: int = 0
    model_ref: str = "{}"
    usage: str = "{}"
    latencies: str = "{}"
    batch_id: str | None = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SnapshotRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def create(
        cls,
        *,
        id: str,
        task_id: str,
        graph_run_id: str,
        stage: str,
        stage_version: int,
        node: str,
        prompt_template_ver: str,
        items: list[InjectedItem] | None = None,
        snapshot_path: str | None = None,
        total_tokens_est: int = 0,
        budget: int | None = None,
        truncated: bool = False,
        model_ref: dict | None = None,
        usage: dict | None = None,
        latencies: dict | None = None,
        batch_id: str | None = None,
        now: str | None = None,
    ) -> "SnapshotRow":
        return cls(
            id=id,
            task_id=task_id,
            graph_run_id=graph_run_id,
            stage=stage,
            stage_version=stage_version,
            node=node,
            batch_id=batch_id,
            items=_dumps([i.model_dump() for i in (items or [])]),
            snapshot_path=snapshot_path,
            total_tokens_est=total_tokens_est,
            budget=budget,
            truncated=1 if truncated else 0,
            prompt_template_ver=prompt_template_ver,
            model_ref=_dumps(model_ref or {}),
            usage=_dumps(usage or {}),
            latencies=_dumps(latencies or {}),
            created_at=now or utcnow_iso(),
        )

    def items_obj(self) -> list[InjectedItem]:
        return [InjectedItem.model_validate(d) for d in _loads(self.items, [])]

    def model_ref_obj(self) -> dict:
        return _loads(self.model_ref, {})

    def usage_obj(self) -> dict:
        return _loads(self.usage, {})

    def latencies_obj(self) -> dict:
        return _loads(self.latencies, {})


class SnapshotDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = (
        "id, task_id, graph_run_id, stage, stage_version, node, batch_id, "
        "items, snapshot_path, total_tokens_est, budget, truncated, "
        "prompt_template_ver, model_ref, usage, latencies, created_at"
    )

    async def put(self, row: SnapshotRow) -> str:
        if not row.created_at:
            row.created_at = utcnow_iso()
        await self._db.aexecute(
            f"INSERT INTO context_snapshot ({self._COLUMNS}) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.id, row.task_id, row.graph_run_id, row.stage,
                row.stage_version, row.node, row.batch_id, row.items,
                row.snapshot_path, row.total_tokens_est, row.budget,
                row.truncated, row.prompt_template_ver, row.model_ref,
                row.usage, row.latencies, row.created_at,
            ),
        )
        return row.id

    async def list_by_task(
        self, task_id: str, *, cursor: str | None, limit: int
    ) -> Page[SnapshotRow]:
        return await _fetch_page(
            self._db,
            "SELECT " + self._COLUMNS + " FROM context_snapshot WHERE task_id = ?",
            [task_id],
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=SnapshotRow,
        )

    async def get(self, snapshot_id: str) -> SnapshotRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM context_snapshot WHERE id = ?",
            (snapshot_id,),
        )
        if row is None:
            raise NotFoundError(f"快照不存在：{snapshot_id}")
        return SnapshotRow.from_row(row)


# ---------- task_event ----------


@dataclass
class EventRow:
    id: int
    task_id: str
    type: str
    payload: str = "{}"
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "EventRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    def payload_dict(self) -> dict:
        return _loads(self.payload, {})


class EventDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = "id, task_id, type, payload, created_at"
    _INSERT_COLUMNS = "task_id, type, payload, created_at"

    async def append(self, task_id: str, type_: str, payload: dict) -> int:
        """落事件并返回自增 id（先落库后广播，dd §6.2；SSE 补发以此为序）。"""
        res = await self._db.aexecute(
            f"INSERT INTO task_event ({self._INSERT_COLUMNS}) "
            "VALUES (?, ?, ?, ?)",
            (task_id, type_, _dumps(payload or {}), utcnow_iso()),
        )
        return int(res.lastrowid)

    async def list_after(
        self, task_id: str, after_id: int, limit: int = 500
    ) -> list[EventRow]:
        rows = await self._db.aquery(
            f"SELECT {self._COLUMNS} FROM task_event "
            "WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (task_id, int(after_id), int(limit)),
        )
        return [EventRow.from_row(r) for r in rows]

    async def purge_before(self, before: str) -> int:
        """惰性清理 created_at < before 的事件（保留期 events_days，dd §6.5）。"""
        res = await self._db.aexecute(
            "DELETE FROM task_event WHERE created_at < ?", (before,)
        )
        return res.rowcount


# ---------- review_record ----------


@dataclass
class ReviewRow:
    id: str
    task_id: str
    testcase_id: str
    action: str
    detail: str = "{}"
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ReviewRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def create(
        cls,
        *,
        id: str,
        task_id: str,
        testcase_id: str,
        action: str,
        detail: dict | None = None,
        now: str | None = None,
    ) -> "ReviewRow":
        return cls(
            id=id,
            task_id=task_id,
            testcase_id=testcase_id,
            action=action,
            detail=_dumps(detail or {}),
            created_at=now or utcnow_iso(),
        )

    def detail_dict(self) -> dict:
        return _loads(self.detail, {})


class ReviewDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = "id, task_id, testcase_id, action, detail, created_at"

    async def append(self, row: ReviewRow) -> str:
        if not row.created_at:
            row.created_at = utcnow_iso()
        await self._db.aexecute(
            f"INSERT INTO review_record ({self._COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
            (row.id, row.task_id, row.testcase_id, row.action, row.detail,
             row.created_at),
        )
        return row.id

    async def get(self, review_id: str) -> ReviewRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM review_record WHERE id = ?", (review_id,)
        )
        if row is None:
            raise NotFoundError(f"评审记录不存在：{review_id}")
        return ReviewRow.from_row(row)

    async def list_by_task(
        self, task_id: str, *, cursor: str | None, limit: int
    ) -> Page[ReviewRow]:
        return await _fetch_page(
            self._db,
            "SELECT " + self._COLUMNS + " FROM review_record WHERE task_id = ?",
            [task_id],
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=ReviewRow,
        )


# ---------- kb_proposal ----------


@dataclass
class ProposalRow:
    id: str
    workspace_id: str
    expires_at: str
    task_id: str | None = None
    payload: str = "{}"
    status: str = "pending"
    confirm_token_hash: str | None = None
    idempotency_key: str | None = None
    fail_count: int = 0
    confirmed_at: str | None = None
    write_result: str | None = None
    created_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ProposalRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    @classmethod
    def create(
        cls,
        *,
        id: str,
        workspace_id: str,
        expires_at: str,
        task_id: str | None = None,
        payload: dict | None = None,
        status: str = "pending",
        confirm_token_hash: str | None = None,
        idempotency_key: str | None = None,
        now: str | None = None,
    ) -> "ProposalRow":
        return cls(
            id=id,
            workspace_id=workspace_id,
            task_id=task_id,
            payload=_dumps(payload or {}),
            status=str(status),
            confirm_token_hash=confirm_token_hash,
            idempotency_key=idempotency_key,
            expires_at=expires_at,
            created_at=now or utcnow_iso(),
        )

    def payload_dict(self) -> dict:
        return _loads(self.payload, {})

    def write_result_dict(self) -> dict | None:
        return _loads(self.write_result, None) if self.write_result else None


class ProposalDAO:
    def __init__(self, db: Database):
        self._db = db

    _COLUMNS = (
        "id, workspace_id, task_id, payload, status, confirm_token_hash, "
        "idempotency_key, fail_count, confirmed_at, expires_at, write_result, "
        "created_at"
    )

    async def create(self, row: ProposalRow) -> None:
        if not row.created_at:
            row.created_at = utcnow_iso()
        try:
            await self._db.aexecute(
                f"INSERT INTO kb_proposal ({self._COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.id, row.workspace_id, row.task_id, row.payload, row.status,
                    row.confirm_token_hash, row.idempotency_key, row.fail_count,
                    row.confirmed_at, row.expires_at, row.write_result, row.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc).upper():
                raise VersionConflict(
                    f"提案幂等键冲突：{row.idempotency_key}",
                    details={"sql_error": str(exc)},
                ) from exc
            raise

    async def get(self, proposal_id: str) -> ProposalRow:
        row = await self._db.aquery_one(
            f"SELECT {self._COLUMNS} FROM kb_proposal WHERE id = ?", (proposal_id,)
        )
        if row is None:
            raise NotFoundError(f"知识库提案不存在：{proposal_id}")
        return ProposalRow.from_row(row)

    async def list_by_workspace(
        self,
        workspace_id: str,
        *,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> Page[ProposalRow]:
        base = (
            "SELECT " + self._COLUMNS + " FROM kb_proposal WHERE workspace_id = ?"
        )
        params: list[Any] = []
        if status is not None:
            base += " AND status = ?"
            params.append(str(status))
        return await _fetch_page(
            self._db,
            base,
            [workspace_id, *params],
            ts_col="created_at",
            limit=limit,
            cursor=cursor,
            row_cls=ProposalRow,
        )

    async def increment_fail_count(self, proposal_id: str) -> int:
        """ReMe 写失败计数 +1，返回最新次数（dd §11.4：行保持 pending 可重试）。"""
        res = await self._db.aexecute(
            "UPDATE kb_proposal SET fail_count = fail_count + 1 WHERE id = ?",
            (proposal_id,),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"知识库提案不存在：{proposal_id}")
        return (await self.get(proposal_id)).fail_count

    async def mark_confirmed(
        self, proposal_id: str, write_result: dict
    ) -> None:
        """终态：confirmed + write_result（verified=False 时由调用方在
        write_result 内置 needs_manual_check，dd §11.4）。"""
        res = await self._db.aexecute(
            "UPDATE kb_proposal SET status = 'confirmed', confirmed_at = ?, "
            "write_result = ? WHERE id = ?",
            (utcnow_iso(), _dumps(write_result), proposal_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"知识库提案不存在：{proposal_id}")

    async def set_status(self, proposal_id: str, status: str) -> None:
        """置 rejected/expired 等终态（保留期惰性清理/显式拒绝，WP-28/WP-29）。"""
        res = await self._db.aexecute(
            "UPDATE kb_proposal SET status = ? WHERE id = ?",
            (str(status), proposal_id),
        )
        if res.rowcount == 0:
            raise NotFoundError(f"知识库提案不存在：{proposal_id}")


# ---------- config（单行 id=1） ----------


@dataclass
class ConfigRow:
    id: int = 1
    model_config: str = "{}"
    runtime_config: str = "{}"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ConfigRow":
        return cls(**{f.name: row[f.name] for f in fields(cls)})

    def model_dict(self) -> dict:
        return _loads(self.model_config, {})

    def runtime_dict(self) -> dict:
        return _loads(self.runtime_config, {})


class ConfigDAO:
    def __init__(self, db: Database):
        self._db = db

    async def get(self) -> ConfigRow:
        """引导行由 001 DDL 内置插入；缺失视为库未初始化。"""
        row = await self._db.aquery_one(
            "SELECT id, model_config, runtime_config FROM config WHERE id = 1"
        )
        if row is None:
            raise NotFoundError("配置不存在：app.db 尚未初始化（请先 init-db）")
        return ConfigRow.from_row(row)

    async def update_model(self, model_config: dict) -> None:
        res = await self._db.aexecute(
            "UPDATE config SET model_config = ? WHERE id = 1",
            (_dumps(model_config),),
        )
        if res.rowcount == 0:
            raise NotFoundError("配置不存在：app.db 尚未初始化（请先 init-db）")

    async def update_runtime(self, runtime_config: dict) -> None:
        res = await self._db.aexecute(
            "UPDATE config SET runtime_config = ? WHERE id = 1",
            (_dumps(runtime_config),),
        )
        if res.rowcount == 0:
            raise NotFoundError("配置不存在：app.db 尚未初始化（请先 init-db）")
