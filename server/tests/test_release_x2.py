"""WP-X2 收尾验收：导出 + 提案全链 + 保留期联动（场景 10~12 收口）。

不重复 WP-27/28/29 细项，只串一条最短发布门禁路径：
1. 采纳用例 → POST /export → zip 含 INDEX + 用例；
2. 提案 create → confirm（幂等键）→ WriteResult；
3. Maintenance.lazy_purge 清过期 event + 过期 exports 目录。
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time
import uuid
import zipfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeLLM, FakeReMeReader
from tester_agent.adapters.reme import Entry, EntryType, ReMeReaderFactory, WriteResult
from tester_agent.api.cases import router as cases_router
from tester_agent.api.error_handling import install_error_handling
from tester_agent.api.kb import router as kb_router
from tester_agent.domain import (
    CaseFileContent,
    CaseRecord,
    CaseStatus,
    CaseStep,
    Lineage,
    ReviewStatus,
    TraceRefs,
)
from tester_agent.graph.main_graph import build_graph
from tester_agent.graph.registry import CASE_DESIGNER, GraphRegistry
from tester_agent.runtime.bus import EventBus
from tester_agent.runtime.context import AppContext
from tester_agent.runtime.maintenance import Maintenance
from tester_agent.runtime.runner import Runner, TaskRegistry
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    CaseRow,
    ConfigDAO,
    EventDAO,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

WS, CONV, TASK = "ws-x2", "conv-x2", "task-x2"
KB = {"kb_id": "kb-1", "options": {}}


def _run(coro):
    return asyncio.run(coro)


class FakeWriter:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def write_proposal(self, token: str, proposal: dict) -> WriteResult:
        self.calls.append((token, proposal))
        return WriteResult(
            ok=True, remote_ref="remote-x2", verified=True, error_code=None
        )


async def _seed_base(db):
    await WorkspaceDAO(db).create(
        WorkspaceRow.create(id=WS, name="x2", kb_config=dict(KB))
    )
    await db.aexecute(
        "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (CONV, WS, "c", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
    )
    await TaskDAO(db).create(
        TaskRow.create(
            id=TASK, conversation_id=CONV, workspace_id=WS,
            status="completed", current_stage="review_export",
            langgraph_thread_id=f"th-{TASK}", graph_run_id=f"run-{TASK}",
            snapshot_level="meta",
        )
    )


async def _seed_adopted_case(db, store, case_id="c-x2"):
    content = CaseFileContent(
        case_id=case_id, point_id="pt-1-1", stage_version=1,
        title="X2采纳用例", priority="P0",
        preconditions=["已登录"],
        steps=[CaseStep(seq=1, action="提交", expect="成功")],
        test_data=None,
        trace_refs=TraceRefs(clause_ids=["h2-1"], entry_ids=[]),
    )
    written = await store.write_case(WS, TASK, 1, content)
    row = CaseRow.from_record(
        TASK,
        CaseRecord(
            case_id=case_id, point_id="pt-1-1", stage_version=1,
            lineage=Lineage(root_case_id=case_id),
            status=CaseStatus.ACTIVE,
            review_status=ReviewStatus.ADOPTED,
            file_path=written.file_path, content_hash=written.content_hash,
            title="X2采纳用例",
            trace_refs=TraceRefs(clause_ids=["h2-1"]),
        ),
        batch_id="b0",
    )
    await TestcaseDAO(db).put_batch([row])


def _make_app(tmp_path: Path, writer: FakeWriter) -> FastAPI:
    tag = uuid.uuid4().hex[:8]
    db_path = tmp_path / f"x2-{tag}.db"
    ckpt_path = tmp_path / f"x2ckpt-{tag}.db"
    run_migrations(db_path)
    store = FileStore(tmp_path / f"x2fs-{tag}")
    factory = ReMeReaderFactory()

    async def _builder(_kb_config):
        return FakeReMeReader(
            [
                Entry(
                    entry_id="e1", entry_version="v1", title="t",
                    content="c", entry_type=EntryType.BUSINESS,
                    link_id="L1", story_id=None,
                    updated_at="2026-09-20T00:00:00Z", raw={},
                )
            ]
        )

    factory.register("sdk", _builder)

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        conn = await aiosqlite.connect(str(ckpt_path), check_same_thread=False)
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        g = build_graph(saver)
        db = Database(db_path)
        graphs = GraphRegistry.from_graph(CASE_DESIGNER, g)
        bus = EventBus(EventDAO(db))
        registry = TaskRegistry(TaskDAO(db))
        app_ctx = AppContext(
            db=db, file_store=store, llm=FakeLLM([]), reme_factory=factory,
            config=ConfigDAO(db), graphs=graphs, bus=bus, registry=registry,
        )
        a.state.db = db
        a.state.db_path = db_path
        a.state.file_store = store
        a.state.app_ctx = app_ctx
        a.state.runner = Runner(app_ctx, heartbeat_interval=0.05)
        a.state.kb_writer = writer
        try:
            yield
        finally:
            flying = registry.all_tasks()
            for t in flying:
                t.cancel()
            await asyncio.gather(*flying, return_exceptions=True)
            await graphs.aclose()
            await conn.close()
            db.close()

    app = FastAPI(lifespan=lifespan)
    install_error_handling(app)
    app.include_router(cases_router)
    app.include_router(kb_router)
    return app


@contextmanager
def open_env(app: FastAPI):
    with TestClient(app) as client:
        db = Database(app.state.db_path)
        try:
            yield SimpleNamespace(
                client=client, db=db, store=app.state.file_store, app=app,
            )
        finally:
            db.close()


def test_export_and_proposal_release_chain(tmp_path):
    """导出 zip 结构 + 提案两阶段确认（发布门禁）。"""
    writer = FakeWriter()
    app = _make_app(tmp_path, writer)
    with open_env(app) as env:
        _run(_seed_base(env.db))
        _run(_seed_adopted_case(env.db, env.store))

        r = env.client.post(f"/api/v1/tasks/{TASK}/export", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ready" and body["skipped"] == []
        dl = env.client.get(body["download_url"] + "?download=1")
        assert dl.status_code == 200
        with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
            names = sorted(zf.namelist())
            assert names[0] == "INDEX.md"
            case_files = [n for n in names if n.startswith("v1/") and n.endswith(".md")]
            assert len(case_files) == 1
            index = zf.read("INDEX.md").decode("utf-8")
            assert "X2采纳用例" in index and "adopted" in index

        create = env.client.post(
            "/api/v1/kb/proposals",
            json={"workspace_id": WS, "task_id": TASK,
                  "payload": {"op": "add", "title": "新链路"}},
        )
        assert create.status_code == 201, create.text
        token = create.json()["confirm_token"]
        pid = create.json()["id"]

        confirm = env.client.post(
            f"/api/v1/kb/proposals/{pid}/confirm",
            json={"confirm_token": token},
            headers={"Idempotency-Key": "x2-idem-1"},
        )
        assert confirm.status_code == 200, confirm.text
        assert confirm.json()["ok"] is True
        assert confirm.json()["remote_ref"] == "remote-x2"
        assert len(writer.calls) == 1

        replay = env.client.post(
            f"/api/v1/kb/proposals/{pid}/confirm",
            json={"confirm_token": token},
            headers={"Idempotency-Key": "x2-idem-1"},
        )
        assert replay.status_code == 200
        assert len(writer.calls) == 1


def test_retention_purge_events_and_exports(tmp_path):
    """保留期：过期 event 行 + 过期 exports/ 目录被清理。"""
    data = tmp_path / "fs"
    db_path = tmp_path / "r.db"
    run_migrations(db_path)
    db = Database(db_path)
    store = FileStore(data)
    _run(_seed_base(db))

    _run(db.aexecute(
        "INSERT INTO task_event (task_id, type, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (TASK, "old", "{}", "2026-08-01T00:00:00.000Z"),
    ))
    _run(EventDAO(db).append(TASK, "fresh", {"n": 1}))

    # FileStore 任务路径：workspaces/{ws}/{task}/exports（非 tasks/ 嵌套）
    stale = data / "workspaces" / WS / TASK / "exports" / "job-old"
    stale.mkdir(parents=True)
    (stale / "export.zip").write_bytes(b"z" * 50)
    old = time.time() - 60 * 86400
    os.utime(stale, (old, old))
    fresh = data / "workspaces" / WS / TASK / "exports" / "job-new"
    fresh.mkdir(parents=True)
    (fresh / "export.zip").write_bytes(b"z" * 20)

    counts = _run(
        Maintenance(
            db, ConfigDAO(db), file_store=store,
            retention={"events_days": 7, "exports_days": 30,
                       "snapshots_days": 30, "proposals_days": 14,
                       "obsolete_cases_days": 30},
        ).lazy_purge()
    )
    assert counts["events"] == 1
    assert counts["export_files"] == 1
    assert not stale.exists()
    assert fresh.exists()
    rows = _run(EventDAO(db).list_after(TASK, 0))
    assert len(rows) == 1 and rows[0].type == "fresh"
    db.close()
