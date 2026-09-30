"""WP-32 Task 15：上下文调试 API + playground + SSE。

验收（plan Task 15 / spec §8/§10.1）：
- 两 owner GET 视图 200；跨 workspace 404 不暴露存在性；
- evictions key-set 分页无重复无遗漏 + partition 过滤；
- playground 前后业务表行数快照一致（含 context_journal 零写入）；
- journal 故障注入 → 端点 degraded 字段不 500；
- SSE：context_policy_changed 经总线可抓到。
"""

from __future__ import annotations

import asyncio
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
from tester_agent.api.context import router as context_router
from tester_agent.api.error_handling import install_error_handling
from tester_agent.api.events import CONTEXT_POLICY_CHANGED, make_events_router
from tester_agent.context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryRefs,
    EntryStatus,
    ProfileName,
)
from tester_agent.context.registry import ContextRegistry
from tester_agent.graph.main_graph import build_graph
from tester_agent.graph.registry import CASE_DESIGNER, GraphRegistry
from tester_agent.runtime.bus import EventBus
from tester_agent.runtime.context import AppContext
from tester_agent.runtime.runner import TaskRegistry
from tester_agent.store.db import Database, run_migrations, utcnow_iso
from tester_agent.store.models import (
    ConfigDAO,
    ContextJournalDAO,
    ContextJournalRow,
    EventDAO,
    TaskDAO,
    TaskRow,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

WS, CONV, TASK = "ws-ctx", "conv-ctx", "task-ctx"
TS0 = "2026-09-28T00:00:00.000Z"
TS1 = "2026-09-28T00:00:01.000Z"
TS2 = "2026-09-28T00:00:02.000Z"
TS3 = "2026-09-28T00:00:03.000Z"


def _run(coro):
    return asyncio.run(coro)


def _err(r):
    return r.json()["error"]


class _BoomJournal:
    """落库必失败：用于注入 store.degraded_journal。"""

    async def record(self, records):
        raise RuntimeError("journal boom")


# ---------- 播种 / app ----------


async def _seed_conv(db, ws=WS, conv=CONV):
    row = await db.aquery_one("SELECT id FROM workspace WHERE id = ?", (ws,))
    if row is None:
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(
                id=ws,
                name="ws",
                kb_config={
                    "kb_id": "kb-1",
                    "knowledge_bases_dir": "",
                    "knowledge_dir": "knowledge",
                    "create_knowledge_base": False,
                    "options": {},
                },
            )
        )
    await db.aexecute(
        "INSERT OR IGNORE INTO conversation "
        "(id, workspace_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (conv, ws, "c", TS0, TS0),
    )


async def _seed_task(db, task_id=TASK, *, ws=WS, conv=CONV):
    await _seed_conv(db, ws=ws, conv=conv)
    await TaskDAO(db).create(
        TaskRow.create(
            id=task_id,
            conversation_id=conv,
            workspace_id=ws,
            status="running",
            current_stage="link_identify",
            langgraph_thread_id=f"th-{task_id}",
            graph_run_id=f"run-{task_id}",
            snapshot_level="meta",
        )
    )


def _entry(
    eid: str,
    *,
    partition=ContextPartition.P2,
    kind=EntryKind.REFLECTION,
    content="正文",
    digest="摘要",
    status=EntryStatus.ACTIVE,
    created_at=TS0,
    tokens_est=4,
    snapshot_id=None,
):
    return ContextEntry(
        entry_id=eid,
        partition=partition,
        entry_kind=kind,
        status=status,
        content=content,
        digest=digest,
        tokens_est=tokens_est,
        refs=EntryRefs(snapshot_id=snapshot_id),
        created_at=created_at,
    )


def _make_app(tmp_path: Path) -> FastAPI:
    tag = uuid.uuid4().hex[:8]
    db_path = tmp_path / f"appctx-{tag}.db"
    ckpt_path = tmp_path / f"ckptctx-{tag}.db"
    run_migrations(db_path)
    store = FileStore(tmp_path / f"wsfiles-ctx{tag}")
    factory = ReMeReaderFactory()
    registry = ContextRegistry()

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        conn = await aiosqlite.connect(str(ckpt_path), check_same_thread=False)
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        g = build_graph(saver)
        db = Database(db_path)
        graphs = GraphRegistry.from_graph(CASE_DESIGNER, g)
        bus = EventBus(EventDAO(db))
        a.state.db = db
        a.state.db_path = db_path
        a.state.file_store = store
        a.state.bus = bus
        a.state.registry = TaskRegistry(TaskDAO(db))
        a.state.reme_factory = factory
        a.state.app_ctx = AppContext(
            db=db,
            file_store=store,
            llm=FakeLLM([]),
            reme_factory=factory,
            config=ConfigDAO(db),
            graphs=graphs,
            bus=bus,
            registry=a.state.registry,
            context_registry=registry,
        )
        try:
            yield
        finally:
            await graphs.aclose()
            await conn.close()
            db.close()

    app = FastAPI(lifespan=lifespan)
    install_error_handling(app)
    app.include_router(context_router)
    app.include_router(make_events_router())
    return app


@contextmanager
def open_env(app: FastAPI):
    with TestClient(app) as client:
        db = Database(app.state.db_path)
        try:
            yield SimpleNamespace(
                client=client,
                db=db,
                app=app,
                registry=app.state.app_ctx.context_registry,
            )
        finally:
            db.close()


@pytest.fixture
def make_app(tmp_path):
    def _factory(**_kw):
        return _make_app(tmp_path)

    return _factory


@pytest.fixture
def env(make_app):
    with open_env(make_app()) as e:
        yield e


def _count(db, table: str) -> int:
    row = _run(db.aquery_one(f"SELECT COUNT(*) AS c FROM {table}"))
    return row["c"]


# ---------- GET 视图 ----------


def test_get_task_and_conversation_context_200(env):
    _run(_seed_task(env.db))
    store_t = env.registry.start_owner(
        owner_type="task", owner_id=TASK, workspace_id=WS
    )
    store_c = env.registry.start_owner(
        owner_type="conversation", owner_id=CONV, workspace_id=WS
    )
    _run(
        store_t.append(
            _entry(
                "kb1",
                partition=ContextPartition.P1,
                kind=EntryKind.KB_BLOCK,
                content="机密段落全文不应外泄",
                digest="kb摘要",
                snapshot_id="snap-1",
            )
        )
    )
    _run(store_t.append(_entry("p2a", content="决策要点", digest="决策")))
    _run(store_t.set_goal("完成链路识别", reason="test"))
    _run(store_c.append(_entry("turn1", kind=EntryKind.CHAT_TURN, content="你好")))

    r = env.client.get(f"/api/v1/tasks/{TASK}/context")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["owner"]["scope"] == "task"
    assert body["owner"]["owner_id"] == TASK
    assert body["goal"] == "完成链路识别"
    p1 = next(e for e in body["entries"] if e["entry_id"] == "kb1")
    assert p1["content"] == ""  # P1 不含 passage 全文
    assert p1["digest"] == "kb摘要"
    assert p1["refs"]["snapshot_id"] == "snap-1"
    p2 = next(e for e in body["entries"] if e["entry_id"] == "p2a")
    assert "决策" in p2["content"]
    assert body["totals"]["P1"] >= 1
    assert body["totals"]["P2"] >= 1

    r2 = env.client.get(f"/api/v1/conversations/{CONV}/context")
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["owner"]["scope"] == "conversation"
    assert b2["owner"]["owner_id"] == CONV
    assert any(e["entry_id"] == "turn1" for e in b2["entries"])


def test_cross_workspace_context_404(env):
    """store.workspace_id ≠ 资源归属 → 404，不暴露存在性。"""
    _run(_seed_task(env.db))
    env.registry.start_owner(
        owner_type="task", owner_id=TASK, workspace_id="ws-other"
    )
    r = env.client.get(f"/api/v1/tasks/{TASK}/context")
    assert r.status_code == 404
    assert _err(r)["code"] == "NOT_FOUND"

    r2 = env.client.get("/api/v1/tasks/no-such-task/context")
    assert r2.status_code == 404


# ---------- evictions ----------


def _jrow(
    id_: str,
    *,
    action: str = "demote",
    partition: str = "P2",
    created_at: str = TS0,
    owner_id: str = TASK,
):
    return ContextJournalRow(
        id=id_,
        workspace_id=WS,
        owner_type="task",
        owner_id=owner_id,
        task_id=owner_id,
        conversation_id=None,
        partition=partition,
        entry_id=f"e-{id_}",
        entry_kind="reflection",
        action=action,
        reason="budget_cut",
        policy_version="cp-v1",
        created_at=created_at,
    )


def test_evictions_pagination_and_partition(env):
    _run(_seed_task(env.db))
    dao = ContextJournalDAO(env.db)
    _run(
        dao.put_batch(
            [
                _jrow("a", action="append", created_at=TS0),  # 非淘汰，应过滤
                _jrow("b", action="demote", partition="P1", created_at=TS1),
                _jrow("c", action="evict", partition="P2", created_at=TS2),
                _jrow("d", action="pin", partition="P2", created_at=TS3),
            ]
        )
    )
    r1 = env.client.get(
        f"/api/v1/tasks/{TASK}/context/evictions", params={"limit": 2}
    )
    assert r1.status_code == 200
    page1 = r1.json()
    ids1 = [it["id"] for it in page1["items"]]
    # append 被过滤；倒序 pin/evict/demote
    assert ids1 == ["d", "c"]
    assert page1["next_cursor"] is not None

    r2 = env.client.get(
        f"/api/v1/tasks/{TASK}/context/evictions",
        params={"limit": 2, "cursor": page1["next_cursor"]},
    )
    page2 = r2.json()
    ids2 = [it["id"] for it in page2["items"]]
    assert ids2 == ["b"]
    assert page2["next_cursor"] is None
    all_ids = ids1 + ids2
    assert len(all_ids) == len(set(all_ids)) == 3
    assert "a" not in all_ids

    r3 = env.client.get(
        f"/api/v1/tasks/{TASK}/context/evictions",
        params={"partition": "P1"},
    )
    assert [it["id"] for it in r3.json()["items"]] == ["b"]


# ---------- playground ----------


def test_playground_zero_business_writes(env):
    _run(_seed_conv(env.db))
    before = {
        t: _count(env.db, t)
        for t in (
            "context_journal",
            "retrieval_trace",
            "context_snapshot",
            "task",
            "stage_artifact",
            "testcase",
            "message",
        )
    }
    r = env.client.post(
        f"/api/v1/workspaces/{WS}/context/playground",
        json={
            "profile": ProfileName.PLAN.value,
            "goal": "支付下单",
            "p1_items": [
                {
                    "entry_id": "kb-pg",
                    "content": "订单金额校验规则" * 20,
                    "digest": "金额校验",
                }
            ],
            "p2_entries": [
                {
                    "entry_id": "ref-pg",
                    "content": "旧讨论" * 80,
                    "digest": "旧讨论",
                    "entry_kind": "reflection",
                }
            ],
            "scope": {"phase": "design", "step_seq": 2},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report"]["profile"] == "plan"
    assert body["report"]["included"] or body["messages"]
    assert "degraded" in body
    after = {t: _count(env.db, t) for t in before}
    assert after == before


def test_playground_workspace_404(env):
    r = env.client.post(
        "/api/v1/workspaces/nope/context/playground",
        json={"profile": "plan"},
    )
    assert r.status_code == 404


# ---------- journal degraded + SSE ----------


def test_journal_fault_returns_degraded_not_500(env):
    _run(_seed_task(env.db))
    store = env.registry.start_owner(
        owner_type="task",
        owner_id=TASK,
        workspace_id=WS,
        journal=_BoomJournal(),
    )
    _run(store.append(_entry("x1", content="将被降级")))
    _run(store.demote("x1", "step_window"))
    assert store.degraded_journal  # 内存有滞留

    r = env.client.get(f"/api/v1/tasks/{TASK}/context")
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is not None
    assert body["degraded"]["journal_pending"] >= 1


def test_sse_context_policy_changed_on_degraded_view(env):
    """GET 视图发现 journal 降级时发 context_policy_changed，总线可抓到。"""
    _run(_seed_task(env.db))
    store = env.registry.start_owner(
        owner_type="task",
        owner_id=TASK,
        workspace_id=WS,
        journal=_BoomJournal(),
    )
    _run(store.append(_entry("x2", content="降级源")))
    _run(store.demote("x2", "budget_cut"))

    # TestClient 与 bus 共享 lifespan loop：用 EventDAO 回放断言落库事件。
    r = env.client.get(f"/api/v1/tasks/{TASK}/context")
    assert r.status_code == 200
    assert r.json()["degraded"]["journal_pending"] >= 1
    rows = _run(EventDAO(env.db).list_after(TASK, 0))
    types = [row.type for row in rows]
    assert CONTEXT_POLICY_CHANGED in types
    payload = next(
        row.payload_dict() for row in rows if row.type == CONTEXT_POLICY_CHANGED
    )
    assert payload.get("reason") == "journal_degraded"
    assert payload.get("pending", 0) >= 1
    assert CONTEXT_POLICY_CHANGED == "context_policy_changed"
