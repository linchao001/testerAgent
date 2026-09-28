"""WP-29 幂等键存储验收测试（dd §6.6）+ 端点接线验收。

- 单元：同 key 同 body 重放、同 key 异 body 409、TTL 过期失效、purge；
- 接线：``POST /cases/review`` 带 Idempotency-Key 两次 → 第二次重放且不
  产生第二条 review_record。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
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
from tester_agent.api.cases import router as cases_router
from tester_agent.api.error_handling import install_error_handling
from tester_agent.domain import CaseFileContent, CaseStep, Lineage, ReviewStatus, TraceRefs
from tester_agent.errors import VersionConflict
from tester_agent.graph.main_graph import build_graph
from tester_agent.graph.registry import CASE_DESIGNER, GraphRegistry
from tester_agent.runtime.bus import EventBus
from tester_agent.runtime.context import AppContext
from tester_agent.runtime.idempotency import IdempotencyStore
from tester_agent.runtime.runner import TaskRegistry
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ConfigDAO,
    EventDAO,
    ReviewDAO,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

WS, CONV, TASK = "ws-1", "conv-1", "task-1"
KB_CONFIG = {
    "kb_id": "kb-1",
    "knowledge_bases_dir": "",
    "knowledge_dir": "knowledge",
    "create_knowledge_base": False,
    "options": {},
}


def _run(coro):
    return asyncio.run(coro)


# ---------- 单元：IdempotencyStore ----------


def test_idem_record_and_replay():
    store = IdempotencyStore()
    h = store.request_hash('{"a":1}')
    store.record("cases.review", "k1", h, 200, {"results": []})
    rec = store.lookup("cases.review", "k1", h)
    assert rec is not None
    assert rec.status_code == 200
    assert rec.body == {"results": []}


def test_idem_different_body_raises_409():
    store = IdempotencyStore()
    store.record("cases.review", "k1", store.request_hash('{"a":1}'), 200, {"results": []})
    with pytest.raises(VersionConflict):
        store.lookup("cases.review", "k1", store.request_hash('{"a":2}'))


def test_idem_ttl_expiry():
    store = IdempotencyStore(ttl_sec=0)
    store.record("cases.review", "k1", store.request_hash("x"), 200, {})
    time.sleep(0.05)
    assert store.lookup("cases.review", "k1", store.request_hash("x")) is None
    assert len(store) == 0


def test_idem_purge_removes_expired():
    store = IdempotencyStore(ttl_sec=3600)
    store.record("a", "k1", store.request_hash("1"), 200, {})
    # 手动置过期，验证 purge 清理
    store._data[("a", "k1")].expires_at = time.monotonic() - 1
    assert store.purge_expired() == 1
    assert len(store) == 0


def test_idem_new_write_purges_expired():
    """record() 每次写入顺带清过期项。"""
    store = IdempotencyStore(ttl_sec=0)
    store.record("a", "k1", store.request_hash("1"), 200, {})
    # k1 已过期；用正 ttl 写 k2 应顺带把 k1 清掉
    store._ttl = 3600.0
    store.record("a", "k2", store.request_hash("2"), 200, {})
    assert len(store) == 1
    assert ("a", "k1") not in store._data
    assert ("a", "k2") in store._data


def test_idem_no_key_returns_none():
    store = IdempotencyStore()
    assert store.lookup("cases.review", "missing", store.request_hash("x")) is None


# ---------- 接线：review 幂等重放 ----------


def _case_content(case_id, title):
    return CaseFileContent(
        case_id=case_id, point_id="pt-1-1", stage_version=1, title=title,
        priority="P0", preconditions=["已登录"],
        steps=[CaseStep(seq=1, action=f"执行-{title}", expect="通过")],
        test_data=None, trace_refs=TraceRefs(clause_ids=["c1"]),
    )


async def _seed(db, store):
    await WorkspaceDAO(db).create(
        WorkspaceRow.create(id=WS, name="ws", kb_config=dict(KB_CONFIG))
    )
    await db.aexecute(
        "INSERT OR IGNORE INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (CONV, WS, "c", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
    )
    await TaskDAO(db).create(
        TaskRow.create(
            id=TASK, conversation_id=CONV, workspace_id=WS, status="completed",
            current_stage="case_generate",
            langgraph_thread_id=f"th-{TASK}", graph_run_id=f"run-{TASK}",
        )
    )
    written = await store.write_case(WS, TASK, 1, _case_content("c1", "用例一"))
    from tester_agent.store.models import CaseRow

    await TestcaseDAO(db).put_batch([
        CaseRow(
            id="c1", task_id=TASK, point_id="pt-1-1", stage_version=1,
            lineage=json.dumps({"root_case_id": "c1"}),
            status="active", review_status=ReviewStatus.PENDING.value,
            file_path=written.file_path, content_hash=written.content_hash,
            title="用例一", trace_refs=json.dumps({"clause_ids": ["c1"]}),
            error_info=None, batch_id="b0",
            created_at="2026-09-26T00:00:00.000Z",
            updated_at="2026-09-26T00:00:00.000Z",
        )
    ])


def _make_app(tmp_path):
    tag = uuid.uuid4().hex[:8]
    db_path = tmp_path / f"appid-{tag}.db"
    ckpt_path = tmp_path / f"ckptid-{tag}.db"
    run_migrations(db_path)
    store = FileStore(tmp_path / f"wsfiles-i{tag}")
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
        idem = IdempotencyStore()
        a.state.db = db
        a.state.db_path = db_path
        a.state.file_store = store
        a.state.bus = bus
        a.state.registry = TaskRegistry(TaskDAO(db))
        a.state.reme_factory = factory
        a.state.idem_store = idem
        a.state.app_ctx = AppContext(
            db=db, file_store=store, llm=FakeLLM([]), reme_factory=factory,
            config=ConfigDAO(db), graphs=graphs, bus=bus,
            registry=TaskRegistry(TaskDAO(db)),
        )
        try:
            yield
        finally:
            await graphs.aclose()
            await conn.close()
            db.close()

    app = FastAPI(lifespan=lifespan)
    install_error_handling(app)
    app.include_router(cases_router)
    return app


@contextmanager
def open_env(app):
    with TestClient(app) as client:
        db = Database(app.state.db_path)
        try:
            yield SimpleNamespace(client=client, db=db, app=app)
        finally:
            db.close()


@pytest.fixture()
def env(tmp_path):
    with open_env(_make_app(tmp_path)) as e:
        yield e


def test_review_idempotency_replay_no_dup_review_record(env):
    _run(_seed(env.db, env.app.state.file_store))
    body = {"items": [{"case_id": "c1", "action": "adopt"}]}
    key = "idem-abc-123"

    r1 = env.client.post("/api/v1/cases/review", json=body,
                         headers={"Idempotency-Key": key})
    assert r1.status_code == 200, r1.json()

    r2 = env.client.post("/api/v1/cases/review", json=body,
                         headers={"Idempotency-Key": key})
    assert r2.status_code == 200
    # 重放内容一致
    assert r1.json() == r2.json()

    # 幂等重放不产生第二条评审记录
    rows = _run(ReviewDAO(env.db)._db.aquery(
        "SELECT COUNT(*) FROM review_record WHERE testcase_id = ?", ("c1",)
    ))
    assert rows[0][0] == 1


def test_review_idempotency_diff_body_409(env):
    _run(_seed(env.db, env.app.state.file_store))
    key = "idem-diff-1"
    env.client.post(
        "/api/v1/cases/review",
        json={"items": [{"case_id": "c1", "action": "adopt"}]},
        headers={"Idempotency-Key": key},
    )
    r = env.client.post(
        "/api/v1/cases/review",
        json={"items": [{"case_id": "c1", "action": "reject"}]},
        headers={"Idempotency-Key": key},
    )
    assert r.status_code == 409
