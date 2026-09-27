"""WP-03 验收：TaskDAO / ArtifactDAO / TestcaseDAO。

覆盖：CRUD、workspace 强制隔离、游标分页（无重无漏/同时间戳 tiebreak）、
状态更新（哨兵不覆盖/显式 None 清空/心跳）、取消标志、Reaper 陈旧候选、
next_version、版本冲突、supersede/obsolete、progress 写入、
put_batch(INSERT OR IGNORE) 重放幂等、评审/内容更新、Row↔Pydantic 转换。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.domain import (
    CaseRecord,
    CaseStatus,
    ClauseRef,
    ErrorInfo,
    Lineage,
    ReviewStatus,
    TraceRefs,
)
from tester_agent.errors import NotFoundError, VersionConflict
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ArtifactDAO,
    ArtifactRow,
    CaseRow,
    Page,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
    decode_cursor,
    encode_cursor,
)

TS0 = "2026-09-26T00:00:00.000Z"
STAGE_LINK = "link_identify"
STAGE_POINT = "point_write"


# ---------- 夹具 ----------


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "app.db"
    run_migrations(db_path)
    handle = Database(db_path)
    yield handle
    handle.close()


async def _seed_workspace(db: Database, ws: str = "ws1") -> None:
    await db.aexecute(
        "INSERT INTO workspace (id, name, created_at) VALUES (?, ?, ?)",
        (ws, f"name-{ws}", TS0),
    )


async def _seed_conversation(
    db: Database, conv: str = "conv1", ws: str = "ws1"
) -> None:
    await db.aexecute(
        "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (conv, ws, "", TS0, TS0),
    )


async def _seed_ws_conv(
    db: Database, ws: str = "ws1", conv: str = "conv1"
) -> None:
    await _seed_workspace(db, ws)
    await _seed_conversation(db, conv, ws)


def _task_row(
    task_id: str = "t1",
    *,
    ws: str = "ws1",
    conv: str = "conv1",
    status: str = "running",
    stage: str = STAGE_LINK,
    run: str = "run-1",
    thread: str = "thread-1",
    now: str = TS0,
    clauses: list[ClauseRef] | None = None,
    error: ErrorInfo | None = None,
) -> TaskRow:
    return TaskRow.create(
        id=task_id,
        conversation_id=conv,
        workspace_id=ws,
        status=status,
        current_stage=stage,
        langgraph_thread_id=thread,
        graph_run_id=run,
        clauses=clauses,
        error=error,
        now=now,
    )


def _case_record(
    case_id: str = "c1",
    *,
    point_id: str = "pt-1-1",
    version: int = 1,
    root_case_id: str | None = None,
    status: CaseStatus = CaseStatus.ACTIVE,
    review: ReviewStatus = ReviewStatus.PENDING,
) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        point_id=point_id,
        stage_version=version,
        lineage=Lineage(root_case_id=root_case_id or case_id),
        status=status,
        review_status=review,
        file_path=f"workspaces/ws1/tasks/t1/v{version}/{case_id}.md",
        content_hash=f"hash-{case_id}",
        title=f"用例-{case_id}",
        trace_refs=TraceRefs(
            clause_ids=["h2-1"], entry_ids=["e1"], point_ids=[point_id]
        ),
    )


# ---------- 游标编解码 ----------


class TestCursor:
    def test_encode_decode_roundtrip(self):
        cur = encode_cursor("id-x", "2026-09-26T10:00:00.000Z")
        assert decode_cursor(cur) == ("id-x", "2026-09-26T10:00:00.000Z")

    @pytest.mark.parametrize("bad", ["!!!notbase64", "bm9waXBl", ""])
    def test_decode_rejects_garbage(self, bad):
        with pytest.raises(ValueError):
            decode_cursor(bad)


# ---------- TaskDAO ----------


class TestTaskDAO:
    async def test_create_and_get_roundtrip(self, db):
        await _seed_ws_conv(db)
        clauses = [
            ClauseRef(
                clause_id="h2-1-h3-2",
                level=3,
                title_path=["支付", "退款", "超时"],
                anchor="退款超过两小时未到账",
                text_hash="abc123",
            )
        ]
        err = ErrorInfo(code="LLM_TIMEOUT", message="上游超时", retryable=True, node="point_write")
        row = _task_row(clauses=clauses, error=err)
        await TaskDAO(db).create(row)

        got = await TaskDAO(db).get("t1")
        assert got.id == "t1"
        assert got.workspace_id == "ws1"
        assert got.status == "running"
        assert got.cancel_requested == 0
        assert got.snapshot_level == "meta"
        assert got.created_at == TS0
        # Row↔Pydantic
        restored = got.clauses_obj()
        assert restored == clauses
        assert got.error_obj() == err
        assert got.requirement_obj() is None

    async def test_get_missing_raises_not_found(self, db):
        with pytest.raises(NotFoundError):
            await TaskDAO(db).get("ghost")

    async def test_get_for_update_reads_within_immediate_tx(self, db):
        await _seed_ws_conv(db)
        await TaskDAO(db).create(_task_row())
        dao = TaskDAO(db)
        async with db.immediate_tx():
            row = await dao.get_for_update("t1")
            await dao.update_status(
                "t1", status="waiting_confirm", current_stage=STAGE_LINK
            )
            assert row.id == "t1"
        reloaded = await dao.get("t1")
        assert reloaded.status == "waiting_confirm"

    async def test_get_for_update_missing_raises(self, db):
        async with db.immediate_tx():
            with pytest.raises(NotFoundError):
                await TaskDAO(db).get_for_update("ghost")

    async def test_list_workspace_isolation(self, db):
        await _seed_ws_conv(db, "ws1", "conv1")
        await _seed_ws_conv(db, "ws2", "conv2")
        await TaskDAO(db).create(_task_row("t1", ws="ws1", conv="conv1"))
        await TaskDAO(db).create(_task_row("t2", ws="ws2", conv="conv2"))

        page = await TaskDAO(db).list_by_workspace(
            "ws1", status=None, cursor=None, limit=50
        )
        assert [t.id for t in page.items] == ["t1"]
        assert page.next_cursor is None

    async def test_list_status_filter_and_pagination_no_dup_no_gap(self, db):
        await _seed_ws_conv(db)
        # 5 条 running + 2 条 failed，created_at 互不相同（倒序键集）
        for i in range(5):
            await TaskDAO(db).create(
                _task_row(
                    f"run-{i}", now=f"2026-09-26T00:00:0{i}.000Z", status="running"
                )
            )
        for i in range(2):
            await TaskDAO(db).create(
                _task_row(
                    f"fail-{i}", now=f"2026-09-26T00:00:1{i}.000Z", status="failed"
                )
            )

        dao = TaskDAO(db)
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page: Page[TaskRow] = await dao.list_by_workspace(
                "ws1", status="running", cursor=cursor, limit=2
            )
            seen.extend(t.id for t in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        # 无重无漏，仅 running；created_at 倒序
        assert sorted(seen) == sorted(f"run-{i}" for i in range(5))
        assert len(seen) == len(set(seen))
        assert seen == sorted(seen, key=lambda x: int(x.split("-")[1]), reverse=True)

    async def test_list_pagination_tie_timestamp_tiebreak_by_id(self, db):
        await _seed_ws_conv(db)
        # 同一 created_at 的两条，仍须靠 id 次序稳定翻出
        await TaskDAO(db).create(_task_row("aaa", now=TS0))
        await TaskDAO(db).create(_task_row("bbb", now=TS0))

        dao = TaskDAO(db)
        page1 = await dao.list_by_workspace("ws1", status=None, cursor=None, limit=1)
        assert [t.id for t in page1.items] == ["bbb"]
        page2 = await dao.list_by_workspace(
            "ws1", status=None, cursor=page1.next_cursor, limit=1
        )
        assert [t.id for t in page2.items] == ["aaa"]
        assert page2.next_cursor is None

    async def test_update_status_semantics(self, db):
        await _seed_ws_conv(db)
        await TaskDAO(db).create(
            _task_row(error=ErrorInfo(code="X", message="m", retryable=False))
        )
        dao = TaskDAO(db)

        # 只改 status：current_stage / error_info 不被覆盖
        await dao.update_status("t1", status="waiting_confirm")
        row = await dao.get("t1")
        assert row.status == "waiting_confirm"
        assert row.current_stage == STAGE_LINK
        assert row.error_info is not None

        # 显式 None 清空 error_info；同时切阶段
        await dao.update_status(
            "t1", status="running", current_stage=STAGE_POINT, error_info=None
        )
        row = await dao.get("t1")
        assert row.current_stage == STAGE_POINT
        assert row.error_info is None

        # dict 写入 error_info + 心跳时间戳
        await dao.update_status(
            "t1",
            status="failed",
            error_info={"code": "INTERRUPTED_BY_RESTART", "message": "重启", "retryable": True},
            heartbeat=True,
        )
        row = await dao.get("t1")
        assert row.status == "failed"
        assert json.loads(row.error_info)["code"] == "INTERRUPTED_BY_RESTART"
        assert row.runner_heartbeat is not None

    async def test_update_status_missing_raises(self, db):
        with pytest.raises(NotFoundError):
            await TaskDAO(db).update_status("ghost", status="running")

    async def test_request_and_check_cancel(self, db):
        await _seed_ws_conv(db)
        dao = TaskDAO(db)
        await dao.create(_task_row())
        assert await dao.is_cancel_requested("t1") is False
        await dao.request_cancel("t1")
        assert await dao.is_cancel_requested("t1") is True
        with pytest.raises(NotFoundError):
            await dao.request_cancel("ghost")

    async def test_heartbeat_zero_rows_for_stale_run(self, db):
        await _seed_ws_conv(db)
        dao = TaskDAO(db)
        await dao.create(_task_row(run="run-1"))
        assert await dao.heartbeat("t1", "run-1") == 1
        got = await dao.get("t1")
        assert got.runner_heartbeat is not None
        # graph_run_id 已切换（回退/重跑）→ 旧 Runner 心跳 0 行，须自杀（dd §6.4）
        assert await dao.heartbeat("t1", "run-old") == 0

    async def test_list_stale_running(self, db):
        await _seed_ws_conv(db)
        dao = TaskDAO(db)
        await dao.create(_task_row("fresh", run="r", now=TS0))
        await dao.heartbeat("fresh", "r")  # 新鲜心跳
        await dao.create(
            _task_row("nohb", run="r", status="running", now=TS0)
        )  # 从无心跳
        await dao.create(
            _task_row("oldhb", run="r", status="cancelling", now=TS0)
        )
        # 手工打一个陈旧心跳
        await db.aexecute(
            "UPDATE task SET runner_heartbeat = ? WHERE id = ?",
            ("2026-09-25T00:00:00.000Z", "oldhb"),
        )
        await dao.create(
            _task_row("done", run="r", status="completed", now=TS0)
        )

        stale = await dao.list_stale_running("2026-09-26T00:00:00.000Z")
        ids = {t.id for t in stale}
        assert ids == {"nohb", "oldhb"}  # fresh 排除、completed 排除

    async def test_start_new_run_switches_identity_and_clears_cancel(self, db):
        await _seed_ws_conv(db)
        dao = TaskDAO(db)
        await dao.create(_task_row(status="waiting_confirm"))
        await dao.request_cancel("t1")

        async with db.immediate_tx():
            await dao.start_new_run(
                "t1", graph_run_id="run-2", thread_id="thread-1::run2", stage=STAGE_LINK
            )
            await dao.update_status("t1", status="running")

        row = await dao.get("t1")
        assert row.graph_run_id == "run-2"
        assert row.langgraph_thread_id == "thread-1::run2"
        assert row.current_stage == STAGE_LINK
        assert row.cancel_requested == 0
        assert row.status == "running"


# ---------- ArtifactDAO ----------


class TestArtifactDAO:
    async def _setup_task(self, db: Database, run: str = "run-1") -> TaskDAO:
        await _seed_ws_conv(db)
        dao = TaskDAO(db)
        await dao.create(_task_row(run=run))
        return dao

    async def test_put_get_and_active_picks_latest_version(self, db):
        await self._setup_task(db)
        dao = ArtifactDAO(db)
        v1 = ArtifactRow.create(
            id="a1", task_id="t1", stage=STAGE_LINK, graph_run_id="run-1",
            stage_version=1, payload={"links": []},
        )
        await dao.put(v1)
        got = await dao.get("a1")
        assert got.payload_dict() == {"links": []}
        assert got.progress is None
        assert got.origin == "system"
        # dataclass 全字段相等（含 JSON 原文与 created_at）
        assert got == v1
        active = await dao.get_active("t1", STAGE_LINK)
        assert active.id == "a1"

        async with db.immediate_tx():
            v2_num = await dao.next_version("t1", STAGE_LINK)
            assert v2_num == 2
            await dao.supersede("a1")
            v2 = ArtifactRow.create(
                id="a2", task_id="t1", stage=STAGE_LINK, graph_run_id="run-1",
                stage_version=v2_num, payload={"links": [1]},
                origin="user_revised", confirmed_by="user",
            )
            await dao.put(v2)

        active = await dao.get_active("t1", STAGE_LINK)
        assert active.id == "a2"
        assert active.origin == "user_revised"
        assert active.confirmed_by == "user"
        # 再次取版本号：版本单调，历史 superseded 也计入 MAX
        assert await dao.next_version("t1", STAGE_LINK) == 3
        # 无任何产物的阶段从 1 起
        assert await dao.next_version("t1", STAGE_POINT) == 1

    async def test_put_duplicate_version_raises_conflict(self, db):
        await self._setup_task(db)
        dao = ArtifactDAO(db)
        await dao.put(
            ArtifactRow.create(
                id="a1", task_id="t1", stage=STAGE_LINK,
                graph_run_id="run-1", stage_version=1,
            )
        )
        with pytest.raises(VersionConflict):
            await dao.put(
                ArtifactRow.create(
                    id="a2", task_id="t1", stage=STAGE_LINK,
                    graph_run_id="run-1", stage_version=1,
                )
            )

    async def test_get_missing_raises(self, db):
        with pytest.raises(NotFoundError):
            await ArtifactDAO(db).get("ghost")

    async def test_list_active_chain_skips_superseded_and_obsolete(self, db):
        await self._setup_task(db)
        dao = ArtifactDAO(db)
        await dao.put(
            ArtifactRow.create(
                id="link-old", task_id="t1", stage=STAGE_LINK,
                graph_run_id="run-1", stage_version=1,
                now="2026-09-26T00:00:00.000Z",
            )
        )
        await dao.supersede("link-old")
        await dao.put(
            ArtifactRow.create(
                id="link-new", task_id="t1", stage=STAGE_LINK,
                graph_run_id="run-1", stage_version=2,
                now="2026-09-26T00:00:01.000Z",
            )
        )
        await dao.put(
            ArtifactRow.create(
                id="point-1", task_id="t1", stage=STAGE_POINT,
                graph_run_id="run-1", stage_version=1,
                now="2026-09-26T00:00:02.000Z",
            )
        )

        chain = await dao.list_active_chain("t1")
        assert [a.id for a in chain] == ["link-new", "point-1"]

    async def test_mark_obsolete_filters_by_stage_and_run(self, db):
        await self._setup_task(db, run="run-1")
        dao = ArtifactDAO(db)
        for stage, aid in [
            (STAGE_LINK, "a-link1"),
            (STAGE_POINT, "a-point1"),
            ("case_generate", "a-case1"),
        ]:
            await dao.put(
                ArtifactRow.create(
                    id=aid, task_id="t1", stage=stage,
                    graph_run_id="run-1", stage_version=1,
                )
            )
        # 空列表短路
        assert await dao.mark_obsolete("t1", [], "run-1") == 0
        # 仅旧 run 的指定阶段作废
        n = await dao.mark_obsolete("t1", [STAGE_POINT, "case_generate"], "run-1")
        assert n == 2
        assert (await dao.get("a-link1")).status == "active"
        assert (await dao.get("a-point1")).status == "obsolete"
        assert (await dao.get("a-case1")).status == "obsolete"
        # 幂等：再跑 0 行（已非 active）
        assert await dao.mark_obsolete("t1", [STAGE_POINT], "run-1") == 0

    async def test_write_progress_roundtrip_and_missing(self, db):
        await self._setup_task(db)
        dao = ArtifactDAO(db)
        await dao.put(
            ArtifactRow.create(
                id="a1", task_id="t1", stage=STAGE_POINT,
                graph_run_id="run-1", stage_version=1,
            )
        )
        batches = [
            {"batch_id": "b1", "node": "point_write", "unit_ids": ["s1"],
             "status": "done", "case_ids": ["c1"]},
        ]
        await dao.write_progress("a1", batches)
        got = await dao.get("a1")
        assert got.progress_list() == batches
        with pytest.raises(NotFoundError):
            await dao.write_progress("ghost", batches)

    async def test_supersede_missing_raises(self, db):
        with pytest.raises(NotFoundError):
            await ArtifactDAO(db).supersede("ghost")


# ---------- TestcaseDAO ----------


class TestTestcaseDAO:
    async def _setup(self, db: Database) -> TestcaseDAO:
        await _seed_ws_conv(db)
        await TaskDAO(db).create(_task_row())
        return TestcaseDAO(db)

    async def test_put_batch_idempotent_replay(self, db):
        dao = await self._setup(db)
        records = [
            _case_record("c1", point_id="pt-1-1"),
            _case_record("c2", point_id="pt-1-2"),
        ]
        rows = [CaseRow.from_record("t1", r, now=TS0) for r in records]
        await dao.put_batch(rows)
        # 重放（崩溃点：文件已成/事务重提，dd §11.1）：INSERT OR IGNORE 不产生重复行
        await dao.put_batch(rows)
        # 即使重放对象带更晚的 created_at/updated_at，旧行首次落库时间不被覆盖
        replayed = [
            CaseRow.from_record(
                "t1", r, now="2026-12-31T00:00:00.000Z"
            )
            for r in records
        ]
        await dao.put_batch(replayed)
        for cid in ("c1", "c2"):
            got = await dao.get(cid)
            assert got.task_id == "t1"
            assert got.created_at == TS0
            assert got.to_record() == next(r for r in records if r.case_id == cid)
        count = await db.aquery_one("SELECT COUNT(*) AS c FROM testcase")
        assert count["c"] == 2

    async def test_put_batch_empty_noop(self, db):
        await TestcaseDAO(db).put_batch([])  # 不应执行 SQL、不报错

    async def test_get_missing_raises(self, db):
        with pytest.raises(NotFoundError):
            await TestcaseDAO(db).get("ghost")

    async def test_list_filters(self, db):
        dao = await self._setup(db)
        rows = [
            CaseRow.from_record("t1", _case_record("c1", version=1), now=TS0),
            CaseRow.from_record(
                "t1",
                _case_record("c2", version=1, review=ReviewStatus.ADOPTED),
                now="2026-09-26T00:00:01.000Z",
            ),
            CaseRow.from_record("t1", _case_record("c3", version=2), now=TS0),
        ]
        await dao.put_batch(rows)

        v1 = await dao.list_by_task("t1", status=None, review=None, version=1,
                                    cursor=None, limit=50)
        assert {c.id for c in v1.items} == {"c1", "c2"}
        adopted = await dao.list_by_task("t1", status="active", review="adopted",
                                         version=None, cursor=None, limit=50)
        assert [c.id for c in adopted.items] == ["c2"]

    async def test_list_pagination_no_dup_no_gap(self, db):
        dao = await self._setup(db)
        rows = [
            CaseRow.from_record(
                "t1", _case_record(f"c{i:02d}"),
                now=f"2026-09-26T00:00:{i:02d}.000Z",
            )
            for i in range(5)
        ]
        await dao.put_batch(rows)

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page: Page[CaseRow] = await dao.list_by_task(
                "t1", status=None, review=None, version=None, cursor=cursor, limit=2
            )
            seen.extend(c.id for c in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert sorted(seen) == [f"c{i:02d}" for i in range(5)]
        assert len(seen) == len(set(seen))

    async def test_edit_updates_updated_at_but_pagination_keeps_created_order(self, db):
        """002 裁决核心语义：编辑/评审只改 updated_at，列表顺序与游标仍按 created_at，
        不会把旧用例顶到首页或造成跨页漂移。"""
        dao = await self._setup(db)
        rows = [
            CaseRow.from_record(
                "t1", _case_record(f"c{i:02d}"),
                now=f"2026-09-26T00:00:{i:02d}.000Z",
            )
            for i in range(5)
        ]
        await dao.put_batch(rows)

        # 把最旧 c00 的 updated_at 拨到"晚于所有 created_at"：若游标错用 updated_at，
        # 它会被顶到首页
        later = "2026-12-31T23:59:59.000Z"
        await db.aexecute(
            "UPDATE testcase SET updated_at = ? WHERE id = ?", (later, "c00")
        )
        # DAO 评审把 updated_at 刷成当前时间（顺带验证评审只动 updated_at）
        await dao.update_review("c00", ReviewStatus.ADOPTED)

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page: Page[CaseRow] = await dao.list_by_task(
                "t1", status=None, review=None, version=None, cursor=cursor, limit=2
            )
            seen.extend(c.id for c in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        # 顺序仍严格按 created_at 倒序，c00 留在末页而非被顶到首页
        assert seen == [f"c{i:02d}" for i in range(4, -1, -1)]
        edited = await dao.get("c00")
        assert edited.created_at == "2026-09-26T00:00:00.000Z"
        # updated_at 被评审刷新为当前真实时间（早于手工拨的 later）
        assert edited.updated_at != later
        assert edited.review_status == "adopted"

    async def test_update_content_and_review(self, db):
        dao = await self._setup(db)
        await dao.put_batch([CaseRow.from_record("t1", _case_record("c1"), now=TS0)])

        await dao.update_content(
            "c1", file_path="new/path.md", content_hash="hash-new", title="新标题"
        )
        await dao.update_review("c1", ReviewStatus.EDITED_ADOPTED)
        got = await dao.get("c1")
        assert got.file_path == "new/path.md"
        assert got.content_hash == "hash-new"
        assert got.title == "新标题"
        assert got.created_at == TS0  # 编辑不改变 created_at
        assert got.review_status == "edited_adopted"
        record = got.to_record()
        assert record.review_status == ReviewStatus.EDITED_ADOPTED
        # 字符串形态也接受
        await dao.update_review("c1", "rejected")
        assert (await dao.get("c1")).review_status == "rejected"

        with pytest.raises(NotFoundError):
            await dao.update_content("ghost", file_path="x", content_hash="y", title="z")
        with pytest.raises(NotFoundError):
            await dao.update_review("ghost", ReviewStatus.ADOPTED)

    async def test_mark_obsolete_by_version(self, db):
        dao = await self._setup(db)
        await dao.put_batch(
            [
                CaseRow.from_record("t1", _case_record("c1", version=1), now=TS0),
                CaseRow.from_record("t1", _case_record("c2", version=1), now=TS0),
                CaseRow.from_record("t1", _case_record("c3", version=2), now=TS0),
            ]
        )
        assert await dao.mark_obsolete_by_version("t1", []) == 0
        n = await dao.mark_obsolete_by_version("t1", [1])
        assert n == 2
        assert (await dao.get("c1")).status == "obsolete"
        assert (await dao.get("c3")).status == "active"
        # 幂等：再跑 0 行
        assert await dao.mark_obsolete_by_version("t1", [1]) == 0

    async def test_mark_error(self, db):
        dao = await self._setup(db)
        await dao.put_batch([CaseRow.from_record("t1", _case_record("c1"), now=TS0)])
        await dao.mark_error("c1", "file_missing")
        got = await dao.get("c1")
        assert json.loads(got.error_info) == {"code": "file_missing"}
        assert got.error_code() == "file_missing"
        with pytest.raises(NotFoundError):
            await dao.mark_error("ghost", "file_missing")


# ---------- Row ↔ Pydantic 转换补充 ----------


class TestRowConversions:
    async def test_task_row_requirement_and_clauses_json(self, db):
        await _seed_ws_conv(db)
        from tester_agent.domain import RequirementRef

        req = RequirementRef(path="workspaces/ws1/tasks/t1/requirement.md",
                             content_hash="deadbeef", clause_count=3)
        clauses = [
            ClauseRef(clause_id=f"h2-{i}", level=2, title_path=[f"章{i}"],
                      anchor=f"片段{i}", text_hash=f"hash{i}")
            for i in range(2)
        ]
        row = TaskRow.create(
            id="t1", conversation_id="conv1", workspace_id="ws1",
            status="running", current_stage="intake",
            langgraph_thread_id="th", graph_run_id="run",
            requirement=req, clauses=clauses, now=TS0,
        )
        await TaskDAO(db).create(row)
        got = await TaskDAO(db).get("t1")
        assert got.requirement_obj() == req
        assert got.clauses_obj() == clauses
        # JSON 原始字符串仍由 Row 承载，中文不转义
        assert "章0" in got.clauses

    async def test_case_row_full_record_roundtrip(self, db):
        await _seed_ws_conv(db)
        await TaskDAO(db).create(_task_row())
        record = _case_record("c9", point_id="pt-2-3")
        record.lineage = Lineage(root_case_id="root-1",
                                 regenerated_from_case_id="old-9")
        row = CaseRow.from_record("t1", record, error_code="hash_conflict", now=TS0)
        await TestcaseDAO(db).put_batch([row])

        got = await TestcaseDAO(db).get("c9")
        assert got.to_record() == record
        assert got.lineage_obj() == record.lineage
        assert got.trace_refs_obj() == record.trace_refs
        assert got.error_code() == "hash_conflict"

    async def test_artifact_payload_accepts_pydantic_dict_str(self, db):
        await _seed_ws_conv(db)
        await TaskDAO(db).create(_task_row())
        dao = ArtifactDAO(db)

        await dao.put(
            ArtifactRow.create(
                id="a-dict", task_id="t1", stage=STAGE_LINK,
                graph_run_id="run-1", stage_version=1, payload={"k": "中文"},
            )
        )
        assert (await dao.get("a-dict")).payload_dict() == {"k": "中文"}

        await dao.put(
            ArtifactRow.create(
                id="a-str", task_id="t1", stage=STAGE_POINT,
                graph_run_id="run-1", stage_version=1, payload='{"raw": true}',
            )
        )
        assert (await dao.get("a-str")).payload_dict() == {"raw": True}
