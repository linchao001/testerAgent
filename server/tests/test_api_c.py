"""WP-27 API-C 验收测试：cases list/get/edit(If-Match)/review 批量校验/
regenerate + ExportService（tech-design §5.4 §3.2④ §5.0 / dd §10.2 §10.3④⑤
§9.4 §7.6）。

验收口径（WBS #27 场景 11 及 §10.2 §10.3④⑤ §9.4 全端点）：
- 场景 11a：编辑 If-Match 乐观锁——过期 hash → 409 VERSION_CONFLICT 且
  details.current 带盘上当前 hash；正确 hash → 200，服务端重算元数据
  （front-matter 误改以 DB 为准），review_status→edited_adopted +
  review_record(edit, diff)；
- 场景 11b：批量评审非法转换整体 422 VALIDATION_REVIEW_TRANSITION——
  任一非法（同状态重复评审/非 active 评审/转换表外动作）则全批不落账；
- 列表键集分页（created_at, id 倒序）无重复无遗漏 + status/review/version
  过滤；详情返回 MD 正文 + content_hash + trace_refs + lineage；
- 导出：默认筛选 active 且 adopted/edited_adopted；打包前 hash 对拍，
  不一致/缺文件列 skipped 不进 zip；同步返回 download_url，超阈值转异步
  job 轮询；zip 结构 v{n}/{point_id}-{slug}.md + INDEX.md；?download=1
  直接下载；
- regenerate HTTP 入口：走 runtime/regenerate.py（WP-26），返回
  new_case_ids 且 lineage 挂旧 case。

夹具：与 test_api_b 同构（portal loop TestClient + 独立 Database 播种），
app 额外挂载 cases_router。用例播种分两种：``_seed_case_row``（仅 DB 行，
regenerate 用）与 ``_seed_case_file``（经 FileStore.write_case 落真实 MD
文件，list/get/edit/export 用——hash 对拍需要盘上真文件）。
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import uuid
import zipfile
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
from tester_agent.adapters.reme import Entry, EntryType, ReMeReaderFactory
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
C1 = "h2-1"


def _run(coro):
    return asyncio.run(coro)


# ---------- SmartLLM（regenerate 走 generate_case_batch，按 json_schema 分发） ----------


def _mq():
    return json.dumps({"keyword_queries": ["订单 下单"],
                       "rewrite_queries": ["创建订单 库存"]}, ensure_ascii=False)


def _rr():
    return json.dumps({"scores": [
        {"entry_id": e.entry_id, "score": 90, "reason": "f"} for e in ENTRIES
    ]}, ensure_ascii=False)


CASE_JSON = json.dumps(
    {"by_point": {"pt-1-1": [{
        "title": "提交订单成功-重生", "priority": "P1", "preconditions": [],
        "steps": [{"seq": 1, "action": "执行-重生", "expect": "通过"}],
        "test_data": None,
        "trace_refs": {"clause_ids": [C1], "entry_ids": []},
    }]}}, ensure_ascii=False
)


class SmartLLM:
    """按 json_schema.required 即时分发应答（与 test_api_b 同口径）。"""

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

# regenerate 前置：link/point/case 三阶段 active 产物（与 test_api_b 同构）
LINK_PLAN_DICT = {
    "links": [{"link_id": "L1", "title": "订单", "summary": "下单", "hit": True,
               "entry_id": "l1", "confidence": 0.9, "story_ids": ["S1"]}],
    "stories": [{"story_id": "S1", "link_id": "L1", "title": "下单",
                 "summary": "购物车下单", "hit": True, "entry_id": "s1",
                 "confidence": 0.9, "rationale": "r",
                 "related_clause_ids": [C1]}],
    "new_suggestions": [],
}
POINT_PLAN_DICT = {"points": [{
    "point_id": "pt-1-1", "story_id": "S1", "title": "正常下单", "angle": "正常",
    "method": "场景法", "clause_ids": [C1], "source_entry_ids": ["b1"],
    "priority": "P0",
}]}


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


async def _seed_task(db, task_id=TASK, *, status="completed", stage="case_generate",
                     conv=CONV, ws=WS):
    await _seed_conv(db, ws=ws, conv=conv)
    await TaskDAO(db).create(
        TaskRow.create(
            id=task_id, conversation_id=conv, workspace_id=ws,
            status=status, current_stage=stage,
            langgraph_thread_id=f"th-{task_id}", graph_run_id=f"run-{task_id}",
            snapshot_level="off",
        )
    )


async def _seed_artifact(db, task_id, art_id, stage, *, version=1, payload=None):
    await ArtifactDAO(db).put(
        ArtifactRow.create(
            id=art_id, task_id=task_id, stage=stage,
            graph_run_id=f"run-{task_id}", stage_version=version,
            payload=payload or {},
        )
    )


def _case_content(case_id, title, *, point_id="pt-1-1", priority="P0"):
    return CaseFileContent(
        case_id=case_id, point_id=point_id, stage_version=1, title=title,
        priority=priority, preconditions=["已登录"],
        steps=[CaseStep(seq=1, action=f"执行-{title}", expect="通过")],
        test_data=None, trace_refs=TraceRefs(clause_ids=[C1]),
    )


async def _seed_case_row(db, task_id, case_id, *, point_id="pt-1-1",
                         status="active", review=ReviewStatus.PENDING,
                         title="提交订单成功", created_at=None):
    """仅 DB 行（regenerate 等不读旧文件的场景；file_path/hash 为占位）。"""
    row = CaseRow.from_record(
        task_id,
        CaseRecord(
            case_id=case_id, point_id=point_id, stage_version=1,
            lineage=Lineage(root_case_id=case_id), status="active",
            review_status=review, file_path="cases/v1/x.md",
            content_hash="sha256:x", title=title,
            trace_refs=TraceRefs(clause_ids=[C1]),
        ),
        batch_id="b0", now=created_at,
    )
    row.status = status
    await TestcaseDAO(db).put_batch([row])
    return row


async def _seed_case_file(db, store, task_id, case_id, *, point_id="pt-1-1",
                          status="active", review=ReviewStatus.PENDING,
                          title="提交订单成功", priority="P0", ws=WS,
                          created_at=None, write_file=True):
    """DB 行 + 真实 MD 文件（write_case 两遍渲染，content_hash 与盘上一致）。

    ``write_file=False`` 时只落 DB 行（file_missing 导出场景）。
    """
    if write_file:
        written = await store.write_case(
            ws, task_id, 1, _case_content(case_id, title, point_id=point_id,
                                          priority=priority)
        )
        file_path, content_hash = written.file_path, written.content_hash
    else:
        file_path, content_hash = f"cases/v1/missing-{case_id}.md", "sha256:ghost"
    row = CaseRow.from_record(
        task_id,
        CaseRecord(
            case_id=case_id, point_id=point_id, stage_version=1,
            lineage=Lineage(root_case_id=case_id), status="active",
            review_status=review, file_path=file_path,
            content_hash=content_hash, title=title,
            trace_refs=TraceRefs(clause_ids=[C1]),
        ),
        batch_id="b0", now=created_at,
    )
    row.status = status
    await TestcaseDAO(db).put_batch([row])
    return row


def _reviews(db, task_id=TASK):
    return _run(ReviewDAO(db).list_by_task(task_id, cursor=None, limit=100)).items


def _events(db, task_id=TASK):
    return _run(EventDAO(db).list_after(task_id, 0))


# ---------- app/lifespan 组装（test_api_b 同构 + cases_router） ----------


def _make_app(tmp_path: Path, *, llm=None, with_runner=True) -> FastAPI:
    tag = uuid.uuid4().hex[:8]
    db_path = tmp_path / f"appc-{tag}.db"
    ckpt_path = tmp_path / f"ckptc-{tag}.db"
    run_migrations(db_path)
    store = FileStore(tmp_path / f"wsfiles-c{tag}")
    reader = FakeReMeReader(list(ENTRIES))
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
        g = build_graph(saver)
        db = Database(db_path)
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


# ---------- list ----------


class TestCaseList:
    def test_list_empty(self, env):
        _run(_seed_task(env.db))
        resp = env.client.get(f"/api/v1/tasks/{TASK}/cases")
        assert resp.status_code == 200
        assert resp.json() == {"items": [], "next_cursor": None}

    def test_list_task_404(self, env):
        resp = env.client.get("/api/v1/tasks/no-such/cases")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_list_pagination_no_dup_no_omit(self, env):
        """键集分页（created_at, id 倒序）：翻页无重复无遗漏（dd §3.3⑦）。"""
        db, store = env.db, env.app.state.file_store
        _run(_seed_task(db))
        for i in range(3):
            _run(_seed_case_file(
                db, store, TASK, f"case-{i}", title=f"用例-{i}",
                created_at=f"2026-09-27T00:00:0{i}.000Z",
            ))
        page1 = env.client.get(f"/api/v1/tasks/{TASK}/cases?limit=2").json()
        assert [c["id"] for c in page1["items"]] == ["case-2", "case-1"]
        assert page1["next_cursor"]
        page2 = env.client.get(
            f"/api/v1/tasks/{TASK}/cases?limit=2&cursor={page1['next_cursor']}"
        ).json()
        assert [c["id"] for c in page2["items"]] == ["case-0"]
        assert page2["next_cursor"] is None
        # 汇总：无重复无遗漏
        assert {c["id"] for c in page1["items"] + page2["items"]} == {
            "case-0", "case-1", "case-2",
        }

    def test_list_filters(self, env):
        db, store = env.db, env.app.state.file_store
        _run(_seed_task(db))
        _run(_seed_case_file(db, store, TASK, "c-pend", title="待评审"))
        _run(_seed_case_file(db, store, TASK, "c-adopt", title="已采纳",
                             review=ReviewStatus.ADOPTED))
        _run(_seed_case_file(db, store, TASK, "c-obs", title="已废弃",
                             status="obsolete"))
        items = env.client.get(f"/api/v1/tasks/{TASK}/cases").json()["items"]
        assert {c["id"] for c in items} == {"c-pend", "c-adopt", "c-obs"}
        adopted = env.client.get(
            f"/api/v1/tasks/{TASK}/cases?review=adopted"
        ).json()["items"]
        assert [c["id"] for c in adopted] == ["c-adopt"]
        active = env.client.get(
            f"/api/v1/tasks/{TASK}/cases?status=active"
        ).json()["items"]
        assert {c["id"] for c in active} == {"c-pend", "c-adopt"}
        # 响应形态（dd §10.2 CaseSummaryOut）
        sample = items[0]
        assert set(sample) == {
            "id", "point_id", "stage_version", "title", "status",
            "review_status", "batch_id", "error_info", "created_at",
            "updated_at",
        }

    def test_list_bad_cursor_400(self, env):
        _run(_seed_task(env.db))
        resp = env.client.get(f"/api/v1/tasks/{TASK}/cases?cursor=@@@")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_BODY"


# ---------- get ----------


class TestCaseGet:
    def test_get_detail(self, env):
        db, store = env.db, env.app.state.file_store
        _run(_seed_task(db))
        row = _run(_seed_case_file(db, store, TASK, "case-1", title="提交订单成功"))
        resp = env.client.get("/api/v1/cases/case-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "case-1"
        assert body["point_id"] == "pt-1-1"
        assert body["content_hash"] == row.content_hash
        assert body["review_status"] == "pending"
        assert body["lineage"] == {"root_case_id": "case-1",
                                   "regenerated_from_case_id": None}
        assert body["trace_refs"]["clause_ids"] == [C1]
        # MD 正文与盘上一致
        disk = _run(store.read_case(WS, TASK, row.file_path))
        assert body["markdown"] == disk
        assert "# 提交订单成功" in body["markdown"]

    def test_get_404(self, env):
        resp = env.client.get("/api/v1/cases/no-such")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"


# ---------- edit（If-Match 乐观锁，场景 11a） ----------


def _edited_md(origin_md: str, *, title: str, step_action: str,
               fm_case_id: str | None = None) -> str:
    """基于盘上正文生成用户编辑稿：改标题/步骤；fm_case_id 模拟误改元数据。"""
    text = origin_md.replace("# 提交订单成功", f"# {title}")
    text = text.replace("执行-提交订单成功", step_action)
    if fm_case_id is not None:
        text = text.replace("case_id: case-1", f"case_id: {fm_case_id}")
    return text


class TestCaseEdit:
    def _setup(self, env):
        db, store = env.db, env.app.state.file_store
        _run(_seed_task(db))
        row = _run(_seed_case_file(db, store, TASK, "case-1", title="提交订单成功"))
        return db, store, row

    def test_edit_ok_recalculates_metadata_and_marks_edited_adopted(self, env):
        db, store, row = self._setup(env)
        origin = _run(store.read_case(WS, TASK, row.file_path))
        # 用户误改 front-matter case_id/stage_version：以服务端（DB）为准
        edited = _edited_md(origin, title="提交订单-改", step_action="提交并支付",
                            fm_case_id="hacked-id")
        edited = edited.replace("stage_version: 1", "stage_version: 99")
        resp = env.client.put(
            "/api/v1/cases/case-1", json={"markdown": edited},
            headers={"If-Match": row.content_hash},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["review_status"] == "edited_adopted"
        assert body["content_hash"] != row.content_hash  # 内容已变
        assert "case_id: case-1" in body["markdown"]  # 服务端重算，非 hacked-id
        assert "stage_version: 1" in body["markdown"]
        assert "# 提交订单-改" in body["markdown"]
        # DB 落账：hash/title/review 更新 + review_record(edit, diff)
        fresh = _run(TestcaseDAO(db).get("case-1"))
        assert fresh.content_hash == body["content_hash"]
        assert fresh.title == "提交订单-改"
        reviews = _reviews(db)
        assert len(reviews) == 1
        assert reviews[0].action == "edit"
        detail = reviews[0].detail_dict()
        assert detail["new_hash"] == body["content_hash"]
        assert detail["diff"]["lines_changed"] >= 1

    def test_edit_stale_if_match_409_with_current_hash(self, env):
        """场景 11a：If-Match 过期 → 409 VERSION_CONFLICT，details 带当前 hash。"""
        db, store, row = self._setup(env)
        origin = _run(store.read_case(WS, TASK, row.file_path))
        edited = _edited_md(origin, title="提交订单-改", step_action="x")
        resp = env.client.put(
            "/api/v1/cases/case-1", json={"markdown": edited},
            headers={"If-Match": "sha256:stale"},
        )
        assert resp.status_code == 409
        err = resp.json()["error"]
        assert err["code"] == "VERSION_CONFLICT"
        assert err["details"]["expected"] == "sha256:stale"
        assert err["details"]["current"] == row.content_hash  # 盘上当前 hash
        # 未落账：review_status 保持 pending，无 review_record
        assert _run(TestcaseDAO(db).get("case-1")).review_status == "pending"
        assert _reviews(db) == []

    def test_edit_missing_if_match_400(self, env):
        db, store, row = self._setup(env)
        origin = _run(store.read_case(WS, TASK, row.file_path))
        resp = env.client.put(
            "/api/v1/cases/case-1",
            json={"markdown": _edited_md(origin, title="t", step_action="x")},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_BODY"

    def test_edit_obsolete_400(self, env):
        db, store, row = self._setup(env)
        _run(TestcaseDAO(db).sweep_stale_idem(TASK, 1, "b0", keep=[]))
        origin = _run(store.read_case(WS, TASK, row.file_path))
        resp = env.client.put(
            "/api/v1/cases/case-1",
            json={"markdown": _edited_md(origin, title="t", step_action="x")},
            headers={"If-Match": row.content_hash},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_BODY"

    def test_edit_unparseable_markdown_400(self, env):
        _db, _store, row = self._setup(env)
        resp = env.client.put(
            "/api/v1/cases/case-1", json={"markdown": "# 无 front-matter"},
            headers={"If-Match": row.content_hash},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_BODY"

    def test_edit_404(self, env):
        resp = env.client.put(
            "/api/v1/cases/no-such", json={"markdown": "x"},
            headers={"If-Match": "sha256:y"},
        )
        assert resp.status_code == 404


# ---------- review（批量校验，场景 11b） ----------


class TestCaseReview:
    def _seed3(self, env):
        db, store = env.db, env.app.state.file_store
        _run(_seed_task(db))
        _run(_seed_case_file(db, store, TASK, "case-1", title="用例一"))
        _run(_seed_case_file(db, store, TASK, "case-2", title="用例二"))
        _run(_seed_case_file(db, store, TASK, "case-3", title="用例三"))
        return db

    def test_review_batch_ok_records_and_transition(self, env):
        db = self._seed3(env)
        resp = env.client.post("/api/v1/cases/review", json={"items": [
            {"case_id": "case-1", "action": "adopt"},
            {"case_id": "case-2", "action": "reject"},
        ]})
        assert resp.status_code == 200
        results = {r["case_id"]: r for r in resp.json()["results"]}
        assert results["case-1"]["review_status"] == "adopted"
        assert results["case-2"]["review_status"] == "rejected"
        dao = TestcaseDAO(db)
        assert _run(dao.get("case-1")).review_status == "adopted"
        assert _run(dao.get("case-2")).review_status == "rejected"
        assert _run(dao.get("case-3")).review_status == "pending"
        reviews = _reviews(db)
        assert len(reviews) == 2
        assert {r.action for r in reviews} == {"adopt", "reject"}
        # 三者互转允许（tech-design §3.2④）：adopted → rejected 改判
        resp2 = env.client.post("/api/v1/cases/review", json={"items": [
            {"case_id": "case-1", "action": "reject"},
        ]})
        assert resp2.status_code == 200
        assert _run(dao.get("case-1")).review_status == "rejected"

    def test_review_same_status_transition_422_all_or_nothing(self, env):
        """场景 11b：批内任一非法（同状态重复评审）→ 整体 422，合法项也不落账。"""
        db = self._seed3(env)
        dao = TestcaseDAO(db)
        _run(dao.update_review("case-2", ReviewStatus.ADOPTED))
        resp = env.client.post("/api/v1/cases/review", json={"items": [
            {"case_id": "case-1", "action": "adopt"},    # 合法
            {"case_id": "case-2", "action": "adopt"},    # 非法：已是 adopted
        ]})
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "VALIDATION_REVIEW_TRANSITION"
        assert err["details"]["case_id"] == "case-2"
        assert "adopted" in err["details"]["allowed"] or err["details"]["allowed"]
        # 整体不落账：case-1 仍 pending，无 review_record
        assert _run(dao.get("case-1")).review_status == "pending"
        assert _run(dao.get("case-2")).review_status == "adopted"
        assert _reviews(db) == []

    def test_review_non_active_422(self, env):
        """非 active 用例评审 → 422 reason=case_not_active（交接登记的口径）。"""
        db = self._seed3(env)
        _run(TestcaseDAO(db).sweep_stale_idem(TASK, 1, "b0", keep=["case-1"]))
        resp = env.client.post("/api/v1/cases/review", json={"items": [
            {"case_id": "case-2", "action": "adopt"},
        ]})
        assert resp.status_code == 422
        err = resp.json()["error"]
        assert err["code"] == "VALIDATION_REVIEW_TRANSITION"
        assert err["details"]["reason"] == "case_not_active"

    def test_review_unknown_case_404(self, env):
        self._seed3(env)
        resp = env.client.post("/api/v1/cases/review", json={"items": [
            {"case_id": "no-such", "action": "adopt"},
        ]})
        assert resp.status_code == 404

    def test_review_bad_action_400(self, env):
        self._seed3(env)
        resp = env.client.post("/api/v1/cases/review", json={"items": [
            {"case_id": "case-1", "action": "approve"},
        ]})
        assert resp.status_code == 400  # pydantic Literal 校验

    def test_review_empty_items_400(self, env):
        self._seed3(env)
        resp = env.client.post("/api/v1/cases/review", json={"items": []})
        assert resp.status_code == 400


# ---------- regenerate（HTTP 入口 → runtime/regenerate.py） ----------


class TestCaseRegenerate:
    def _seed_regen(self, db):
        _run(_seed_task(db, status="completed", stage="case_generate"))
        _run(_seed_artifact(db, TASK, "art-link", "link_identify",
                            payload=LINK_PLAN_DICT))
        _run(_seed_artifact(db, TASK, "art-point", "point_write",
                            payload=POINT_PLAN_DICT))
        _run(_seed_artifact(db, TASK, "art-case", "case_generate"))
        _run(_seed_case_row(db, TASK, "case-1"))

    def test_regenerate_http_entry(self, make_app):
        app = make_app(llm=SmartLLM([CASE_JSON]))
        with open_env(app) as env:
            self._seed_regen(env.db)
            resp = env.client.post("/api/v1/cases/regenerate", json={
                "case_ids": ["case-1"], "instruction": "补充库存不足边界",
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["task_id"] == TASK
            assert body["status"] == "completed"
            assert len(body["new_case_ids"]) == 1
            new_id = body["new_case_ids"][0]
            assert new_id != "case-1"
            new_row = _run(TestcaseDAO(env.db).get(new_id))
            lineage = new_row.lineage_obj()
            assert lineage.regenerated_from_case_id == "case-1"
            assert lineage.root_case_id == "case-1"
            # 任务回 completed；新用例文件已落盘
            assert _run(TaskDAO(env.db).get(TASK)).status == "completed"
            disk = _run(env.app.state.file_store.read_case(
                WS, TASK, new_row.file_path
            ))
            assert "提交订单成功-重生" in disk

    def test_regenerate_cross_task_case_404(self, make_app):
        app = make_app(llm=SmartLLM([CASE_JSON]))
        with open_env(app) as env:
            db = env.db
            self._seed_regen(db)
            _run(_seed_task(db, "task-2", status="completed",
                            stage="case_generate", conv="conv-2", ws="ws-2"))
            _run(_seed_case_row(db, "task-2", "case-2"))
            resp = env.client.post("/api/v1/cases/regenerate", json={
                "case_ids": ["case-1", "case-2"], "instruction": "x",
            })
            assert resp.status_code == 404
            # 状态冲突不误置 failed（WP-26 口径）
            assert _run(TaskDAO(db).get(TASK)).status == "completed"

    def test_regenerate_unknown_first_case_404(self, env):
        resp = env.client.post("/api/v1/cases/regenerate", json={
            "case_ids": ["no-such"], "instruction": "x",
        })
        assert resp.status_code == 404

    def test_regenerate_empty_instruction_400(self, env):
        resp = env.client.post("/api/v1/cases/regenerate", json={
            "case_ids": ["case-1"], "instruction": "",
        })
        assert resp.status_code == 400


# ---------- export ----------


def _zip_names(content: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return sorted(zf.namelist())


def _zip_read(content: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        return zf.read(name).decode("utf-8")


class TestCaseExport:
    def test_export_sync_filters_and_zip_structure(self, env):
        """dd §9.4：默认仅 active 且 adopted/edited_adopted 进 zip；
        zip 结构 v{n}/{point_id}-{slug}.md + INDEX.md。"""
        db, store = env.db, env.app.state.file_store
        _run(_seed_task(db))
        _run(_seed_case_file(db, store, TASK, "c-adopt", title="采纳用例",
                             review=ReviewStatus.ADOPTED, priority="P0"))
        _run(_seed_case_file(db, store, TASK, "c-edit", title="编辑采纳",
                             review=ReviewStatus.EDITED_ADOPTED, priority="P1"))
        _run(_seed_case_file(db, store, TASK, "c-pend", title="待评审"))
        _run(_seed_case_file(db, store, TASK, "c-rej", title="已拒绝",
                             review=ReviewStatus.REJECTED))
        _run(_seed_case_file(db, store, TASK, "c-obs", title="废弃采纳",
                             status="obsolete", review=ReviewStatus.ADOPTED))

        resp = env.client.post(f"/api/v1/tasks/{TASK}/export", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["skipped"] == []
        assert body["job_id"]
        assert body["download_url"] == (
            f"/api/v1/tasks/{TASK}/export/{body['job_id']}"
        )

        dl = env.client.get(body["download_url"] + "?download=1")
        assert dl.status_code == 200
        names = _zip_names(dl.content)
        assert names == [
            "INDEX.md",
            "v1/pt-1-1-编辑采纳.md",  # sorted 按 UTF-8 字节序
            "v1/pt-1-1-采纳用例.md",
        ]
        index = _zip_read(dl.content, "INDEX.md")
        # INDEX 序号按 (created_at, id) 升序生成序：c-adopt 先于 c-edit
        assert "| 1 | 采纳用例 | P0 | adopted |" in index
        assert "| 2 | 编辑采纳 | P1 | edited_adopted |" in index
        assert "待评审" not in index and "已拒绝" not in index
        # export_ready 事件落 task_event（SSE 可见）
        ready = [e for e in _events(db) if e.type == "export_ready"]
        assert len(ready) == 1
        assert ready[0].payload_dict()["case_count"] == 2
        # 轮询句柄对同步导出同样可用
        poll = env.client.get(body["download_url"])
        assert poll.status_code == 200
        assert poll.json()["status"] == "ready"

    def test_export_hash_mismatch_and_missing_file_skipped(self, env):
        """R34：打包前逐文件 hash 对拍，不一致/缺文件列 skipped 不进 zip。"""
        db, store = env.db, env.app.state.file_store
        _run(_seed_task(db))
        ok = _run(_seed_case_file(db, store, TASK, "c-ok", title="正常",
                                  review=ReviewStatus.ADOPTED))
        drift = _run(_seed_case_file(db, store, TASK, "c-drift", title="漂移",
                                     review=ReviewStatus.ADOPTED))
        _run(_seed_case_file(db, store, TASK, "c-gone", title="缺文件",
                             review=ReviewStatus.ADOPTED, write_file=False))
        # 盘上改文件（不经 API）→ hash 漂移
        path = store.root / "workspaces" / WS / TASK / drift.file_path
        path.write_text(path.read_text(encoding="utf-8") + "\n手工改动\n",
                        encoding="utf-8")

        resp = env.client.post(f"/api/v1/tasks/{TASK}/export", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        skipped = {s["case_id"]: s["reason"] for s in body["skipped"]}
        assert skipped == {"c-drift": "hash_mismatch", "c-gone": "file_missing"}
        dl = env.client.get(body["download_url"] + "?download=1")
        names = _zip_names(dl.content)
        assert names == ["INDEX.md", "v1/pt-1-1-正常.md"]
        index = _zip_read(dl.content, "INDEX.md")
        assert "| 1 | 正常 |" in index and "漂移" not in index

    def test_export_explicit_case_ids_still_filtered(self, env):
        """显式 case_ids 仍过同一筛选口径（pending 不进 zip）；跨任务 404。"""
        db, store = env.db, env.app.state.file_store
        _run(_seed_task(db))
        _run(_seed_case_file(db, store, TASK, "c-adopt", title="采纳",
                             review=ReviewStatus.ADOPTED))
        _run(_seed_case_file(db, store, TASK, "c-pend", title="待评审"))
        resp = env.client.post(f"/api/v1/tasks/{TASK}/export", json={
            "case_ids": ["c-adopt", "c-pend"],
        })
        assert resp.status_code == 200
        dl = env.client.get(resp.json()["download_url"] + "?download=1")
        assert _zip_names(dl.content) == ["INDEX.md", "v1/pt-1-1-采纳.md"]

        _run(_seed_task(db, "task-2", conv="conv-2", ws="ws-2"))
        _run(_seed_case_file(db, store, "task-2", "c-other", title="他任务",
                             review=ReviewStatus.ADOPTED, ws="ws-2"))
        cross = env.client.post(f"/api/v1/tasks/{TASK}/export", json={
            "case_ids": ["c-other"],
        })
        assert cross.status_code == 404

    def test_export_async_over_threshold(self, make_app, monkeypatch):
        """超阈值转异步：POST 返 running + job_id；轮询转 ready 后可下载。"""
        from tester_agent.runtime import export as export_mod

        monkeypatch.setattr(export_mod, "SYNC_MAX_CASES", 0)
        app = make_app()
        with open_env(app) as env:
            db, store = env.db, env.app.state.file_store
            _run(_seed_task(db))
            _run(_seed_case_file(db, store, TASK, "c-adopt", title="采纳用例",
                                 review=ReviewStatus.ADOPTED))
            resp = env.client.post(f"/api/v1/tasks/{TASK}/export", json={})
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "running"
            assert body["download_url"] is None
            job_id = body["job_id"]

            # 轮询到 ready（后台 task 在 portal loop 上推进）
            import time
            deadline = time.time() + 5
            poll_body = {}
            while time.time() < deadline:
                poll = env.client.get(f"/api/v1/tasks/{TASK}/export/{job_id}")
                assert poll.status_code == 200
                poll_body = poll.json()
                if poll_body["status"] == "ready":
                    break
                time.sleep(0.05)
            assert poll_body["status"] == "ready"
            assert poll_body["download_url"] == (
                f"/api/v1/tasks/{TASK}/export/{job_id}"
            )
            dl = env.client.get(poll_body["download_url"] + "?download=1")
            assert dl.status_code == 200
            assert _zip_names(dl.content) == [
                "INDEX.md", "v1/pt-1-1-采纳用例.md",
            ]

    def test_export_job_404_and_cross_task(self, env):
        _run(_seed_task(env.db))
        resp = env.client.get(f"/api/v1/tasks/{TASK}/export/no-such-job")
        assert resp.status_code == 404
        resp2 = env.client.post("/api/v1/tasks/no-such/export", json={})
        assert resp2.status_code == 404

    def test_export_empty_selection_ready_empty_zip(self, env):
        """无可导出用例：0 条 ≤ 阈值 → 同步 ready，zip 仅 INDEX.md。"""
        db, store = env.db, env.app.state.file_store
        _run(_seed_task(db))
        _run(_seed_case_file(db, store, TASK, "c-pend", title="待评审"))
        resp = env.client.post(f"/api/v1/tasks/{TASK}/export", json={})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"
        dl = env.client.get(resp.json()["download_url"] + "?download=1")
        assert _zip_names(dl.content) == ["INDEX.md"]
