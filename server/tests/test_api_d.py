"""WP-28 API-D 验收测试：traces/snapshots 只读端点 + playground + kb 提案
两阶段 + import-linter 门禁（tech-design §5.5/§5.6 / dd §10.2 §8.7 §11.4
§10.3⑥ §15.2 场景 12）。

验收口径（WBS #28）：

- **场景 12a（import-linter 门禁）**：AST 扫描 graph/ runtime/ store/ 全部
  源码，禁止从 adapters 导入 ReMeWriter/WriteResult/UnavailableWriter——
  图运行代码路径物理上不可达 ReMe 写接口（PRD 7 硬性要求）；
- **场景 12b（confirm 幂等重放）**：confirmed 提案带同 Idempotency-Key
  重复确认 → 返回首次 WriteResult，writer 只被调用一次；
- traces：列表键集分页无重无漏 + stage/version 过滤；详情含候选
  kept/drop_reason、注入、引用闭环（referenced/hallucinated/weak）、降级；
- snapshots：列表元数据；详情 items 含偏移；full 档 items/{position} 按
  偏移读回全文；meta 档与越界 position 均 404；
- playground：临时 TaskContext 跑管线，漏斗/候选/注入随响应返回，
  **不落任何业务表**（retrieval_trace/context_snapshot 行数恒 0），
  overrides 白名单（top_k/query_paths/types）外键 400；
- kb 提案：创建只回明文令牌一次（库内仅 sha256）；confirm 校验
  令牌（400 KB_TOKEN_INVALID）/期限（409 PROPOSAL_EXPIRED 并置 expired）/
  状态（409）；写失败保持 pending + fail_count+1，有效期内同 token 同键
  可重试成功；verified=False → confirmed 且 needs_manual_check=true。

夹具：与 test_api_c 同构（portal loop TestClient + 独立 Database 播种），
app 挂载 debug/kb 路由，``app.state.kb_writer`` 默认 FakeWriter（可脚本化）。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
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

from fakes import FakeLLM, FakeReMeReader
from tester_agent.adapters.reme import (
    Entry,
    EntryType,
    ReMeReaderFactory,
    WriteResult,
)
from tester_agent.api.debug import router as debug_router
from tester_agent.api.error_handling import install_error_handling
from tester_agent.api.kb import router as kb_router
from tester_agent.domain import Candidate, DegradedStep, InjectedItem
from tester_agent.errors import KbUnreachable
from tester_agent.graph.main_graph import build_graph
from tester_agent.graph.registry import CASE_DESIGNER, GraphRegistry
from tester_agent.runtime.bus import EventBus
from tester_agent.runtime.context import AppContext
from tester_agent.runtime.runner import Runner, TaskRegistry
from tester_agent.store.db import Database, iso_ago, run_migrations, utcnow_iso
from tester_agent.store.models import (
    ConfigDAO,
    EventDAO,
    ProposalDAO,
    ProposalRow,
    SnapshotDAO,
    SnapshotRow,
    TaskDAO,
    TaskRow,
    TraceDAO,
    TraceRow,
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


def _entry(eid, *, title, content, etype, link_id="L1", story_id=None):
    return Entry(entry_id=eid, entry_version=f"v-{eid}", title=title,
                 content=content, entry_type=etype, link_id=link_id,
                 story_id=story_id, updated_at="2026-09-20T00:00:00Z",
                 raw={"summary": title})


ENTRIES = [
    _entry("a1", title="下单接口", content="POST /order 创建订单 参数",
           etype=EntryType.API, story_id="S1"),
    _entry("b1", title="订单业务规则", content="金额 校验 库存 订单",
           etype=EntryType.BUSINESS),
    _entry("si1", title="下单故事", content="下单 购物车",
           etype=EntryType.LINK_INDEX, story_id="S1"),
    # 链路级索引条目（story_id=None）：kb/tree 派生树的链路节点
    _entry("li1", title="下单链路", content="下单 链路 索引",
           etype=EntryType.LINK_INDEX, story_id=None),
]


class FakeWriter:
    """ReMeWriter 结构替身：脚本化返回/抛错，记录调用。"""

    def __init__(self, script: list | None = None):
        self._script = list(script or [])
        self.calls: list[tuple[str, dict]] = []

    async def write_proposal(self, token: str, proposal: dict) -> WriteResult:
        self.calls.append((token, proposal))
        item = self._script.pop(0) if self._script else WriteResult(
            ok=True, remote_ref="remote-1", verified=True, error_code=None
        )
        if isinstance(item, BaseException):
            raise item
        return item


# playground 用 SmartLLM：按 json_schema 分发（multi_query 为默认分支）
def _mq():
    return json.dumps({"keyword_queries": ["订单 下单"],
                       "rewrite_queries": ["创建订单 库存"]}, ensure_ascii=False)


class SmartLLM:
    def __init__(self):
        from tester_agent.adapters.llm import LLMResult

        self._result_cls = LLMResult
        self.chat_calls = 0

    async def chat(self, messages, *, model=None, temperature=None,
                   json_schema=None, timeout=None, stream_writer=None):
        self.chat_calls += 1
        required = set((json_schema or {}).get("required") or [])
        content = json.dumps({"scores": []}) if "scores" in required else _mq()
        return self._result_cls(
            content=content,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            model=model or "fake-model", finish_reason="stop", retries=0,
            latency_ms=1,
        )


# ---------- 播种 ----------


async def _seed_conv(db, ws=WS, conv=CONV):
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


async def _seed_task(db, task_id=TASK, *, ws=WS, conv=CONV):
    await _seed_conv(db, ws=ws, conv=conv)
    await TaskDAO(db).create(
        TaskRow.create(
            id=task_id, conversation_id=conv, workspace_id=ws,
            status="completed", current_stage="case_generate",
            langgraph_thread_id=f"th-{task_id}", graph_run_id=f"run-{task_id}",
            snapshot_level="meta",
        )
    )


async def _seed_trace(db, task_id, trace_id, *, stage="point_write", version=1,
                      node="point_write", batch_id="b0", created_at=None):
    cands = [
        Candidate(entry_id="b1", entry_version="v-b1", title="订单业务规则",
                  score=9.0, source_channel="raw:0",
                  entry_type=EntryType.BUSINESS, kept=True, latency_ms=3),
        Candidate(entry_id="x9", entry_version="v-x9", title="无关条目",
                  score=1.0, source_channel="keyword:1",
                  entry_type=EntryType.DEFECT, kept=False,
                  drop_reason="budget_cut", latency_ms=2),
    ]
    row = TraceRow.create(
        id=trace_id, task_id=task_id, graph_run_id=f"run-{task_id}",
        stage=stage, stage_version=version, node=node, batch_id=batch_id,
        query_variant={"queries": [{"channel": "raw", "text": "订单"}]},
        candidates=cands, injected_ids=["b1"],
        degraded=[DegradedStep(step="rerank", reason="llm_5xx",
                               fallback="rule_score")],
        now=created_at,
    )
    await TraceDAO(db).append(row)
    await TraceDAO(db).update_referenced(trace_id, ["b1"], ["b1"], ["ghost-1"])
    return row


async def _seed_snapshot(db, task_id, snap_id, *, stage="point_write",
                         with_path=None, items=None, created_at=None):
    row = SnapshotRow.create(
        id=snap_id, task_id=task_id, graph_run_id=f"run-{task_id}",
        stage=stage, stage_version=1, node=stage,
        prompt_template_ver="v1",
        items=items if items is not None else [
            InjectedItem(entry_id="b1", entry_version="v-b1",
                         title="订单业务规则", tokens_est=10, position=0),
        ],
        snapshot_path=with_path,
        total_tokens_est=10, budget=16000, truncated=False,
        model_ref={"provider": "fake", "model": "m", "temperature": 0.2,
                   "top_p": 1.0},
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        latencies={"multi_query": 1, "recall": 2},
        batch_id="b0", now=created_at,
    )
    await SnapshotDAO(db).put(row)
    return row


async def _seed_full_snapshot(db, store, task_id=TASK, ws=WS):
    """真实 full 档：SnapshotWriter 落 JSONL + items 带偏移 + DB 行。"""
    writer = store.open_snapshot_writer(ws, task_id, "point_write", 1,
                                        "point_write", "b0")
    try:
        lines = [
            {"entry_id": "b1", "entry_version": "v-b1", "title": "订单业务规则",
             "tokens_est": 4, "position": 0, "content": "规则正文-金额校验"},
            {"entry_id": "a1", "entry_version": "v-a1", "title": "下单接口",
             "tokens_est": 5, "position": 1, "content": "接口正文-创建订单"},
        ]
        items = []
        for ln in lines:
            off, length = writer.append(ln)
            items.append(InjectedItem(
                entry_id=ln["entry_id"], entry_version=ln["entry_version"],
                title=ln["title"], tokens_est=ln["tokens_est"],
                position=ln["position"], char_offset=off, byte_length=length,
            ))
        snap_id, rel_path = writer.snapshot_id, writer.rel_path
    finally:
        writer.close()
    await _seed_snapshot(db, task_id, snap_id, with_path=rel_path, items=items)
    return snap_id, lines


TOKEN = "tok-plain-1"
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()


async def _seed_proposal(db, pid="prop-1", *, ws=WS, task_id=TASK,
                         status="pending", expires_at=None, idem=None,
                         token_hash=TOKEN_HASH):
    await ProposalDAO(db).create(
        ProposalRow.create(
            id=pid, workspace_id=ws, task_id=task_id,
            payload={"op": "add", "entry": {"title": "新链路"}},
            status=status, confirm_token_hash=token_hash,
            idempotency_key=idem,
            expires_at=expires_at or _future(3600),
        )
    )
    return pid


def _future(seconds: float) -> str:
    return iso_ago(-seconds)


# ---------- app/lifespan 组装（test_api_c 同构 + debug/kb 路由） ----------


def _make_app(tmp_path: Path, *, llm=None, with_ctx=True,
              writer=None) -> FastAPI:
    tag = uuid.uuid4().hex[:8]
    db_path = tmp_path / f"appd-{tag}.db"
    ckpt_path = tmp_path / f"ckptd-{tag}.db"
    run_migrations(db_path)
    store = FileStore(tmp_path / f"wsfiles-d{tag}")
    reader = FakeReMeReader(list(ENTRIES))
    factory = ReMeReaderFactory()

    async def _builder(_kb_config):
        return reader

    factory.register("sdk", _builder)
    app_llm = llm if llm is not None else FakeLLM([_mq()])

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
        a.state.reader = reader
        a.state.kb_writer = writer if writer is not None else FakeWriter()
        if with_ctx:
            a.state.app_ctx = AppContext(
                db=db, file_store=store, llm=app_llm, reme_factory=factory,
                config=ConfigDAO(db), graphs=graphs, bus=bus, registry=registry,
            )
            a.state.runner = Runner(a.state.app_ctx, heartbeat_interval=0.05)
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
    app.include_router(debug_router)
    app.include_router(kb_router)
    return app


@contextmanager
def open_env(app: FastAPI):
    with TestClient(app) as client:
        db = Database(app.state.db_path)
        try:
            yield SimpleNamespace(client=client, db=db, app=app)
        finally:
            db.close()


@pytest.fixture()
def make_app(tmp_path):
    built: list[FastAPI] = []

    def build(**kw):
        app = _make_app(tmp_path, **kw)
        built.append(app)
        return app

    yield build


@pytest.fixture()
def env(make_app):
    with open_env(make_app()) as e:
        yield e


def _err(resp):
    return resp.json()["error"]


# ---------- traces ----------


def test_traces_empty(env):
    _run(_seed_task(env.db))
    r = env.client.get(f"/api/v1/tasks/{TASK}/traces")
    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None}


def test_traces_list_filters_and_pagination(env):
    _run(_seed_task(env.db))
    _run(_seed_trace(env.db, TASK, "tr-1", stage="point_write", version=1,
                     created_at="2026-09-26T00:00:01.000Z"))
    _run(_seed_trace(env.db, TASK, "tr-2", stage="link_identify", version=1,
                     created_at="2026-09-26T00:00:02.000Z"))
    _run(_seed_trace(env.db, TASK, "tr-3", stage="point_write", version=2,
                     created_at="2026-09-26T00:00:03.000Z"))

    # 全量列表：倒序（新→旧）
    r = env.client.get(f"/api/v1/tasks/{TASK}/traces")
    assert [i["id"] for i in r.json()["items"]] == ["tr-3", "tr-2", "tr-1"]
    # 摘要计数维度
    item = r.json()["items"][0]
    assert item["candidate_count"] == 2 and item["kept_count"] == 1
    assert item["injected_count"] == 1 and item["referenced_count"] == 1
    assert item["degraded_count"] == 1 and item["query_count"] == 1

    # stage/version 过滤
    r = env.client.get(f"/api/v1/tasks/{TASK}/traces", params={"stage": "point_write"})
    assert [i["id"] for i in r.json()["items"]] == ["tr-3", "tr-1"]
    r = env.client.get(
        f"/api/v1/tasks/{TASK}/traces",
        params={"stage": "point_write", "version": 2},
    )
    assert [i["id"] for i in r.json()["items"]] == ["tr-3"]

    # 键集分页 limit=2：无重无漏
    ids: list[str] = []
    cursor = None
    for _ in range(3):
        r = env.client.get(
            f"/api/v1/tasks/{TASK}/traces",
            params={"limit": 2, **({"cursor": cursor} if cursor else {})},
        )
        body = r.json()
        ids += [i["id"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert ids == ["tr-3", "tr-2", "tr-1"] and cursor is None

    # 非法游标 → 400
    r = env.client.get(f"/api/v1/tasks/{TASK}/traces", params={"cursor": "!!bad"})
    assert r.status_code == 400
    assert _err(r)["code"] == "VALIDATION_BODY"


def test_traces_task_404(env):
    r = env.client.get("/api/v1/tasks/nope/traces")
    assert r.status_code == 404
    assert _err(r)["code"] == "NOT_FOUND"


def test_trace_detail_full_fields(env):
    _run(_seed_task(env.db))
    _run(_seed_trace(env.db, TASK, "tr-1"))
    r = env.client.get("/api/v1/traces/tr-1")
    assert r.status_code == 200
    body = r.json()
    assert body["stage"] == "point_write" and body["batch_id"] == "b0"
    assert body["query_variant"]["queries"][0]["text"] == "订单"
    kept = {c["entry_id"]: c for c in body["candidates"]}
    assert kept["b1"]["kept"] is True
    assert kept["x9"]["kept"] is False
    assert kept["x9"]["drop_reason"] == "budget_cut"
    assert body["injected_ids"] == ["b1"]
    assert body["referenced_ids"] == ["b1"]
    assert body["weak_ref_ids"] == ["b1"]
    assert body["hallucinated_ids"] == ["ghost-1"]
    assert body["degraded"][0]["step"] == "rerank"


def test_trace_detail_404(env):
    r = env.client.get("/api/v1/traces/nope")
    assert r.status_code == 404


# ---------- snapshots ----------


def test_snapshots_list_and_detail(env):
    _run(_seed_task(env.db))
    _run(_seed_snapshot(env.db, TASK, "snap-1",
                        created_at="2026-09-26T00:00:01.000Z"))
    _run(_seed_snapshot(env.db, TASK, "snap-2",
                        created_at="2026-09-26T00:00:02.000Z"))

    r = env.client.get(f"/api/v1/tasks/{TASK}/snapshots")
    assert [i["id"] for i in r.json()["items"]] == ["snap-2", "snap-1"]
    item = r.json()["items"][0]
    assert item["item_count"] == 1 and item["total_tokens_est"] == 10
    assert item["budget"] == 16000 and item["truncated"] is False
    assert item["has_full"] is False  # 无 snapshot_path = meta 档
    assert item["prompt_template_ver"] == "v1"

    r = env.client.get("/api/v1/snapshots/snap-1")
    assert r.status_code == 200
    body = r.json()
    assert body["items"][0]["entry_id"] == "b1"
    assert body["items"][0]["position"] == 0
    assert body["model_ref"]["model"] == "m"
    assert body["usage"]["total_tokens"] == 15
    assert body["latencies"]["recall"] == 2


def test_snapshots_task_404_and_detail_404(env):
    assert env.client.get("/api/v1/tasks/nope/snapshots").status_code == 404
    assert env.client.get("/api/v1/snapshots/nope").status_code == 404


def test_snapshot_item_full_roundtrip(env):
    """full 档：按 items[position] 偏移读回注入知识全文（dd §4.4）。"""
    _run(_seed_task(env.db))
    store = env.app.state.file_store
    snap_id, lines = _run(_seed_full_snapshot(env.db, store))

    for pos, ln in enumerate(lines):
        r = env.client.get(f"/api/v1/snapshots/{snap_id}/items/{pos}")
        assert r.status_code == 200
        body = r.json()
        assert body["position"] == pos
        assert body["entry_id"] == ln["entry_id"]
        assert body["entry_version"] == ln["entry_version"]
        assert body["title"] == ln["title"]
        assert body["content"] == ln["content"]


def test_snapshot_item_meta_and_position_404(env):
    _run(_seed_task(env.db))
    _run(_seed_snapshot(env.db, TASK, "snap-meta"))  # 无 snapshot_path
    r = env.client.get("/api/v1/snapshots/snap-meta/items/0")
    assert r.status_code == 404

    store = env.app.state.file_store
    snap_id, _ = _run(_seed_full_snapshot(env.db, store))
    r = env.client.get(f"/api/v1/snapshots/{snap_id}/items/9")
    assert r.status_code == 404


# ---------- playground ----------


def _count(db, table: str) -> int:
    row = _run(db.aquery_one(f"SELECT COUNT(*) AS c FROM {table}"))
    return row["c"]


def test_playground_happy_no_business_writes(env):
    """dd §8.7：漏斗/候选/注入随响应返回，不落任何业务表。"""
    _run(_seed_task(env.db))  # workspace + task 仅用于归属；管线不写业务表
    r = env.client.post(
        f"/api/v1/workspaces/{WS}/retrieval/playground",
        json={"query": "订单 下单", "stage": "point_write"},
    )
    assert r.status_code == 200
    body = r.json()
    funnel = body["funnel"]
    assert funnel["total"] == funnel["kept"] + funnel["errors"] + sum(
        funnel["dropped"].values()
    )
    assert funnel["total"] >= 1
    # 候选带 kept/drop_reason 全集；注入条目 ⊆ kept
    kept_ids = {c["entry_id"] for c in body["candidates"] if c["kept"]}
    injected_ids = {i["entry_id"] for i in body["injected"]}
    assert injected_ids and injected_ids <= kept_ids
    assert body["token_est"] > 0
    assert "recall" in body["latencies"] or body["latencies"]
    # 不落业务表
    assert _count(env.db, "retrieval_trace") == 0
    assert _count(env.db, "context_snapshot") == 0


def test_playground_overrides_and_validation(make_app):
    app = make_app(llm=SmartLLM())
    with open_env(app) as env:
        _run(_seed_task(env.db))
        # types 覆盖生效：仅 api+business 类型进候选（LINK_INDEX 被裁）
        r = env.client.post(
            f"/api/v1/workspaces/{WS}/retrieval/playground",
            json={"query": "订单", "stage": "point_write",
                  "overrides": {"top_k": 5, "query_paths": 1,
                                "types": ["api", "business"]}},
        )
        assert r.status_code == 200
        cand_types = {c["entry_type"] for c in r.json()["candidates"]}
        assert cand_types <= {"api", "business"}

        # 未知 override 键 → 400
        r = env.client.post(
            f"/api/v1/workspaces/{WS}/retrieval/playground",
            json={"query": "订单", "stage": "point_write",
                  "overrides": {"nope": 1}},
        )
        assert r.status_code == 400
        assert _err(r)["code"] == "VALIDATION_BODY"

        # 非法类型值 → 400
        r = env.client.post(
            f"/api/v1/workspaces/{WS}/retrieval/playground",
            json={"query": "订单", "stage": "point_write",
                  "overrides": {"types": ["nope"]}},
        )
        assert r.status_code == 400

        # 未知阶段 → 400
        r = env.client.post(
            f"/api/v1/workspaces/{WS}/retrieval/playground",
            json={"query": "订单", "stage": "no_such_stage"},
        )
        assert r.status_code == 400


def test_playground_workspace_404_and_no_ctx_409(make_app):
    app = make_app()
    with open_env(app) as env:
        r = env.client.post(
            "/api/v1/workspaces/nope/retrieval/playground",
            json={"query": "订单", "stage": "point_write"},
        )
        assert r.status_code == 404

    app2 = make_app(with_ctx=False)
    with open_env(app2) as env2:
        _run(_seed_task(env2.db))
        r = env2.client.post(
            f"/api/v1/workspaces/{WS}/retrieval/playground",
            json={"query": "订单", "stage": "point_write"},
        )
        assert r.status_code == 409
        assert _err(r)["code"] == "TASK_STATE_CONFLICT"


# ---------- kb/tree ----------


def test_kb_tree(env):
    _run(_seed_task(env.db))
    r = env.client.get(f"/api/v1/workspaces/{WS}/kb/tree")
    assert r.status_code == 200
    body = r.json()
    links = body["links"] if isinstance(body, dict) else body
    assert links  # FakeReMeReader 由 ENTRIES 派生树
    assert env.client.get("/api/v1/workspaces/nope/kb/tree").status_code == 404


# ---------- kb 提案 ----------


def test_proposal_create_and_token_only_once(env):
    _run(_seed_task(env.db))
    r = env.client.post(
        "/api/v1/kb/proposals",
        json={"workspace_id": WS, "task_id": TASK,
              "payload": {"op": "add"}},
    )
    assert r.status_code == 201
    assert r.headers["Location"].endswith(r.json()["id"])
    body = r.json()
    assert len(body["confirm_token"]) == 64  # 32 字节 hex
    assert body["expires_at"] > utcnow_iso()

    # 库内只存 sha256，不存明文
    row = _run(ProposalDAO(env.db).get(body["id"]))
    assert row.confirm_token_hash == hashlib.sha256(
        body["confirm_token"].encode()
    ).hexdigest()
    assert body["confirm_token"] not in json.dumps(row.__dict__)


def test_proposal_create_guards(env):
    _run(_seed_task(env.db))
    # 工作区不存在 → 404
    r = env.client.post("/api/v1/kb/proposals",
                        json={"workspace_id": "nope", "payload": {}})
    assert r.status_code == 404
    # 任务不存在 → 404
    r = env.client.post("/api/v1/kb/proposals",
                        json={"workspace_id": WS, "task_id": "nope",
                              "payload": {}})
    assert r.status_code == 404
    # 跨工作区任务 → 404 不暴露存在性
    _run(_seed_task(env.db, task_id="task-2", ws="ws-2", conv="conv-2"))
    r = env.client.post("/api/v1/kb/proposals",
                        json={"workspace_id": WS, "task_id": "task-2",
                              "payload": {}})
    assert r.status_code == 404


def test_proposals_list_filter_pagination(env):
    _run(_seed_task(env.db))
    _run(_seed_proposal(env.db, "p1"))
    _run(_seed_proposal(env.db, "p2", status="confirmed"))
    _run(_seed_proposal(env.db, "p3"))

    r = env.client.get(f"/api/v1/workspaces/{WS}/kb/proposals")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 3
    assert "confirm_token_hash" not in json.dumps(items)  # 永不泄漏哈希

    r = env.client.get(f"/api/v1/workspaces/{WS}/kb/proposals",
                       params={"status": "pending"})
    assert {i["id"] for i in r.json()["items"]} == {"p1", "p3"}

    r = env.client.get(f"/api/v1/workspaces/{WS}/kb/proposals",
                       params={"limit": 2})
    assert len(r.json()["items"]) == 2 and r.json()["next_cursor"]
    cursor = r.json()["next_cursor"]
    r2 = env.client.get(f"/api/v1/workspaces/{WS}/kb/proposals",
                        params={"limit": 2, "cursor": cursor})
    assert len(r2.json()["items"]) == 1 and r2.json()["next_cursor"] is None

    assert env.client.get(
        "/api/v1/workspaces/nope/kb/proposals").status_code == 404


def test_confirm_happy_and_replay_same_key(make_app):
    """场景 12b：confirm 幂等重放返回首次结果，writer 只调一次。"""
    writer = FakeWriter()
    app = make_app(writer=writer)
    with open_env(app) as env:
        _run(_seed_task(env.db))
        _run(_seed_proposal(env.db, "p1"))
        payload = {"confirm_token": TOKEN}
        headers = {"Idempotency-Key": "idem-1"}

        r = env.client.post("/api/v1/kb/proposals/p1/confirm",
                            json=payload, headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["remote_ref"] == "remote-1"
        assert body["verified"] is True
        assert body["needs_manual_check"] is False
        row = _run(ProposalDAO(env.db).get("p1"))
        assert row.status == "confirmed" and row.confirmed_at
        assert row.write_result_dict()["remote_ref"] == "remote-1"

        # 同键重放：同结果，writer 不再次调用
        r2 = env.client.post("/api/v1/kb/proposals/p1/confirm",
                             json=payload, headers=headers)
        assert r2.status_code == 200 and r2.json() == body
        assert len(writer.calls) == 1

        # 已确认 + 不同幂等键 → 409
        r3 = env.client.post("/api/v1/kb/proposals/p1/confirm",
                             json=payload,
                             headers={"Idempotency-Key": "idem-2"})
        assert r3.status_code == 409
        assert _err(r3)["code"] == "TASK_STATE_CONFLICT"
        assert len(writer.calls) == 1


def test_confirm_token_and_idem_guards(env):
    _run(_seed_task(env.db))
    _run(_seed_proposal(env.db, "p1"))

    # 错令牌 → 400 KB_TOKEN_INVALID（dd §10.3⑥）
    r = env.client.post("/api/v1/kb/proposals/p1/confirm",
                        json={"confirm_token": "wrong"},
                        headers={"Idempotency-Key": "k1"})
    assert r.status_code == 400
    assert _err(r)["code"] == "KB_TOKEN_INVALID"

    # 缺令牌字段 → 400（pydantic → VALIDATION_BODY）
    r = env.client.post("/api/v1/kb/proposals/p1/confirm",
                        json={}, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 400

    # 缺 Idempotency-Key 头 → 400（tech-design §5.0：kb confirm 必带）
    r = env.client.post("/api/v1/kb/proposals/p1/confirm",
                        json={"confirm_token": TOKEN})
    assert r.status_code == 400
    assert _err(r)["code"] == "VALIDATION_BODY"

    # 提案不存在 → 404
    r = env.client.post("/api/v1/kb/proposals/nope/confirm",
                        json={"confirm_token": TOKEN},
                        headers={"Idempotency-Key": "k1"})
    assert r.status_code == 404

    # 上述守卫均未落账：仍 pending、未绑定键
    row = _run(ProposalDAO(env.db).get("p1"))
    assert row.status == "pending" and row.idempotency_key is None


def test_confirm_expired_409(env):
    _run(_seed_task(env.db))
    _run(_seed_proposal(env.db, "p1", expires_at=iso_ago(60)))  # 已过期
    r = env.client.post("/api/v1/kb/proposals/p1/confirm",
                        json={"confirm_token": TOKEN},
                        headers={"Idempotency-Key": "k1"})
    assert r.status_code == 409
    assert _err(r)["code"] == "PROPOSAL_EXPIRED"
    # 惰性置 expired 终态
    assert _run(ProposalDAO(env.db).get("p1")).status == "expired"
    # 终态再确认 → 409 状态冲突
    r = env.client.post("/api/v1/kb/proposals/p1/confirm",
                        json={"confirm_token": TOKEN},
                        headers={"Idempotency-Key": "k1"})
    assert r.status_code == 409
    assert _err(r)["code"] == "TASK_STATE_CONFLICT"


def test_confirm_write_fail_then_retry(make_app):
    """dd §11.4 失败语义：写失败保持 pending + fail_count，有效期内同
    token 同幂等键可重试成功。"""
    writer = FakeWriter([
        WriteResult(ok=False, remote_ref=None, verified=False,
                    error_code="remote_rejected"),
        WriteResult(ok=True, remote_ref="remote-9", verified=True,
                    error_code=None),
    ])
    app = make_app(writer=writer)
    with open_env(app) as env:
        _run(_seed_task(env.db))
        _run(_seed_proposal(env.db, "p1"))
        payload = {"confirm_token": TOKEN}
        headers = {"Idempotency-Key": "k1"}

        r = env.client.post("/api/v1/kb/proposals/p1/confirm",
                            json=payload, headers=headers)
        assert r.status_code == 502
        err = _err(r)
        assert err["code"] == "KB_UNREACHABLE" and err["retryable"] is True
        assert err["details"]["error_code"] == "remote_rejected"
        assert err["details"]["fail_count"] == 1
        row = _run(ProposalDAO(env.db).get("p1"))
        assert row.status == "pending" and row.fail_count == 1
        assert row.idempotency_key == "k1"  # 键已绑定

        # 同 token 同键重试 → 成功
        r = env.client.post("/api/v1/kb/proposals/p1/confirm",
                            json=payload, headers=headers)
        assert r.status_code == 200 and r.json()["remote_ref"] == "remote-9"
        row = _run(ProposalDAO(env.db).get("p1"))
        assert row.status == "confirmed" and row.fail_count == 1

    # writer 抛异常路径：同样 pending + fail_count
    writer2 = FakeWriter([KbUnreachable("boom")])
    app2 = make_app(writer=writer2)
    with open_env(app2) as env2:
        _run(_seed_task(env2.db))
        _run(_seed_proposal(env2.db, "p1"))
        r = env2.client.post("/api/v1/kb/proposals/p1/confirm",
                             json={"confirm_token": TOKEN},
                             headers={"Idempotency-Key": "k1"})
        assert r.status_code == 502
        row = _run(ProposalDAO(env2.db).get("p1"))
        assert row.status == "pending" and row.fail_count == 1


def test_confirm_needs_manual_check(make_app):
    """verified=False → confirmed 但 write_result 标 needs_manual_check（S6）。"""
    writer = FakeWriter([WriteResult(ok=True, remote_ref="remote-7",
                                     verified=False, error_code=None)])
    app = make_app(writer=writer)
    with open_env(app) as env:
        _run(_seed_task(env.db))
        _run(_seed_proposal(env.db, "p1"))
        r = env.client.post("/api/v1/kb/proposals/p1/confirm",
                            json={"confirm_token": TOKEN},
                            headers={"Idempotency-Key": "k1"})
        assert r.status_code == 200
        assert r.json()["verified"] is False
        assert r.json()["needs_manual_check"] is True
        row = _run(ProposalDAO(env.db).get("p1"))
        assert row.status == "confirmed"
        assert row.write_result_dict()["needs_manual_check"] is True


def test_confirm_default_writer_unavailable(env):
    """默认 UnavailableWriter：确认 → 502，提案保持 pending。"""
    env.app.state.kb_writer = None  # 触发 _writer 回退 UnavailableWriter
    _run(_seed_task(env.db))
    _run(_seed_proposal(env.db, "p1"))
    r = env.client.post("/api/v1/kb/proposals/p1/confirm",
                        json={"confirm_token": TOKEN},
                        headers={"Idempotency-Key": "k1"})
    assert r.status_code == 502
    assert _err(r)["details"]["reason"] == "writer_not_registered"
    assert _run(ProposalDAO(env.db).get("p1")).status == "pending"


# ---------- 场景 12a：import-linter 门禁 ----------


def test_import_gate_graph_runtime_store_no_writer():
    """dd §15.2 场景 12 / tech-design §2：图运行代码路径（graph/ runtime/
    store/）禁止 import ReMe 写接口——AST 级静态门禁。"""
    import tester_agent

    pkg_root = Path(tester_agent.__file__).resolve().parent
    banned = {"ReMeWriter", "WriteResult", "UnavailableWriter"}
    violations: list[str] = []
    for sub in ("graph", "runtime", "store"):
        for path in sorted((pkg_root / sub).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                module = ""
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                    module = ""
                if (module.endswith("adapters.reme") or "adapters" in module) \
                        and banned & set(names):
                    violations.append(
                        f"{path.relative_to(pkg_root)}: {sorted(banned & set(names))}"
                    )
    assert violations == [], f"L3 禁入 ReMe 写接口：{violations}"
