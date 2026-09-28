"""WP-29 Reconciler 验收测试：DB↔文件三态对账（dd §11.3 / tech-design §3.3③
场景 10）+ 手动触发端点。

验收口径：
- file_missing：DB 有行、文件缺失 → mark_error(file_missing)；
- hash_conflict：DB 行 hash ≠ 盘上文件 hash → mark_error(hash_conflict)；
- orphan：盘上文件存在、DB 无行 → 报告 orphan_paths，不挂接、不改行；
- 已带 error_info 的行不参与 hash 比对（等用户消解）；
- 手动端点 POST /workspaces/{id}/reconcile 返回计数与清单。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeLLM
from tester_agent.adapters.reme import ReMeReaderFactory
from tester_agent.api.error_handling import install_error_handling
from tester_agent.api.maintenance import router as maintenance_router
from tester_agent.domain import CaseFileContent, CaseRecord, CaseStep, Lineage, ReviewStatus, TraceRefs
from tester_agent.graph.main_graph import build_graph
from tester_agent.graph.registry import CASE_DESIGNER, GraphRegistry
from tester_agent.runtime.bus import EventBus
from tester_agent.runtime.context import AppContext
from tester_agent.runtime.idempotency import IdempotencyStore
from tester_agent.runtime.reconciler import Reconciler
from tester_agent.runtime.runner import TaskRegistry
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ConfigDAO,
    EventDAO,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

WS, CONV, TASK = "ws-1", "conv-1", "task-1"
C1 = "h2-1"
KB_CONFIG = {
    "kb_id": "kb-1",
    "knowledge_bases_dir": "",
    "knowledge_dir": "knowledge",
    "create_knowledge_base": False,
    "options": {},
}


def _run(coro):
    return asyncio.run(coro)


def _case_content(case_id, title):
    return CaseFileContent(
        case_id=case_id, point_id="pt-1-1", stage_version=1, title=title,
        priority="P0", preconditions=["已登录"],
        steps=[CaseStep(seq=1, action=f"执行-{title}", expect="通过")],
        test_data=None, trace_refs=TraceRefs(clause_ids=[C1]),
    )


async def _seed_task(db, task_id=TASK, *, ws=WS, conv=CONV):
    row = await db.aquery_one("SELECT id FROM workspace WHERE id = ?", (ws,))
    if row is None:
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=ws, name="ws", kb_config=dict(KB_CONFIG))
        )
    await db.aexecute(
        "INSERT OR IGNORE INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (conv, ws, "c", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
    )
    await TaskDAO(db).create(
        TaskRow.create(
            id=task_id, conversation_id=conv, workspace_id=ws,
            status="completed", current_stage="case_generate",
            langgraph_thread_id=f"th-{task_id}", graph_run_id=f"run-{task_id}",
        )
    )


async def _seed_case(db, store, task_id, case_id, title, *, ws=WS):
    """DB 行 + 真实 MD 文件（content_hash 与盘上一致）。"""
    written = await store.write_case(ws, task_id, 1, _case_content(case_id, title))
    row = CaseRow(
        id=case_id, task_id=task_id, point_id="pt-1-1", stage_version=1,
        lineage=json.dumps({"root_case_id": case_id}),
        status="active", review_status=ReviewStatus.PENDING.value,
        file_path=written.file_path, content_hash=written.content_hash,
        title=title, trace_refs=json.dumps({"clause_ids": [C1]}),
        error_info=None, batch_id="b0",
        created_at="2026-09-26T00:00:00.000Z",
        updated_at="2026-09-26T00:00:00.000Z",
    )
    await TestcaseDAO(db).put_batch([row])
    return row


from tester_agent.store.models import CaseRow  # noqa: E402


def _make_app(tmp_path: Path) -> FastAPI:
    tag = uuid.uuid4().hex[:8]
    db_path = tmp_path / f"appr-{tag}.db"
    ckpt_path = tmp_path / f"ckptr-{tag}.db"
    run_migrations(db_path)
    store = FileStore(tmp_path / f"wsfiles-r{tag}")
    factory = ReMeReaderFactory()

    async def _builder(_kb_config):
        from fakes import FakeReMeReader

        return FakeReMeReader([])

    factory.register("sdk", _builder)

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        conn = await aiosqlite.connect(str(ckpt_path), check_same_thread=False)
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        g = build_graph(saver)
        db = Database(db_path)
        graphs = GraphRegistry.from_graph(CASE_DESIGNER, g)
        bus = EventBus(EventDAO(db))
        registry = TaskRegistry(TaskDAO(db))
        a.state.db = db
        a.state.db_path = db_path
        a.state.file_store = store
        a.state.bus = bus
        a.state.registry = registry
        a.state.reme_factory = factory
        a.state.idem_store = IdempotencyStore()
        a.state.app_ctx = AppContext(
            db=db, file_store=store, llm=FakeLLM([]), reme_factory=factory,
            config=ConfigDAO(db), graphs=graphs, bus=bus, registry=registry,
        )
        try:
            yield
        finally:
            await graphs.aclose()
            await conn.close()
            db.close()

    app = FastAPI(lifespan=lifespan)
    install_error_handling(app)
    app.include_router(maintenance_router)
    return app


@contextmanager
def open_env(app: FastAPI):
    with TestClient(app) as client:
        db = Database(app.state.db_path)
        try:
            yield SimpleNamespace(client=client, db=db, app=app,
                                  store=app.state.file_store)
        finally:
            db.close()


@pytest.fixture()
def env(tmp_path):
    with open_env(_make_app(tmp_path)) as e:
        yield e


def _err_info(db, case_id):
    row = _run(TestcaseDAO(db).get(case_id))
    return json.loads(row.error_info)["code"] if row.error_info else None


def test_reconcile_clean_no_issues(env):
    _run(_seed_task(env.db))
    _run(_seed_case(env.db, env.store, TASK, "c1", "用例一"))
    report = _run(Reconciler(env.db, env.store).reconcile_workspace(WS))
    assert report.file_missing_count == 0
    assert report.hash_conflict_count == 0
    assert report.orphan_count == 0
    assert _err_info(env.db, "c1") is None


def test_reconcile_file_missing_marks_error(env):
    _run(_seed_task(env.db))
    _run(_seed_case(env.db, env.store, TASK, "c1", "用例一"))
    # 外部删除文件
    from tester_agent.store.workspace_files import _normalize_hash
    row = _run(TestcaseDAO(env.db).get("c1"))
    task_dir = env.store._task_dir(WS, TASK)
    (task_dir / row.file_path).unlink()

    report = _run(Reconciler(env.db, env.store).reconcile_workspace(WS))
    assert report.file_missing_count == 1
    assert report.tasks[0].file_missing == ["c1"]
    assert _err_info(env.db, "c1") == "file_missing"


def test_reconcile_hash_conflict_marks_error(env):
    _run(_seed_task(env.db))
    _run(_seed_case(env.db, env.store, TASK, "c1", "用例一"))
    # 外部改写文件（改变内容但不更新 DB hash）
    row = _run(TestcaseDAO(env.db).get("c1"))
    target = env.store._task_dir(WS, TASK) / row.file_path
    target.write_text("被外部篡改的内容\n", encoding="utf-8")

    report = _run(Reconciler(env.db, env.store).reconcile_workspace(WS))
    assert report.hash_conflict_count == 1
    assert report.tasks[0].hash_conflict == ["c1"]
    assert _err_info(env.db, "c1") == "hash_conflict"


def test_reconcile_orphan_file_reported(env):
    _run(_seed_task(env.db))
    _run(_seed_case(env.db, env.store, TASK, "c1", "用例一"))
    # 盘上多一个孤儿 MD（DB 无行）
    cases_dir = env.store._task_dir(WS, TASK) / "cases" / "v1"
    cases_dir.mkdir(parents=True, exist_ok=True)
    (cases_dir / "orphan-case.md").write_text("# 孤儿\n", encoding="utf-8")

    report = _run(Reconciler(env.db, env.store).reconcile_workspace(WS))
    assert report.orphan_count == 1
    assert report.tasks[0].orphan_paths == ["cases/v1/orphan-case.md"]
    # 孤儿不影响正常行
    assert _err_info(env.db, "c1") is None


def test_reconcile_skips_rows_with_existing_error(env):
    """已带 error_info 的行不参与 hash 比对（dd §11.3：by_path 排除）。"""
    _run(_seed_task(env.db))
    _run(_seed_case(env.db, env.store, TASK, "c1", "用例一"))
    # 先标记 file_missing
    _run(TestcaseDAO(env.db).mark_error("c1", "file_missing"))
    # 再篡改文件：不应被判定为 hash_conflict（行已带 error_info）
    row = _run(TestcaseDAO(env.db).get("c1"))
    target = env.store._task_dir(WS, TASK) / row.file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("篡改\n", encoding="utf-8")

    report = _run(Reconciler(env.db, env.store).reconcile_workspace(WS))
    assert report.hash_conflict_count == 0
    # error_info 保持原值，不被覆盖
    assert _err_info(env.db, "c1") == "file_missing"


def test_reconcile_workspace_404(env):
    from tester_agent.errors import NotFoundError

    with pytest.raises(NotFoundError):
        _run(Reconciler(env.db, env.store).reconcile_workspace("nope"))


def test_reconcile_endpoint(env):
    _run(_seed_task(env.db))
    _run(_seed_case(env.db, env.store, TASK, "c1", "用例一"))
    row = _run(TestcaseDAO(env.db).get("c1"))
    (env.store._task_dir(WS, TASK) / row.file_path).unlink()

    r = env.client.post(f"/api/v1/workspaces/{WS}/reconcile")
    assert r.status_code == 200
    body = r.json()
    assert body["file_missing_count"] == 1
    assert body["hash_conflict_count"] == 0
    assert body["orphan_count"] == 0
    assert body["tasks"][0]["file_missing"] == ["c1"]

    # 工作区不存在 → 404
    assert env.client.post("/api/v1/workspaces/nope/reconcile").status_code == 404
