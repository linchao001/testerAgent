"""WP-23 Reaper + 启停序列测试（dd §6.5）。

验收口径（WBS #23，场景 6）：重启改判 → /run 游标恢复。覆盖：
- Reaper.reap_on_startup：陈旧 running/cancelling → failed(INTERRUPTED_BY_RESTART)，
  新鲜 running 与 waiting_* 不动，error_info 定型；
- graceful_shutdown：取消在飞任务等时限、超时返 False、无任务返 True；
- Maintenance.lazy_purge：按 retention 清 event/快照/终态提案，pending 保留，
  配置读取与默认值；
- 端到端：lifespan 启动时把陈旧 running 改判 failed。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.main import create_app
from tester_agent.runtime.maintenance import DEFAULT_RETENTION, Maintenance
from tester_agent.runtime.reaper import INTERRUPTED_BY_RESTART, Reaper
from tester_agent.runtime.runner import TaskRegistry
from tester_agent.settings import Settings
from tester_agent.store.db import Database, run_migrations, utcnow_iso
from tester_agent.store.models import (
    EventDAO,
    ProposalDAO,
    ProposalRow,
    SnapshotDAO,
    SnapshotRow,
    TaskDAO,
    TaskRow,
    WorkspaceDAO,
    WorkspaceRow,
)

WS, CONV, TASK, RUN = "ws1", "conv1", "t1", "run-1"


# ---------- 夹具 ----------


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "app.db"
    run_migrations(db_path)
    handle = Database(db_path)
    yield handle
    handle.close()


_UNSET = object()


async def _seed_task(db, *, status, heartbeat=_UNSET):
    await WorkspaceDAO(db).create(WorkspaceRow.create(id=WS, name="ws"))
    await db.aexecute(
        "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (CONV, WS, "t", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
    )
    await TaskDAO(db).create(
        TaskRow.create(
            id=TASK, conversation_id=CONV, workspace_id=WS,
            status=status, current_stage="intake",
            langgraph_thread_id="th-1", graph_run_id=RUN,
        )
    )
    if heartbeat is not _UNSET:
        await db.aexecute(
            "UPDATE task SET runner_heartbeat = ? WHERE id = ?",
            (heartbeat, TASK),
        )


# ---------- ① Reaper.reap_on_startup ----------


class TestReapOnStartup:
    async def test_stale_running_null_heartbeat_reaped(self, db):
        await _seed_task(db, status="running")
        reaper = Reaper(TaskDAO(db), stale_sec=120)

        reaped = await reaper.reap_on_startup()
        assert reaped == [TASK]

        row = await TaskDAO(db).get(TASK)
        assert row.status == "failed"
        info = row.error_obj()
        assert info.code == INTERRUPTED_BY_RESTART
        assert info.retryable is True
        assert info.node is None

    async def test_stale_running_old_heartbeat_reaped(self, db):
        old = "2026-09-25T00:00:00.000Z"
        await _seed_task(db, status="running", heartbeat=old)
        reaper = Reaper(TaskDAO(db), stale_sec=120)
        assert await reaper.reap_on_startup() == [TASK]

    async def test_stale_cancelling_reaped(self, db):
        await _seed_task(db, status="cancelling")
        reaper = Reaper(TaskDAO(db), stale_sec=120)
        assert await reaper.reap_on_startup() == [TASK]
        assert (await TaskDAO(db).get(TASK)).status == "failed"

    async def test_fresh_running_not_reaped(self, db):
        await _seed_task(db, status="running", heartbeat=utcnow_iso())
        reaper = Reaper(TaskDAO(db), stale_sec=120)
        assert await reaper.reap_on_startup() == []
        assert (await TaskDAO(db).get(TASK)).status == "running"

    async def test_waiting_states_not_reaped(self, db):
        await _seed_task(db, status="waiting_confirm")
        for status in ("waiting_confirm", "waiting_input", "completed",
                       "failed", "aborted"):
            # 行已在 _seed_task 种下，直接改状态即可
            await db.aexecute(
                "UPDATE task SET status = ? WHERE id = ?", (status, TASK)
            )
            reaper = Reaper(TaskDAO(db), stale_sec=120)
            assert await reaper.reap_on_startup() == []
            assert (await TaskDAO(db).get(TASK)).status == status


# ---------- ② graceful_shutdown ----------


class TestGracefulShutdown:
    async def test_no_tasks_returns_true(self):
        registry = TaskRegistry(TaskDAO.__new__(TaskDAO))  # 不触 DB
        reaper = Reaper.__new__(Reaper)
        reaper._stale_sec = 120
        assert await reaper.graceful_shutdown(registry, timeout_sec=1.0) is True

    async def test_cancels_and_waits_returns_true(self):
        registry = TaskRegistry.__new__(TaskRegistry)
        registry._tasks = {}

        cancelled = {"n": 0}

        async def worker():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled["n"] += 1
                raise

        t = asyncio.create_task(worker())
        registry._tasks[TASK] = t
        await asyncio.sleep(0.02)  # 让任务进入挂起态再取消
        reaper = Reaper.__new__(Reaper)

        ok = await reaper.graceful_shutdown(registry, timeout_sec=2.0)
        assert ok is True
        assert cancelled["n"] == 1
        assert t.cancelled()

    async def test_timeout_returns_false(self):
        registry = TaskRegistry.__new__(TaskRegistry)
        registry._tasks = {}

        async def immune():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # 吞掉取消继续阻塞，模拟未在时限内退出
                await asyncio.Event().wait()

        t = asyncio.create_task(immune())
        registry._tasks[TASK] = t
        await asyncio.sleep(0.02)  # 让任务进入挂起态（immune 的 try 已生效）
        reaper = Reaper.__new__(Reaper)

        ok = await reaper.graceful_shutdown(registry, timeout_sec=0.05)
        assert ok is False
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)


# ---------- ③ Maintenance.lazy_purge ----------


class TestLazyPurge:
    async def _make_snapshot(self, db, sid, created):
        row = SnapshotRow.create(
            id=sid, task_id=TASK, graph_run_id=RUN, stage="intake",
            stage_version=1, node="intake", prompt_template_ver="v1",
            now=created,
        )
        await SnapshotDAO(db).put(row)

    async def _make_proposal(self, db, pid, status, created):
        row = ProposalRow(
            id=pid, workspace_id=WS, task_id=TASK, status=status,
            expires_at="2026-10-30T00:00:00.000Z", created_at=created,
        )
        await ProposalDAO(db).create(row)

    async def test_purges_old_events_keeps_recent(self, db):
        await _seed_task(db, status="running")
        await EventDAO(db).append(TASK, "e0", {})
        # 手工落一条 30 天前的旧事件
        await db.aexecute(
            "INSERT INTO task_event (task_id, type, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (TASK, "old", "{}", "2026-08-20T00:00:00.000Z"),
        )
        m = Maintenance(db, retention={"events_days": 7})
        counts = await m.lazy_purge()
        assert counts["events"] == 1
        rows = await EventDAO(db).list_after(TASK, 0)
        assert len(rows) == 1 and rows[0].type == "e0"

    async def test_purges_old_snapshots(self, db):
        await _seed_task(db, status="running")
        await self._make_snapshot(db, "s-new", utcnow_iso())
        await self._make_snapshot(db, "s-old", "2026-08-01T00:00:00.000Z")

        counts = await Maintenance(db, retention={"snapshots_days": 30}).lazy_purge()
        assert counts["snapshots"] == 1
        page = await SnapshotDAO(db).list_by_task(TASK, cursor=None, limit=10)
        assert [r.id for r in page.items] == ["s-new"]

    async def test_purges_terminal_proposals_keeps_pending(self, db):
        await _seed_task(db, status="running")
        await self._make_proposal(db, "p-pending", "pending", "2026-08-01T00:00:00.000Z")
        await self._make_proposal(db, "p-confirmed", "confirmed", "2026-08-01T00:00:00.000Z")
        await self._make_proposal(db, "p-recent", "confirmed", utcnow_iso())

        counts = await Maintenance(db, retention={"proposals_days": 14}).lazy_purge()
        assert counts["proposals"] == 1
        # pending 旧提案保留，confirmed 旧的删除，近期 confirmed 保留
        assert await ProposalDAO(db).get("p-pending")
        with pytest.raises(Exception):
            await ProposalDAO(db).get("p-confirmed")
        assert await ProposalDAO(db).get("p-recent")

    async def test_default_retention_shape(self):
        assert DEFAULT_RETENTION == {
            "events_days": 7, "snapshots_days": 30, "proposals_days": 14,
            "obsolete_cases_days": 30, "exports_days": 30,
        }

    async def test_retention_zero_purges_all_old_events(self, db):
        await _seed_task(db, status="running")
        await db.aexecute(
            "INSERT INTO task_event (task_id, type, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (TASK, "old", "{}", "2026-09-27T00:00:00.000Z"),
        )
        counts = await Maintenance(db, retention={"events_days": 0}).lazy_purge()
        assert counts["events"] == 1
        assert await EventDAO(db).list_after(TASK, 0) == []


# ---------- ④ 端到端：lifespan 启动改判（场景 6 前半） ----------


class TestLifespanReap:
    def test_startup_reclaims_stale_running_task(self, tmp_path):
        data_dir = tmp_path / "data"
        db_path = data_dir / "app.db"
        run_migrations(db_path)

        # 预种陈旧 running 任务（心跳空），随后关闭播种连接
        seed_db = Database(db_path)

        async def _prepare():
            await WorkspaceDAO(seed_db).create(
                WorkspaceRow.create(id=WS, name="ws")
            )
            await seed_db.aexecute(
                "INSERT INTO conversation (id, workspace_id, title, "
                "created_at, updated_at) VALUES (?,?,?,?,?)",
                (CONV, WS, "t", "2026-09-26T00:00:00.000Z",
                 "2026-09-26T00:00:00.000Z"),
            )
            await TaskDAO(seed_db).create(
                TaskRow.create(
                    id=TASK, conversation_id=CONV, workspace_id=WS,
                    status="running", current_stage="intake",
                    langgraph_thread_id="th-1", graph_run_id=RUN,
                )
            )

        asyncio.run(_prepare())
        seed_db.close()

        settings = Settings.from_env({
            "TESTER_AGENT_DATA_DIR": str(data_dir),
            "TESTER_AGENT_SINGLE_WORKER": "1",
        })
        with TestClient(create_app(settings)) as client:
            assert client.get("/healthz").status_code == 200

        # 启动时 Reaper 已把陈旧 running 改判 failed
        verify = Database(db_path)
        row = asyncio.run(TaskDAO(verify).get(TASK))
        assert row.status == "failed"
        assert row.error_obj().code == INTERRUPTED_BY_RESTART
        verify.close()
