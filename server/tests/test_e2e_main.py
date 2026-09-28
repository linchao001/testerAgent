"""WP-X1 API 级 E2E（dd §15.1 / §15.2）：主场景、回退继承、崩溃恢复。

Playwright 浏览器层见 web/e2e（opt-in，不入 PR 必跑）。本文件用 ASGI
TestClient + 假图节点（写真实 artifact/case 文件）覆盖主路径 HTTP 面。
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
import zipfile
from contextlib import asynccontextmanager, contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeLLM, FakeReMeReader
from tester_agent.adapters.reme import ReMeReaderFactory
from tester_agent.api.cases import router as cases_router
from tester_agent.api.error_handling import install_error_handling
from tester_agent.api.tasks import router as tasks_router
from tester_agent.domain import (
    CaseFileContent,
    CaseRecord,
    CaseStep,
    Lineage,
    ReviewStatus,
    TraceRefs,
)
from tester_agent.graph.constants import (
    STAGE_CASE_GENERATE,
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from tester_agent.graph.main_graph import build_graph
from tester_agent.graph.registry import CASE_DESIGNER, GraphRegistry
from tester_agent.runtime.bus import EventBus
from tester_agent.runtime.context import AppContext
from tester_agent.runtime.runner import Runner, TaskRegistry
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ArtifactDAO,
    ArtifactRow,
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

WS, CONV, TASK = "ws-x1", "conv-x1", "task-x1"
KB = {"mode": "sdk", "target": "/tmp/reme", "kb_id": "kb-1", "options": {}}
C1 = "h2-1"

LINK_PLAN = {
    "links": [{
        "link_id": "L1", "title": "订单", "summary": "下单", "hit": True,
        "entry_id": "l1", "entry_version": "v1", "confidence": 0.9,
        "story_ids": ["S1"],
    }],
    "stories": [{
        "story_id": "S1", "link_id": "L1", "title": "下单",
        "summary": "购物车下单", "hit": True, "entry_id": "s1",
        "entry_version": "v1", "confidence": 0.9, "rationale": "r",
        "related_clause_ids": [C1],
    }],
    "new_suggestions": [],
}
POINT_PLAN = {"points": [{
    "point_id": "pt-1-1", "story_id": "S1", "title": "正常下单",
    "angle": "正常", "method": "场景法", "clause_ids": [C1],
    "source_entry_ids": ["b1"], "priority": "P0",
}]}


def _run(coro):
    return asyncio.run(coro)


def _wait_status(db, task_id, want, *, timeout=8.0):
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = _run(TaskDAO(db).get(task_id))
        if row.status == want:
            return row
        time.sleep(0.05)
    raise AssertionError(f"wait {want} timeout, last={row.status if row else None}")


def _wait_confirm_at(db, task_id, stage, *, timeout=8.0):
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = _run(TaskDAO(db).get(task_id))
        if row.status == "waiting_confirm" and row.current_stage == stage:
            return row
        time.sleep(0.05)
    raise AssertionError(
        f"wait confirm@{stage} timeout: {row.status if row else None}"
        f"@{row.current_stage if row else None}"
    )


def _e2e_nodes(store: FileStore):
    """假图：写真实 link/point artifact；case_generate 落真实 MD + testcase 行。"""

    async def intake(ctx, state):
        return {}

    async def link(ctx, state):
        dao = ctx.daos.artifact
        v = await dao.next_version(ctx.task.id, STAGE_LINK_IDENTIFY)
        aid = "art-link" if v == 1 else f"art-link-v{v}"
        await dao.put(ArtifactRow.create(
            id=aid, task_id=ctx.task.id, stage=STAGE_LINK_IDENTIFY,
            graph_run_id=ctx.run_id, stage_version=v, payload=LINK_PLAN,
        ))
        return {"link_plan": LINK_PLAN}

    async def point(ctx, state):
        dao = ctx.daos.artifact
        v = await dao.next_version(ctx.task.id, STAGE_POINT_WRITE)
        aid = "art-point" if v == 1 else f"art-point-v{v}"
        await dao.put(ArtifactRow.create(
            id=aid, task_id=ctx.task.id, stage=STAGE_POINT_WRITE,
            graph_run_id=ctx.run_id, stage_version=v, payload=POINT_PLAN,
        ))
        return {"point_plan": POINT_PLAN}

    async def case_gen(ctx, state):
        content = CaseFileContent(
            case_id="case-x1-1", point_id="pt-1-1", stage_version=1,
            title="提交订单成功", priority="P0", preconditions=["已登录"],
            steps=[CaseStep(seq=1, action="提交订单", expect="创建成功")],
            test_data=None, trace_refs=TraceRefs(clause_ids=[C1]),
        )
        written = await store.write_case(
            ctx.task.workspace_id, ctx.task.id, 1, content,
        )
        row = CaseRow.from_record(
            ctx.task.id,
            CaseRecord(
                case_id="case-x1-1", point_id="pt-1-1", stage_version=1,
                lineage=Lineage(root_case_id="case-x1-1"), status="active",
                review_status=ReviewStatus.PENDING,
                file_path=written.file_path, content_hash=written.content_hash,
                title="提交订单成功", trace_refs=TraceRefs(clause_ids=[C1]),
            ),
            batch_id="b0",
        )
        await TestcaseDAO(ctx.app.db).put_batch([row])
        dao = ctx.daos.artifact
        await dao.put(ArtifactRow.create(
            id="art-case", task_id=ctx.task.id, stage=STAGE_CASE_GENERATE,
            graph_run_id=ctx.run_id, stage_version=1,
            payload={"case_count": 1, "case_ids": ["case-x1-1"]},
        ))
        return {}

    async def coverage(ctx, state):
        return {}

    return {
        STAGE_INTAKE: intake,
        STAGE_LINK_IDENTIFY: link,
        STAGE_POINT_WRITE: point,
        STAGE_CASE_GENERATE: case_gen,
        "coverage_check": coverage,
    }


def _make_app(tmp_path: Path) -> FastAPI:
    tag = uuid.uuid4().hex[:8]
    db_path = tmp_path / f"e2e-{tag}.db"
    ckpt_path = tmp_path / f"ckpt-{tag}.db"
    run_migrations(db_path)
    store = FileStore(tmp_path / f"files-{tag}")
    reader = FakeReMeReader([])
    factory = ReMeReaderFactory()

    async def _builder(_kb):
        return reader

    factory.register("sdk", _builder)

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        conn = await aiosqlite.connect(str(ckpt_path), check_same_thread=False)
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        from tester_agent.tools.capabilities import caps_from_stage_nodes

        g = build_graph(saver, caps=caps_from_stage_nodes(_e2e_nodes(store)))
        db = Database(db_path)
        cfg = ConfigDAO(db)
        rt = (await cfg.get()).runtime_dict()
        rt["human_gate_review"] = False
        await cfg.update_runtime(rt)
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
        a.state.bus = bus
        a.state.reme_factory = factory
        a.state.graphs = graphs
        a.state.registry = registry
        a.state.app_ctx = app_ctx
        a.state.runner = Runner(app_ctx, heartbeat_interval=0.05)
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
    app.include_router(tasks_router)
    app.include_router(cases_router)
    return app


@contextmanager
def open_env(app: FastAPI):
    with TestClient(app) as client:
        db = Database(app.state.db_path)
        try:
            yield SimpleNamespace(client=client, db=db, app=app)
        finally:
            db.close()


async def _seed_task(db, task_id=TASK, *, status="waiting_input",
                     stage=STAGE_INTAKE):
    row = await db.aquery_one("SELECT id FROM workspace WHERE id = ?", (WS,))
    if row is None:
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=WS, name="ws", kb_config=dict(KB))
        )
    await db.aexecute(
        "INSERT OR IGNORE INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (CONV, WS, "c", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
    )
    await TaskDAO(db).create(
        TaskRow.create(
            id=task_id, conversation_id=CONV, workspace_id=WS,
            status=status, current_stage=stage,
            langgraph_thread_id=f"th-{task_id}",
            graph_run_id=f"run-{task_id}", snapshot_level="meta",
        )
    )


@pytest.fixture()
def env(tmp_path):
    with open_env(_make_app(tmp_path)) as e:
        yield e


class TestE2EMainScenario:
    def test_requirement_to_review_to_export(self, env):
        """主场景：run → CP1 → CP2 → completed → review → export。"""
        client, db = env.client, env.db
        _run(_seed_task(db))

        r = client.post(f"/api/v1/tasks/{TASK}/run")
        assert r.status_code == 200
        _wait_confirm_at(db, TASK, STAGE_LINK_IDENTIFY)

        detail = client.get(f"/api/v1/tasks/{TASK}").json()
        art = detail["active_artifacts"][STAGE_LINK_IDENTIFY]
        r2 = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_LINK_IDENTIFY, "artifact_id": art["id"],
            "expected_version": art["stage_version"], "action": "confirm",
        })
        assert r2.status_code == 200
        _wait_confirm_at(db, TASK, STAGE_POINT_WRITE)

        detail = client.get(f"/api/v1/tasks/{TASK}").json()
        art2 = detail["active_artifacts"][STAGE_POINT_WRITE]
        r3 = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_POINT_WRITE, "artifact_id": art2["id"],
            "expected_version": art2["stage_version"], "action": "confirm",
        })
        assert r3.status_code == 200
        _wait_status(db, TASK, "completed")

        cases = client.get(f"/api/v1/tasks/{TASK}/cases").json()["items"]
        assert len(cases) == 1
        case_id = cases[0]["id"]

        rev = client.post("/api/v1/cases/review", json={
            "items": [{"case_id": case_id, "action": "adopt"}],
        })
        assert rev.status_code == 200
        assert rev.json()["results"][0]["review_status"] == "adopted"

        exp = client.post(f"/api/v1/tasks/{TASK}/export", json={})
        assert exp.status_code == 200
        body = exp.json()
        assert body["status"] == "ready"
        job_id = body["job_id"]
        dl = client.get(
            f"/api/v1/tasks/{TASK}/export/{job_id}?download=1"
        )
        assert dl.status_code == 200
        zf = zipfile.ZipFile(BytesIO(dl.content))
        names = zf.namelist()
        assert "INDEX.md" in names
        assert any(n.endswith(".md") and n != "INDEX.md" for n in names)


class TestE2ERollbackInherit:
    def test_summary_only_revision_inherits_downstream(self, env):
        """回退继承：仅改 story 摘要 → affected_point_ids 空，用例仍 active。"""
        client, db = env.client, env.db
        _run(_seed_task(db))
        client.post(f"/api/v1/tasks/{TASK}/run")
        _wait_confirm_at(db, TASK, STAGE_LINK_IDENTIFY)
        detail = client.get(f"/api/v1/tasks/{TASK}").json()
        art = detail["active_artifacts"][STAGE_LINK_IDENTIFY]
        client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_LINK_IDENTIFY, "artifact_id": art["id"],
            "expected_version": 1, "action": "confirm",
        })
        _wait_confirm_at(db, TASK, STAGE_POINT_WRITE)
        detail = client.get(f"/api/v1/tasks/{TASK}").json()
        art2 = detail["active_artifacts"][STAGE_POINT_WRITE]
        client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_POINT_WRITE, "artifact_id": art2["id"],
            "expected_version": 1, "action": "confirm",
        })
        _wait_status(db, TASK, "completed")

        link_art = _run(ArtifactDAO(db).get_active(TASK, STAGE_LINK_IDENTIFY))
        revised = {
            "links": LINK_PLAN["links"],
            "stories": [{
                **LINK_PLAN["stories"][0],
                "summary": "购物车下单-仅改摘要",
            }],
            "new_suggestions": [],
        }
        rb = client.post(f"/api/v1/tasks/{TASK}/rollback", json={
            "target_stage": STAGE_LINK_IDENTIFY,
            "artifact_id": link_art.id,
            "expected_version": link_art.stage_version,
            "revised_artifact": revised,
        })
        assert rb.status_code == 200, rb.text
        impact = rb.json()["impact"]
        assert impact["target_stage"] == STAGE_LINK_IDENTIFY
        assert impact["affected_point_ids"] == []

        page = _run(TestcaseDAO(db).list_by_task(
            TASK, status="active", review=None, version=None, cursor=None, limit=50,
        ))
        assert len(page.items) == 1
        assert page.items[0].id == "case-x1-1"


class TestE2ECrashResume:
    def test_failed_run_resumes_from_batch_cursor(self, env):
        """崩溃恢复：failed + progress 游标 → /run 带回 resume_from。"""
        client, db = env.client, env.db
        _run(_seed_task(db, status="failed", stage=STAGE_POINT_WRITE))
        _run(ArtifactDAO(db).put(ArtifactRow.create(
            id="art-point", task_id=TASK, stage=STAGE_POINT_WRITE,
            graph_run_id=f"run-{TASK}", stage_version=1, payload=POINT_PLAN,
            progress=[
                {"batch_id": "b0", "node": STAGE_POINT_WRITE,
                 "unit_ids": ["u1"], "status": "done", "idem": "i0",
                 "result_ids": ["c1"]},
                {"batch_id": "b1", "node": STAGE_POINT_WRITE,
                 "unit_ids": ["u2"], "status": "started", "idem": "i1",
                 "result_ids": []},
            ],
        )))
        r = client.post(f"/api/v1/tasks/{TASK}/run")
        assert r.status_code == 200
        assert r.json()["resume_from"] == {
            "node": STAGE_POINT_WRITE, "batch_id": "b1", "done": 1, "total": 2,
        }
        _wait_status(db, TASK, "waiting_confirm")
