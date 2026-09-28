"""WP-26 API-B 验收测试：tasks create/get/run/cancel/confirm(含 modify)/
answer/rollback + regenerate 入口（tech-design §5.2 §5.3 / dd §10.2 §10.3
§7.6 §11.2）。

验收口径（WBS #26 场景 4/5 及 §10.2 §10.3 §7.6 全端点）：
- 场景 4：迟到版本 confirm → 409 VERSION_CONFLICT；
- 场景 5：waiting_* 取消即生效（直接 200 aborted，无在飞批次）；
- create 先写 requirement.md 后落 task 行（初始 waiting_input）；
- run 返回 events_url + resume_from 批次游标；重复 run → 409；
- confirm 放行越 gate / modify 产生 v2(user_revised) + checkpoint_revision
  消息 + aupdate_state 写新 plan 后 resume；非 active/跨任务/阶段不符拒绝；
- answer 经 Command(resume=answers) 恢复函数式 interrupt + clarification_qa
  消息留痕；
- rollback 协议落账 + Runner 接管（新派生 thread ::run1 从目标阶段重跑）；
- regenerate 入口：lineage 挂旧 case、keep_original 语义、状态冲突不误置
  failed（前置校验失败保持原状态）。

夹具：裸 FastAPI + 自定义 lifespan（与 main.create_app 同序注入 test 组件：
Database/EventBus/FileStore/ReMeReaderFactory/GraphRegistry(假节点主图)/
TaskRegistry/Runner），TestClient 用 ``with`` 保持单一事件循环，后台 Runner
任务在 portal loop 上持续运行；测试线程经独立 Database 连接 + asyncio.run
轮询 DB 终态（Database 为单线程 executor 同步包装，跨 loop 安全）。
"""

from __future__ import annotations

import asyncio
import contextlib
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
from langgraph.types import interrupt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeLLM, FakeReMeReader
from tester_agent.adapters.reme import Entry, EntryType, ReMeReaderFactory
from tester_agent.api.error_handling import install_error_handling
from tester_agent.api.tasks import router as tasks_router
from tester_agent.domain import (
    CaseRecord,
    Lineage,
    LinkPlan,
    LinkRef,
    PointPlan,
    ReviewStatus,
    StoryRef,
    TestPoint as PointModel,
    TraceRefs,
)
from tester_agent.errors import (
    AppError,
    NotFoundError,
    TaskCancelled,
    TaskStateConflict,
)
from tester_agent.graph.constants import (
    STAGE_CASE_GENERATE,
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from tester_agent.graph.main_graph import build_graph
from tester_agent.graph.registry import CASE_DESIGNER, GraphRegistry
from tester_agent.tools.capabilities import caps_from_stage_nodes
from tester_agent.runtime.bus import EventBus
from tester_agent.runtime.context import AppContext
from tester_agent.runtime.regenerate import regenerate_cases
from tester_agent.runtime.runner import Runner, TaskRegistry
from tester_agent.store.db import Database, iso_ago, run_migrations
from tester_agent.store.models import (
    ArtifactDAO,
    ArtifactRow,
    CaseRow,
    ConfigDAO,
    EventDAO,
    MessageDAO,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

WS, CONV, TASK = "ws-1", "conv-1", "task-1"
KB_CONFIG = {"mode": "sdk", "target": "/tmp/reme", "kb_id": "kb-1", "options": {}}
REQUIREMENT_MD = "# 需求\n\n## 下单\n用户提交订单后扣减库存。\n"
C1 = "h2-1"


def _run(coro):
    return asyncio.run(coro)


# ---------- 产物 fixture（与 WP-18/20 测试同构） ----------

LINK_PLAN = LinkPlan(
    links=[LinkRef(link_id="L1", title="订单", summary="下单", hit=True,
                   entry_id="l1", confidence=0.9, story_ids=["S1"])],
    stories=[
        StoryRef(story_id="S1", link_id="L1", title="下单", summary="购物车下单",
                 hit=True, entry_id="s1", confidence=0.9, rationale="r",
                 related_clause_ids=[C1]),
    ],
    new_suggestions=[],
)
POINT_PLAN = PointPlan(points=[
    PointModel(point_id="pt-1-1", story_id="S1", title="正常下单", angle="正常",
               method="场景法", clause_ids=[C1], source_entry_ids=["b1"],
               priority="P0"),
])
LINK_PLAN_DICT = LINK_PLAN.model_dump()
POINT_PLAN_DICT = POINT_PLAN.model_dump()
REVISED_LINK_PLAN_DICT = json.loads(json.dumps(LINK_PLAN_DICT, ensure_ascii=False))
REVISED_LINK_PLAN_DICT["stories"][0]["title"] = "下单-修订"


def _entry(eid, *, title, content, etype, link_id="L1", story_id=None):
    return Entry(entry_id=eid, entry_version=f"v-{eid}", title=title,
                 content=content, entry_type=etype, link_id=link_id,
                 story_id=story_id, updated_at="2026-09-20T00:00:00Z",
                 raw={"summary": title})


ENTRIES = [
    _entry("a1", title="下单接口", content="POST /order 创建订单 参数",
           etype=EntryType.API, story_id="S1"),
    _entry("b1", title="订单业务规则", content="金额 校验 库存",
           etype=EntryType.BUSINESS),
    _entry("si1", title="下单故事", content="下单 购物车",
           etype=EntryType.LINK_INDEX, story_id="S1"),
]


# ---------- SmartLLM（按 json_schema 区分 multi_query/rerank/case） ----------


def _mq():
    return json.dumps({"keyword_queries": ["订单 下单"],
                       "rewrite_queries": ["创建订单 库存"]}, ensure_ascii=False)


def _rr():
    return json.dumps({"scores": [
        {"entry_id": e.entry_id, "score": 90, "reason": "f"} for e in ENTRIES
    ]}, ensure_ascii=False)


def _case(title, cid):
    return {"title": title, "priority": "P1", "preconditions": [],
            "steps": [{"seq": 1, "action": f"执行-{title}", "expect": "通过"}],
            "test_data": None,
            "trace_refs": {"clause_ids": [cid], "entry_ids": []}}


class SmartLLM:
    """按 json_schema.required 即时分发应答（多调用穿插场景脚本队列会错位）。"""

    def __init__(self, case_responses: list[str]):
        from tester_agent.adapters.llm import LLMResult

        self._result_cls = LLMResult
        self._case_responses = list(case_responses)
        self.chat_calls = 0
        self.kinds: list[str] = []

    async def chat(self, messages, *, model=None, temperature=None,
                   json_schema=None, timeout=None, stream_writer=None):
        self.chat_calls += 1
        required = set((json_schema or {}).get("required") or [])
        if "by_point" in required:
            kind, content = "case", self._case_responses.pop(0)
        elif "scores" in required:
            kind, content = "rerank", _rr()
        else:
            kind, content = "multi_query", _mq()
        self.kinds.append(kind)
        return self._result_cls(
            content=content,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            model=model or "fake-model", finish_reason="stop", retries=0,
            latency_ms=1,
        )


CASE_JSON = json.dumps(
    {"by_point": {"pt-1-1": [_case("提交订单成功-重生", C1)]}}, ensure_ascii=False
)


# ---------- 播种（独立 Database 连接，WAL 多连接安全） ----------


async def _seed_conv(db, ws=WS, conv=CONV):
    # 幂等：同库多种任务时 ws/conv 已存在则跳过（workspace DAO 无 OR IGNORE）
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


async def _seed_task(db, task_id=TASK, *, status="waiting_input",
                     stage=STAGE_INTAKE, conv=CONV, ws=WS,
                     snapshot_level="meta", thread=None):
    await _seed_conv(db, ws=ws, conv=conv)
    await TaskDAO(db).create(
        TaskRow.create(
            id=task_id, conversation_id=conv, workspace_id=ws,
            status=status, current_stage=stage,
            langgraph_thread_id=thread or f"th-{task_id}",
            graph_run_id=f"run-{task_id}", snapshot_level=snapshot_level,
        )
    )


async def _seed_artifact(db, task_id, art_id, stage, *, version=1, payload=None,
                         progress=None, status="active", origin="system"):
    await ArtifactDAO(db).put(
        ArtifactRow.create(
            id=art_id, task_id=task_id, stage=stage, graph_run_id=f"run-{task_id}",
            stage_version=version, payload=payload or {}, progress=progress,
            status=status, origin=origin,
        )
    )


async def _seed_case(db, task_id, case_id="case-1", point_id="pt-1-1",
                     status="active"):
    row = CaseRow.from_record(
        task_id,
        CaseRecord(
            case_id=case_id, point_id=point_id, stage_version=1,
            lineage=Lineage(root_case_id=case_id), status="active",
            review_status=ReviewStatus.PENDING, file_path="cases/v1/x.md",
            content_hash="sha256:x", title="提交订单成功", trace_refs=TraceRefs(),
        ),
        batch_id="b0",
    )
    row.status = status
    await TestcaseDAO(db).put_batch([row])


def _get_task(db, task_id=TASK):
    return _run(TaskDAO(db).get(task_id))


def _wait_status(db, task_id, want, *, timeout=6.0):
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = _get_task(db)
        if row.status == want:
            return row
        time.sleep(0.05)
    raise AssertionError(
        f"等待状态 {want} 超时，最后状态：{row.status if row else None}"
    )


def _wait_clarification(db, task_id, *, timeout=6.0):
    """等待澄清 interrupt 落账（须见 clarification_needed，避免命中 seed 的 waiting_input）。"""
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = _get_task(db)
        events = _events(db, task_id)
        if row.status == "waiting_input" and any(
            e.type == "clarification_needed" for e in events
        ):
            return row
        time.sleep(0.05)
    raise AssertionError(
        f"等待 clarification 超时，最后状态：{row.status if row else None}"
    )


def _wait_confirm_at(db, task_id, stage, *, timeout=6.0):
    """等待"恢复执行后停在 stage 的检查点"。

    confirm 返回句柄时后台 _run 尚未把状态切离当前 waiting_confirm，
    单纯轮询 status 会命中上一次 gate 的旧状态——须联合 current_stage 判定。
    """
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = _get_task(db)
        if row.status == "waiting_confirm" and row.current_stage == stage:
            return row
        time.sleep(0.05)
    raise AssertionError(
        f"等待 waiting_confirm@{stage} 超时，最后："
        f"{row.status if row else None}@{row.current_stage if row else None}"
    )


def _wait_rollback_done(db, task_id, *, timeout=6.0):
    """回退接管完成：新派生 thread 上重新停在 waiting_confirm。"""
    deadline = time.time() + timeout
    row = None
    while time.time() < deadline:
        row = _get_task(db)
        if row.status == "waiting_confirm" and row.langgraph_thread_id.endswith(
            "::run1"
        ):
            return row
        time.sleep(0.05)
    raise AssertionError(
        f"等待回退后 waiting_confirm 超时，最后：{row.status if row else None} "
        f"thread={row.langgraph_thread_id if row else None}"
    )


def _messages(db, task_id, kind=None):
    rows = _run(MessageDAO(db).list_by_task(task_id, kind=kind))
    return rows


def _events(db, task_id):
    return _run(EventDAO(db).list_after(task_id, 0))


# ---------- 假图节点（gated 主图拓扑，artifact 写入走真实 DAO） ----------


def _default_nodes(capture: dict | None = None):
    capture = capture if capture is not None else {}

    async def intake(ctx, state):
        return {}

    async def link(ctx, state):
        dao = ctx.daos.artifact
        v = await dao.next_version(ctx.task.id, STAGE_LINK_IDENTIFY)
        aid = "art-link" if v == 1 else f"art-link-v{v}"
        await dao.put(ArtifactRow.create(
            id=aid, task_id=ctx.task.id, stage=STAGE_LINK_IDENTIFY,
            graph_run_id=ctx.run_id, stage_version=v, payload=LINK_PLAN_DICT,
        ))
        return {"link_plan": LINK_PLAN_DICT}

    async def point(ctx, state):
        capture.setdefault("link_plans", []).append(
            dict(state.get("link_plan") or {})
        )
        dao = ctx.daos.artifact
        v = await dao.next_version(ctx.task.id, STAGE_POINT_WRITE)
        aid = "art-point" if v == 1 else f"art-point-v{v}"
        await dao.put(ArtifactRow.create(
            id=aid, task_id=ctx.task.id, stage=STAGE_POINT_WRITE,
            graph_run_id=ctx.run_id, stage_version=v, payload=POINT_PLAN_DICT,
        ))
        return {"point_plan": POINT_PLAN_DICT}

    async def case_gen(ctx, state):
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


def _intake_clarify_node(questions, captured):
    async def _node(ctx, state):
        resumed = interrupt({"node": STAGE_INTAKE, "questions": questions})
        captured.append(resumed)
        return {}

    return _node


def _wait_cancel_node():
    async def _node(ctx, state):
        while not await ctx.cancelled():
            await asyncio.sleep(0.01)
        raise TaskCancelled("cancel at batch boundary")

    return _node


def _intake_slow_node(delay=0.4):
    async def _node(ctx, state):
        await asyncio.sleep(delay)
        return {}

    return _node


# ---------- app/lifespan 组装 ----------


def _make_app(
    tmp_path: Path,
    *,
    nodes=None,
    graph=None,
    llm=None,
    entries=None,
    with_runner=True,
) -> FastAPI:
    tag = uuid.uuid4().hex[:8]
    db_path = tmp_path / f"app-{tag}.db"
    ckpt_path = tmp_path / f"ckpt-{tag}.db"
    run_migrations(db_path)
    store = FileStore(tmp_path / f"wsfiles-{tag}")
    reader = FakeReMeReader(entries if entries is not None else list(ENTRIES))
    factory = ReMeReaderFactory()

    async def _builder(_kb_config):
        return reader

    factory.register("sdk", _builder)
    app_llm = llm if llm is not None else FakeLLM([])

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        conn = await aiosqlite.connect(str(ckpt_path), check_same_thread=False)
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        g = graph if graph is not None else build_graph(
            saver, caps=caps_from_stage_nodes(nodes or _default_nodes())
        )
        db = Database(db_path)
        # API-B 场景覆盖到 case 后完成；关闭评审门禁以匹配原五阶段验收口径
        cfg = ConfigDAO(db)
        rt = (await cfg.get()).runtime_dict()
        rt["human_gate_review"] = False
        await cfg.update_runtime(rt)
        graphs = GraphRegistry.from_graph(CASE_DESIGNER, g)
        bus = EventBus(EventDAO(db))
        registry = TaskRegistry(TaskDAO(db))
        app_ctx = AppContext(
            db=db, file_store=store, llm=app_llm, reme_factory=factory,
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
        if with_runner:
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
    return app


@contextmanager
def open_env(app: FastAPI):
    """进入 TestClient lifespan（portal loop），附独立 Database 供播种/轮询。"""
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


def _create_task(client, conv=CONV, md=REQUIREMENT_MD):
    return client.post(
        "/api/v1/tasks", json={"conversation_id": conv, "requirement_md": md}
    )


# ---------- create ----------


class TestTasksCreate:
    def test_create_201_location_file_and_shape(self, env):
        client, db = env.client, env.db
        _run(_seed_conv(db))
        resp = _create_task(client)
        assert resp.status_code == 201
        task_id = resp.json()["id"]
        assert resp.headers["location"] == f"/api/v1/tasks/{task_id}"
        body = resp.json()
        assert body["status"] == "waiting_input"
        assert body["current_stage"] == STAGE_INTAKE
        assert body["active_artifacts"] == {}
        assert body["progress"] == {}
        assert body["error_info"] is None
        assert body["stale"] is False
        # dd §10.3①：先文件后 DB——requirement.md 已落盘且内容一致
        md_file = (
            env.app.state.file_store.root / "workspaces" / WS / task_id
            / "requirement.md"
        )
        assert md_file.read_text(encoding="utf-8") == REQUIREMENT_MD

    def test_create_missing_conversation_404(self, env):
        resp = _create_task(env.client, conv="no-such")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_create_empty_requirement_400(self, env):
        _run(_seed_conv(env.db))
        resp = _create_task(env.client, md="")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_BODY"


# ---------- get ----------


class TestTaskGet:
    def test_get_404(self, env):
        resp = env.client.get("/api/v1/tasks/no-such")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_stale_flag_waiting_only_after_7d(self, env):
        db = env.db
        _run(_seed_task(db, "t-fresh"))
        _run(_seed_task(db, "t-old-waiting"))
        _run(_seed_task(db, "t-old-running", status="running"))
        for tid in ("t-old-waiting", "t-old-running"):
            _run(db.aexecute(
                "UPDATE task SET updated_at = ? WHERE id = ?",
                (iso_ago(8 * 86400), tid),
            ))
        assert env.client.get("/api/v1/tasks/t-fresh").json()["stale"] is False
        old_waiting = env.client.get("/api/v1/tasks/t-old-waiting").json()
        assert old_waiting["stale"] is True
        assert old_waiting["status"] == "waiting_input"  # 原状态不变，仅标记
        # running 不 stale（仅 waiting_* 生效）
        assert env.client.get("/api/v1/tasks/t-old-running").json()["stale"] is False


# ---------- run ----------


class TestTaskRun:
    def test_full_happy_path_run_confirm_confirm_completed(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db))
        r = client.post(f"/api/v1/tasks/{TASK}/run")
        assert r.status_code == 200
        body = r.json()
        assert body["task_id"] == TASK
        assert body["graph_run_id"] == f"run-{TASK}"
        assert body["events_url"] == f"/api/v1/tasks/{TASK}/events"
        assert body["resume_from"] == {
            "node": STAGE_INTAKE, "batch_id": None, "done": 0, "total": 0,
        }

        _wait_status(db, TASK, "waiting_confirm")
        row = _get_task(db)
        assert row.current_stage == STAGE_LINK_IDENTIFY
        detail = client.get(f"/api/v1/tasks/{TASK}").json()
        art = detail["active_artifacts"][STAGE_LINK_IDENTIFY]
        assert art["id"] == "art-link" and art["stage_version"] == 1
        assert art["confirmed_by"] is None and art["origin"] == "system"

        # CP1 放行 → CP2
        r2 = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-link",
            "expected_version": 1, "action": "confirm",
        })
        assert r2.status_code == 200
        assert r2.json()["artifact_id"] == "art-link"
        assert _run(ArtifactDAO(db).get("art-link")).confirmed_by == "user"
        _wait_confirm_at(db, TASK, STAGE_POINT_WRITE)
        assert STAGE_POINT_WRITE in client.get(f"/api/v1/tasks/{TASK}").json()[
            "active_artifacts"
        ]

        # CP2 放行 → 跑到 END → completed
        r3 = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_POINT_WRITE, "artifact_id": "art-point",
            "expected_version": 1, "action": "confirm",
        })
        assert r3.status_code == 200
        _wait_status(db, TASK, "completed")
        detail = client.get(f"/api/v1/tasks/{TASK}").json()
        assert set(detail["active_artifacts"]) == {
            STAGE_LINK_IDENTIFY, STAGE_POINT_WRITE,
        }
        assert detail["error_info"] is None
        # 终态后重复 confirm → 409
        r4 = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_POINT_WRITE, "artifact_id": "art-point",
            "expected_version": 1, "action": "confirm",
        })
        assert r4.status_code == 409
        assert r4.json()["error"]["code"] == "TASK_STATE_CONFLICT"

    def test_run_resume_from_batch_cursor(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db, status="failed", stage=STAGE_POINT_WRITE))
        _run(_seed_artifact(
            db, TASK, "art-point", STAGE_POINT_WRITE, payload=POINT_PLAN_DICT,
            progress=[
                {"batch_id": "b0", "node": STAGE_POINT_WRITE,
                 "unit_ids": ["u1"], "status": "done", "idem": "i0",
                 "result_ids": ["c1"]},
                {"batch_id": "b1", "node": STAGE_POINT_WRITE,
                 "unit_ids": ["u2"], "status": "started", "idem": "i1",
                 "result_ids": []},
            ],
        ))
        r = client.post(f"/api/v1/tasks/{TASK}/run")
        assert r.status_code == 200
        assert r.json()["resume_from"] == {
            "node": STAGE_POINT_WRITE, "batch_id": "b1", "done": 1, "total": 2,
        }
        _wait_status(db, TASK, "waiting_confirm")  # 起跑正常（假图到 CP1）

    def test_duplicate_run_busy_409(self, make_app):
        app = make_app(nodes={
            **_default_nodes(), STAGE_INTAKE: _intake_slow_node(0.4),
        })
        with open_env(app) as env:
            client, db = env.client, env.db
            _run(_seed_task(db))
            first = client.post(f"/api/v1/tasks/{TASK}/run")
            assert first.status_code == 200
            second = client.post(f"/api/v1/tasks/{TASK}/run")
            assert second.status_code == 409
            assert second.json()["error"]["code"] == "TASK_BUSY"
            _wait_status(db, TASK, "waiting_confirm")

    def test_run_on_completed_409(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db, status="completed", stage=STAGE_POINT_WRITE))
        r = client.post(f"/api/v1/tasks/{TASK}/run")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "TASK_STATE_CONFLICT"

    def test_runner_not_ready_409_but_create_ok(self, make_app):
        app = make_app(with_runner=False)
        with open_env(app) as env:
            client, db = env.client, env.db
            _run(_seed_task(db))
            r = client.post(f"/api/v1/tasks/{TASK}/run")
            assert r.status_code == 409
            err = r.json()["error"]
            assert err["code"] == "TASK_STATE_CONFLICT"
            assert err["details"]["reason"] == "runner_not_ready"


# ---------- cancel ----------


class TestTaskCancel:
    def test_cancel_waiting_direct_aborted_200(self, env):
        """场景 5：waiting_input 取消即生效（200 aborted，无在飞批次）。"""
        client, db = env.client, env.db
        _run(_seed_task(db))
        r = client.post(f"/api/v1/tasks/{TASK}/cancel")
        assert r.status_code == 200
        assert r.json() == {"task_id": TASK, "status": "aborted"}
        assert _get_task(db).status == "aborted"

    def test_cancel_running_202_then_aborted(self, make_app):
        app = make_app(nodes={
            **_default_nodes(), STAGE_INTAKE: _wait_cancel_node(),
        })
        with open_env(app) as env:
            client, db = env.client, env.db
            _run(_seed_task(db))
            assert client.post(f"/api/v1/tasks/{TASK}/run").status_code == 200
            deadline = time.time() + 5
            while time.time() < deadline and _get_task(db).status != "running":
                time.sleep(0.05)
            r = client.post(f"/api/v1/tasks/{TASK}/cancel")
            assert r.status_code == 202
            assert r.json()["status"] == "cancelling"
            assert _get_task(db).cancel_requested == 1  # 标志已置（协作取消）
            _wait_status(db, TASK, "aborted")

    def test_cancel_cancelling_idempotent_202(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db, status="cancelling"))
        r = client.post(f"/api/v1/tasks/{TASK}/cancel")
        assert r.status_code == 202
        assert r.json()["status"] == "cancelling"

    def test_cancel_terminal_409(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db, status="completed", stage=STAGE_POINT_WRITE))
        r = client.post(f"/api/v1/tasks/{TASK}/cancel")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "TASK_STATE_CONFLICT"


# ---------- confirm（含 modify） ----------


class TestTaskConfirm:
    def test_stale_version_conflict_409_then_confirm_ok(self, env):
        """场景 4：迟到版本 confirm → 409 VERSION_CONFLICT；正确版本放行。"""
        client, db = env.client, env.db
        _run(_seed_task(db))
        client.post(f"/api/v1/tasks/{TASK}/run")
        _wait_status(db, TASK, "waiting_confirm")

        stale = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-link",
            "expected_version": 2, "action": "confirm",
        })
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
        assert _get_task(db).status == "waiting_confirm"  # 冲突不改状态

        ok = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-link",
            "expected_version": 1, "action": "confirm",
        })
        assert ok.status_code == 200
        _wait_confirm_at(db, TASK, STAGE_POINT_WRITE)

    def test_modify_creates_v2_revision_and_updates_state(self, env):
        client, db = env.client, env.db
        capture: dict = {}
        # 换带 capture 的图：需重建 app —— env 夹具为默认图，这里直连校验分两层：
        # 1) HTTP 契约（v2/origin/confirmed_by/消息/state 更新经断言捕获）
        _run(_seed_task(db))
        client.post(f"/api/v1/tasks/{TASK}/run")
        _wait_status(db, TASK, "waiting_confirm")

        r = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-link",
            "expected_version": 1, "action": "modify",
            "payload": REVISED_LINK_PLAN_DICT,
        })
        assert r.status_code == 200
        out = r.json()
        assert out["status"] == "running"
        new_id = out["artifact_id"]
        assert new_id != "art-link"
        assert out["stage_version"] == 2

        art_old = _run(ArtifactDAO(db).get("art-link"))
        assert art_old.status == "superseded"
        art_new = _run(ArtifactDAO(db).get(new_id))
        assert art_new.status == "active"
        assert art_new.origin == "user_revised"
        assert art_new.confirmed_by == "user"
        assert art_new.payload_dict() == REVISED_LINK_PLAN_DICT

        msgs = _messages(db, TASK, kind="checkpoint_revision")
        assert len(msgs) == 1
        assert msgs[0].payload_dict() == {
            "stage": STAGE_LINK_IDENTIFY, "old_version": 1, "new_version": 2,
        }
        # resume 后下游（point_write）读到的是修订后 plan（state 已更新）
        _wait_confirm_at(db, TASK, STAGE_POINT_WRITE)

    def test_modify_with_capture_via_custom_graph(self, make_app):
        """modify 的 aupdate_state 效果：下游节点捕获的 link_plan 为修订版。"""
        capture: dict = {}
        app = make_app(nodes=_default_nodes(capture))
        with open_env(app) as env:
            client, db = env.client, env.db
            _run(_seed_task(db))
            client.post(f"/api/v1/tasks/{TASK}/run")
            _wait_status(db, TASK, "waiting_confirm")
            r = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
                "gate_kind": "plan_confirm",
                "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-link",
                "expected_version": 1, "action": "modify",
                "payload": REVISED_LINK_PLAN_DICT,
            })
            assert r.status_code == 200
            _wait_confirm_at(db, TASK, STAGE_POINT_WRITE)
            assert capture["link_plans"], "point_write 未执行"
            got = capture["link_plans"][-1]
            assert got["stories"][0]["title"] == "下单-修订"

    def test_modify_without_payload_400(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db))
        client.post(f"/api/v1/tasks/{TASK}/run")
        _wait_status(db, TASK, "waiting_confirm")
        r = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-link",
            "expected_version": 1, "action": "modify",
        })
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VALIDATION_BODY"

    def test_modify_bad_payload_422(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db))
        client.post(f"/api/v1/tasks/{TASK}/run")
        _wait_status(db, TASK, "waiting_confirm")
        r = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-link",
            "expected_version": 1, "action": "modify",
            "payload": {"links": "not-a-list"},
        })
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ARTIFACT_REVISION"

    def test_confirm_stage_mismatch_400(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db))
        client.post(f"/api/v1/tasks/{TASK}/run")
        _wait_status(db, TASK, "waiting_confirm")
        r = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_POINT_WRITE, "artifact_id": "art-link",
            "expected_version": 1, "action": "confirm",
        })
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VALIDATION_BODY"

    def test_confirm_superseded_artifact_400(self, make_app):
        # modify 产生 v2 后，对旧 v1 confirm：版本匹配但非 active → 400
        app = make_app()
        with open_env(app) as env:
            client, db = env.client, env.db
            _run(_seed_task(db))
            client.post(f"/api/v1/tasks/{TASK}/run")
            _wait_status(db, TASK, "waiting_confirm")
            m = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
                "gate_kind": "plan_confirm",
                "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-link",
                "expected_version": 1, "action": "modify",
                "payload": REVISED_LINK_PLAN_DICT,
            })
            assert m.status_code == 200
            _wait_confirm_at(db, TASK, STAGE_POINT_WRITE)
            r = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
                "gate_kind": "plan_confirm",
                "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-link",
                "expected_version": 1, "action": "confirm",
            })
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "VALIDATION_BODY"

    def test_confirm_cross_task_artifact_404(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db, status="waiting_confirm",
                        stage=STAGE_LINK_IDENTIFY))
        _run(_seed_task(db, "task-2", status="completed",
                        stage=STAGE_LINK_IDENTIFY, conv="conv-2", ws="ws-2"))
        _run(_seed_artifact(db, "task-2", "art-other", STAGE_LINK_IDENTIFY))
        r = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-other",
            "expected_version": 1, "action": "confirm",
        })
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_confirm_on_running_409(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db, status="running"))
        _run(_seed_artifact(db, TASK, "art-link", STAGE_LINK_IDENTIFY,
                            payload=LINK_PLAN_DICT))
        r = client.post(f"/api/v1/tasks/{TASK}/confirm", json={
            "gate_kind": "plan_confirm",
            "stage": STAGE_LINK_IDENTIFY, "artifact_id": "art-link",
            "expected_version": 1, "action": "confirm",
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "TASK_STATE_CONFLICT"


# ---------- answer ----------


class TestTaskAnswer:
    def _env_with_clarify(self, make_app):
        questions = [{"id": "q1", "question": "角色是谁？", "options": ["管理员"]}]
        captured: list = []
        app = make_app(nodes={
            **_default_nodes(),
            STAGE_INTAKE: _intake_clarify_node(questions, captured),
        })
        return questions, captured, app

    def test_answer_resumes_interrupt_and_leaves_message(self, make_app):
        questions, captured, app = self._env_with_clarify(make_app)
        with open_env(app) as env:
            client, db = env.client, env.db
            _run(_seed_task(db))
            client.post(f"/api/v1/tasks/{TASK}/run")
            _wait_clarification(db, TASK)
            assert _get_task(db).current_stage == STAGE_INTAKE

            r = client.post(f"/api/v1/tasks/{TASK}/answer", json={
                "answers": [{"question_id": "q1", "answer": "普通用户"}],
            })
            assert r.status_code == 200
            assert r.json() == {"task_id": TASK, "status": "running"}

            _wait_status(db, TASK, "waiting_confirm")  # 恢复后跑到 CP1
            assert captured and captured[0] == [
                {"id": "q1", "answer": "普通用户"}
            ]
            msgs = _messages(db, TASK, kind="clarification_qa")
            assert len(msgs) == 1
            assert msgs[0].payload_dict() == {
                "answers": [{"id": "q1", "answer": "普通用户"}]
            }
            # 澄清问题载荷经 clarification_needed 事件发射
            events = _events(db, TASK)
            clar = [e for e in events if e.type == "clarification_needed"]
            assert len(clar) == 1
            assert clar[-1].payload_dict() == {"questions": questions}

    def test_answer_missing_question_id_400(self, make_app):
        _questions, _captured, app = self._env_with_clarify(make_app)
        with open_env(app) as env:
            client, db = env.client, env.db
            _run(_seed_task(db))
            client.post(f"/api/v1/tasks/{TASK}/run")
            _wait_clarification(db, TASK)
            r = client.post(f"/api/v1/tasks/{TASK}/answer", json={
                "answers": [{"answer": "x"}],
            })
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "VALIDATION_BODY"

    def test_answer_missing_answer_400(self, make_app):
        _questions, _captured, app = self._env_with_clarify(make_app)
        with open_env(app) as env:
            client, db = env.client, env.db
            _run(_seed_task(db))
            client.post(f"/api/v1/tasks/{TASK}/run")
            _wait_clarification(db, TASK)
            r = client.post(f"/api/v1/tasks/{TASK}/answer", json={
                "answers": [{"question_id": "q1"}],
            })
            assert r.status_code == 400

    def test_answer_on_waiting_confirm_409(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db, status="waiting_confirm",
                        stage=STAGE_LINK_IDENTIFY))
        r = client.post(f"/api/v1/tasks/{TASK}/answer", json={
            "answers": [{"question_id": "q1", "answer": "x"}],
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "TASK_STATE_CONFLICT"


# ---------- rollback ----------


class TestTaskRollback:
    def test_rollback_no_revision_new_thread_and_rerun(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db))
        client.post(f"/api/v1/tasks/{TASK}/run")
        _wait_status(db, TASK, "waiting_confirm")

        r = client.post(f"/api/v1/tasks/{TASK}/rollback", json={
            "target_stage": STAGE_LINK_IDENTIFY,
            "artifact_id": "art-link", "expected_version": 1,
        })
        assert r.status_code == 200
        body = r.json()
        assert body["graph_run_id"] != f"run-{TASK}"
        assert body["impact"]["target_stage"] == STAGE_LINK_IDENTIFY
        assert body["impact"]["summary"]

        row = _wait_rollback_done(db, TASK)  # 重跑到 CP1（新派生 thread ::run1）
        assert row.current_stage == STAGE_LINK_IDENTIFY
        # 节点重跑产出 v2（无修订：旧 v1 保持 active，节点按 next_version 续写）
        art = _run(ArtifactDAO(db).get_active(TASK, STAGE_LINK_IDENTIFY))
        assert art.stage_version == 2 and art.id == "art-link-v2"

    def test_rollback_wrong_version_409(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db))
        client.post(f"/api/v1/tasks/{TASK}/run")
        _wait_status(db, TASK, "waiting_confirm")
        r = client.post(f"/api/v1/tasks/{TASK}/rollback", json={
            "target_stage": STAGE_LINK_IDENTIFY,
            "artifact_id": "art-link", "expected_version": 3,
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "VERSION_CONFLICT"

    def test_rollback_on_running_409(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db, status="running"))
        _run(_seed_artifact(db, TASK, "art-link", STAGE_LINK_IDENTIFY,
                            payload=LINK_PLAN_DICT))
        r = client.post(f"/api/v1/tasks/{TASK}/rollback", json={
            "target_stage": STAGE_LINK_IDENTIFY,
            "artifact_id": "art-link", "expected_version": 1,
        })
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "TASK_STATE_CONFLICT"

    def test_rollback_cross_task_artifact_404(self, env):
        client, db = env.client, env.db
        _run(_seed_task(db, status="waiting_confirm",
                        stage=STAGE_LINK_IDENTIFY))
        _run(_seed_task(db, "task-2", status="completed",
                        stage=STAGE_LINK_IDENTIFY, conv="conv-2", ws="ws-2"))
        _run(_seed_artifact(db, "task-2", "art-other", STAGE_LINK_IDENTIFY))
        r = client.post(f"/api/v1/tasks/{TASK}/rollback", json={
            "target_stage": STAGE_LINK_IDENTIFY,
            "artifact_id": "art-other", "expected_version": 1,
        })
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"


# ---------- regenerate（runtime 入口直调；HTTP 端点归 WP-27） ----------


async def _regen_app(tmp_path, *, task_status="completed", stage=STAGE_CASE_GENERATE,
                     with_case_artifact=True, case_task="task-1"):
    """regenerate 直调环境：AppContext + SmartLLM + FakeReMeReader。"""
    db_path = tmp_path / f"regen-{uuid.uuid4().hex[:8]}.db"
    run_migrations(db_path)
    db = Database(db_path)
    await _seed_task(db, case_task, status=task_status, stage=stage,
                     snapshot_level="off")
    await _seed_artifact(db, case_task, "art-link", STAGE_LINK_IDENTIFY,
                         payload=LINK_PLAN_DICT)
    await _seed_artifact(db, case_task, "art-point", STAGE_POINT_WRITE,
                         payload=POINT_PLAN_DICT)
    if with_case_artifact:
        await _seed_artifact(db, case_task, "art-case", STAGE_CASE_GENERATE)
    if task_status == "completed" and with_case_artifact:
        await _seed_case(db, case_task)
    store = FileStore(tmp_path / f"regen-ws-{uuid.uuid4().hex[:6]}")
    reader = FakeReMeReader(list(ENTRIES))
    factory = ReMeReaderFactory()

    async def _builder(_kb_config):
        return reader

    factory.register("sdk", _builder)
    llm = SmartLLM([CASE_JSON])
    app_ctx = AppContext(
        db=db, file_store=store, llm=llm, reme_factory=factory,
        config=ConfigDAO(db), graphs=None,
        bus=EventBus(EventDAO(db)), registry=TaskRegistry(TaskDAO(db)),
    )
    return SimpleNamespace(db=db, app=app_ctx, llm=llm)


class TestRegenerate:
    @pytest.mark.asyncio
    async def test_regenerate_lineage_events_and_message(self, tmp_path):
        bundle = await _regen_app(tmp_path)
        try:
            rows = await regenerate_cases(
                bundle.app, TASK, case_ids=["case-1"],
                instruction="补充库存不足边界",
            )
            assert len(rows) == 1
            new = rows[0]
            assert new.id != "case-1"
            assert new.batch_id.startswith("regen-")
            lineage = new.lineage_obj()
            assert lineage.regenerated_from_case_id == "case-1"
            assert lineage.root_case_id == "case-1"
            # 任务回 completed 并发 task_done；指令留痕
            task = await TaskDAO(bundle.db).get(TASK)
            assert task.status == "completed"
            events = await EventDAO(bundle.db).list_after(TASK, 0)
            assert events[-1].type == "task_done"
            assert events[-1].payload_dict() == {"status": "completed"}
            msgs = await MessageDAO(bundle.db).list_by_task(
                TASK, kind="regen_instruction"
            )
            assert len(msgs) == 1
            assert msgs[0].content == "补充库存不足边界"
            assert msgs[0].payload_dict() == {
                "case_ids": ["case-1"], "keep_original": True,
            }
            # keep_original=True：旧行保留 active
            old = await TestcaseDAO(bundle.db).get("case-1")
            assert old.status == "active"
            assert bundle.llm.kinds.count("case") == 1
        finally:
            bundle.db.close()

    @pytest.mark.asyncio
    async def test_regenerate_keep_original_false_obsoletes(self, tmp_path):
        bundle = await _regen_app(tmp_path)
        try:
            rows = await regenerate_cases(
                bundle.app, TASK, case_ids=["case-1"],
                instruction="重写该用例", keep_original=False,
            )
            assert len(rows) == 1
            old = await TestcaseDAO(bundle.db).get("case-1")
            assert old.status == "obsolete"
            new = await TestcaseDAO(bundle.db).get(rows[0].id)
            assert new.status == "active"
        finally:
            bundle.db.close()

    @pytest.mark.asyncio
    async def test_regenerate_state_conflict_keeps_status(self, tmp_path):
        """非法迁移在拿锁前裁决：报 409 且任务状态不被误置 failed。"""
        bundle = await _regen_app(tmp_path, task_status="waiting_confirm",
                                  stage=STAGE_POINT_WRITE, with_case_artifact=False)
        try:
            with pytest.raises(TaskStateConflict):
                await regenerate_cases(
                    bundle.app, TASK, case_ids=["case-1"], instruction="x",
                )
            task = await TaskDAO(bundle.db).get(TASK)
            assert task.status == "waiting_confirm"
            assert task.error_info is None
        finally:
            bundle.db.close()

    @pytest.mark.asyncio
    async def test_regenerate_missing_case_artifact_keeps_status(self, tmp_path):
        """前置校验失败（缺 case artifact）保持原状态——started 标志口径。"""
        bundle = await _regen_app(tmp_path, with_case_artifact=False)
        try:
            with pytest.raises(AppError):
                await regenerate_cases(
                    bundle.app, TASK, case_ids=["case-1"], instruction="x",
                )
            task = await TaskDAO(bundle.db).get(TASK)
            assert task.status == "completed"  # 未被置 failed
            assert task.error_info is None
        finally:
            bundle.db.close()

    @pytest.mark.asyncio
    async def test_regenerate_cross_task_case_404(self, tmp_path):
        bundle = await _regen_app(tmp_path)
        try:
            await _seed_task(bundle.db, "task-2", status="completed",
                             stage=STAGE_CASE_GENERATE, snapshot_level="off",
                             conv="conv-2", ws="ws-2")
            await _seed_artifact(bundle.db, "task-2", "art-case-2",
                                 STAGE_CASE_GENERATE)
            await _seed_case(bundle.db, "task-2", case_id="case-2")
            with pytest.raises(NotFoundError):
                await regenerate_cases(
                    bundle.app, TASK, case_ids=["case-2"], instruction="x",
                )
            task = await TaskDAO(bundle.db).get(TASK)
            assert task.status == "completed"
        finally:
            bundle.db.close()
