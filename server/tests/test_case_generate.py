"""WP-19 case_generate + 用例批次提交协议（dd §11.1 §7.4③ §7.5③）。

验收口径（WBS #19）：
- "四崩溃点位重放产物等价、无重复行/文件"：commit_case_batch 在 tmp 写盘/
  rename 后/DB 事务中/DB 提交后四种崩溃点重放，产物等价、不产生重复行或
  重复文件；sweep_stale_idem 清理上一轮孤儿行。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.adapters.reme import Entry, EntryType, IndexMirror
from tester_agent.domain import (
    CaseFileContent,
    CaseRecord,
    CaseStep,
    Lineage,
    LinkPlan,
    LinkRef,
    PointPlan,
    ReviewStatus,
    StoryRef,
    TestPoint as PointModel,
    TraceRefs,
)
from tester_agent.context.journal import JournalAction, JournalRecord
from tester_agent.context.models import EntryKind, EntryStatus
from tester_agent.context.store import ContextStore
from tester_agent.errors import LLMBadOutput
from tester_agent.graph.batch import run_in_batches
from tester_agent.graph.constants import STAGE_CASE_GENERATE
from tester_agent.graph.nodes import (
    build_case_intent,
    case_generate_node,
    commit_case_batch,
    finalize_cases,
)
from tester_agent.graph.nodes.case_generate import CASE_ID_NS
from tester_agent.runtime.context import AppContext, DAOs, TaskContext
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ArtifactDAO,
    ArtifactRow,
    CaseRow,
    ConfigDAO,
    MessageDAO,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
    TraceDAO,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

from fakes import FakeLLM, FakeReMeReader

WS = "ws1"
TASK = "task1"
CONV = "conv1"
RUN = "run-1"


def _entry(eid, *, title, content, etype, link_id=None, story_id=None):
    return Entry(entry_id=eid, entry_version=f"v-{eid}", title=title, content=content,
                 entry_type=etype, link_id=link_id, story_id=story_id,
                 updated_at="2026-09-20T00:00:00Z", raw={"summary": title})


ENTRIES = [
    _entry("a1", title="下单接口", content="POST /order 创建订单 参数",
           etype=EntryType.API, link_id="L1", story_id="S1"),
    _entry("d1", title="订单表", content="order 表 字段 状态",
           etype=EntryType.DB, link_id="L1"),
    _entry("b1", title="订单业务规则", content="金额 校验 库存",
           etype=EntryType.BUSINESS, link_id="L1"),
    _entry("df1", title="超卖缺陷", content="库存 并发 超卖",
           etype=EntryType.DEFECT, link_id="L1"),
]
RERANK_SCORES = {"a1": 100, "d1": 90, "b1": 80, "df1": 70}

LINK_PLAN = LinkPlan(
    links=[LinkRef(link_id="L1", title="订单", summary="下单", hit=True,
                   entry_id="l1", confidence=0.9, story_ids=["S1", "S2"])],
    stories=[
        StoryRef(story_id="S1", link_id="L1", title="下单", summary="购物车下单",
                 hit=True, entry_id="s1", confidence=0.8, rationale="r",
                 related_clause_ids=["h2-1"]),
        StoryRef(story_id="S2", link_id="L1", title="支付", summary="支付回调",
                 hit=True, entry_id="s2", confidence=0.88, rationale="r",
                 related_clause_ids=["h2-1-h3-1"]),
    ],
    new_suggestions=[],
)

POINT_PLAN = PointPlan(points=[
    PointModel(point_id="pt-1-1", story_id="S1", title="正常下单", angle="正常",
               method="场景法", clause_ids=["h2-1"], source_entry_ids=["b1"], priority="P0"),
    PointModel(point_id="pt-1-2", story_id="S2", title="支付回调", angle="正常",
               method="状态迁移", clause_ids=["h2-1-h3-1"], source_entry_ids=["a1"], priority="P1"),
])


def _mq():
    return json.dumps({"keyword_queries": ["下单 接口"], "rewrite_queries": ["支付 回调"]},
                      ensure_ascii=False)


def _rr():
    return json.dumps({"scores": [{"entry_id": k, "score": v, "reason": "f"}
                                   for k, v in RERANK_SCORES.items()]}, ensure_ascii=False)


# 本批输出：pt-1-1 两条用例，pt-1-2 一条
CASE_JSON = json.dumps({
    "by_point": {
        "pt-1-1": [
            {"title": "提交订单成功", "priority": "P0",
             "preconditions": ["已登录", "购物车有商品"],
             "steps": [{"seq": 1, "action": "点击提交", "expect": "订单创建成功"},
                       {"seq": 2, "action": "查看订单", "expect": "状态待支付"}],
             "test_data": "订单金额 0.01",
             "trace_refs": {"clause_ids": ["h2-1"], "entry_ids": ["a1", "b1", "ghost"]}},
            {"title": "购物车为空提交", "priority": "P1",
             "preconditions": ["已登录"],
             "steps": [{"seq": 1, "action": "点击提交", "expect": "提示购物车为空"}],
             "test_data": None,
             "trace_refs": {"clause_ids": ["h2-1"], "entry_ids": ["b1"]}},
        ],
        "pt-1-2": [
            {"title": "支付回调更新状态", "priority": "P1",
             "preconditions": ["订单已创建"],
             "steps": [{"seq": 1, "action": "模拟回调", "expect": "订单状态已支付"}],
             "test_data": None,
             "trace_refs": {"clause_ids": ["h2-1-h3-1"], "entry_ids": ["a1", "ghost2"]}},
        ],
    }
}, ensure_ascii=False)


# ---------- 纯函数 ----------


def test_build_case_intent_from_points():
    intent = build_case_intent(POINT_PLAN.points)
    assert "正常下单" in intent and "场景法" in intent
    assert "支付回调" in intent and "状态迁移" in intent


def test_finalize_deterministic_case_id_and_whitelist():
    cases, degraded, asserted = finalize_cases(
        json.loads(CASE_JSON), points=POINT_PLAN.points,
        whitelist={"a1", "b1", "d1", "df1"},
        task_id=TASK, version=1, batch_id="b0",
    )
    assert len(cases) == 3
    # case_id 确定性：同输入同 seq 恒等
    cid_first = cases[0][1].case_id
    cases2, _, _ = finalize_cases(
        json.loads(CASE_JSON), points=POINT_PLAN.points,
        whitelist={"a1", "b1", "d1", "df1"},
        task_id=TASK, version=1, batch_id="b0",
    )
    assert cases2[0][1].case_id == cid_first
    # 不同 batch_id → 不同 case_id
    cases3, _, _ = finalize_cases(
        json.loads(CASE_JSON), points=POINT_PLAN.points,
        whitelist={"a1", "b1", "d1", "df1"},
        task_id=TASK, version=1, batch_id="b1",
    )
    assert cases3[0][1].case_id != cid_first
    # ghost/ghost2 不在白名单 → 剔除 + degraded
    pt11 = [c for _, c in cases if c.point_id == "pt-1-1"][0]
    assert pt11.trace_refs.entry_ids == ["a1", "b1"]  # ghost 剔除
    assert any("ghost" in d["reason"] for d in degraded)
    assert "ghost" in asserted
    # point_ids 自动回填
    assert pt11.trace_refs.point_ids == ["pt-1-1"]
    assert pt11.priority == "P0"


def test_finalize_missing_point_allowed_empty():
    # 模型未对某点产出 → 跳过（不报错，覆盖率兜底）
    obj = {"by_point": {"pt-1-1": [{"title": "t", "steps": [{"seq": 1, "action": "a"}]}]}}
    cases, _, _ = finalize_cases(
        obj, points=POINT_PLAN.points, whitelist=set(),
        task_id=TASK, version=1, batch_id="b0",
    )
    assert len(cases) == 1
    assert cases[0][1].point_id == "pt-1-1"


def test_finalize_bad_outputs():
    # 空 title
    with pytest.raises(LLMBadOutput):
        finalize_cases(
            {"by_point": {"pt-1-1": [{"title": "", "steps": [{"seq": 1, "action": "a"}]}]}},
            points=POINT_PLAN.points, whitelist=set(), task_id=TASK, version=1, batch_id="b0",
        )
    # 空 steps
    with pytest.raises(LLMBadOutput):
        finalize_cases(
            {"by_point": {"pt-1-1": [{"title": "t", "steps": []}]}},
            points=POINT_PLAN.points, whitelist=set(), task_id=TASK, version=1, batch_id="b0",
        )
    # 非法 priority → P1（不报错）
    cases, _, _ = finalize_cases(
        {"by_point": {"pt-1-1": [{"title": "t", "priority": "P9",
                                   "steps": [{"seq": 1, "action": "a"}]}]}},
        points=POINT_PLAN.points, whitelist=set(), task_id=TASK, version=1, batch_id="b0",
    )
    assert cases[0][1].priority == "P1"


# ---------- commit_case_batch：先文件后 DB + 幂等 + sweep ----------


@dataclass
class Stack:
    ctx: TaskContext
    db: Database
    store: FileStore
    llm: FakeLLM
    reader: FakeReMeReader


@pytest.fixture()
async def make_stack(tmp_path):
    handles: list[Database] = []

    async def build(*, script=None, entries=None, agent_config=None, snapshot_level="off"
                    ) -> Stack:
        db_path = tmp_path / f"app-{len(handles)}.db"
        run_migrations(db_path)
        db = Database(db_path)
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=WS, name="w",
                                kb_config={"kb_id": "KB1", "options": {}})
        )
        await db.aexecute(
            "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (CONV, WS, "t", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
        )
        await TaskDAO(db).create(
            TaskRow.create(id=TASK, conversation_id=CONV, workspace_id=WS,
                           status="running", current_stage=STAGE_CASE_GENERATE,
                           langgraph_thread_id="t1", graph_run_id=RUN)
        )
        store = FileStore(tmp_path / "data")
        # clauses cache 供 read_clause
        md = "# 需求\n\n## 订单\n\n下单 支付。\n\n### 支付\n\n回调。\n"
        await store.save_requirement(WS, TASK, md)
        await store.save_clauses_cache(WS, TASK, _clauses_cache(md))
        llm = FakeLLM(script or [])
        reader = FakeReMeReader(entries or ENTRIES)
        mirror = IndexMirror(reader)
        app = AppContext(db=db, file_store=store, llm=llm, reme_factory=None,
                         config=ConfigDAO(db))
        ctx = TaskContext(
            app=app, task=await TaskDAO(db).get(TASK), run_id=RUN,
            files=store, reader=reader, snapshot_level=snapshot_level,
            mirror=mirror, agent_config=agent_config or {},
            daos=DAOs(task=TaskDAO(db), message=MessageDAO(db),
                      artifact=ArtifactDAO(db), testcase=TestcaseDAO(db),
                      trace=TraceDAO(db)),
        )
        handles.append(db)
        return Stack(ctx=ctx, db=db, store=store, llm=llm, reader=reader)

    yield build
    for db in handles:
        db.close()


def _clauses_cache(md):
    lines = md.split("\n")
    out = []
    cur = None
    pos = 0
    for ln in lines:
        b = (ln + "\n").encode("utf-8")
        start = pos
        pos += len(b)
        if ln.startswith("##") and not ln.startswith("###"):
            if cur:
                cur["end_offset"] = start
                out.append(cur)
            cur = {"clause_id": f"h2-{len(out)+1}", "level": 2,
                   "title_path": [ln.lstrip("#").strip()], "anchor": "x",
                   "text_hash": "x", "status": "active",
                   "start_offset": start, "end_offset": start}
        elif ln.startswith("###"):
            if cur:
                cur["end_offset"] = start
                out.append(cur)
            cur = {"clause_id": "h2-1-h3-1", "level": 3,
                   "title_path": ["订单", ln.lstrip("#").strip()], "anchor": "x",
                   "text_hash": "x", "status": "active",
                   "start_offset": start, "end_offset": start}
    if cur:
        cur["end_offset"] = len(md.encode("utf-8"))
        out.append(cur)
    return out


async def _make_artifact(ctx, version=1, progress=None):
    await ArtifactDAO(ctx.app.db).put(
        ArtifactRow.create(
            id=uuid.uuid4().hex, task_id=TASK, stage=STAGE_CASE_GENERATE,
            graph_run_id=RUN, stage_version=version, payload={},
            origin="system", status="active", confirmed_by=None,
            progress=progress or [],
        )
    )


def _build_cases():
    return finalize_cases(
        json.loads(CASE_JSON), points=POINT_PLAN.points,
        whitelist={"a1", "b1", "d1", "df1"},
        task_id=TASK, version=1, batch_id="b0",
    )[0]


async def test_commit_writes_files_and_rows(make_stack):
    stack = await make_stack()
    await _make_artifact(stack.ctx)
    cases = _build_cases()
    rows = await commit_case_batch(stack.ctx, "b0", cases, version=1)

    assert len(rows) == 3
    # 行存在
    for r in rows:
        got = await TestcaseDAO(stack.db).get(r.id)
        assert got.status == "active" and got.batch_id == "b0"
        assert got.file_path
    # 文件存在
    for r in rows:
        content = await stack.store.read_case(WS, TASK, r.file_path)
        assert r.title in content
    # artifact.progress 回填
    art = await ArtifactDAO(stack.db).get_active(TASK, STAGE_CASE_GENERATE)
    prog = art.progress_list()
    assert prog[0]["batch_id"] == "b0" and prog[0]["status"] == "done"
    assert set(prog[0]["result_ids"]) == {r.id for r in rows}


async def test_commit_replay_idempotent_no_duplicate_rows_or_files(make_stack):
    """重放同一批：INSERT OR IGNORE 不重复行；同 hash 文件不重复写。"""
    stack = await make_stack()
    await _make_artifact(stack.ctx)
    cases = _build_cases()
    rows1 = await commit_case_batch(stack.ctx, "b0", cases, version=1)
    rows2 = await commit_case_batch(stack.ctx, "b0", cases, version=1)  # 重放

    assert [r.id for r in rows1] == [r.id for r in rows2]
    # 行数不变（无重复）
    all_rows = await TestcaseDAO(stack.db).list_by_batch(TASK, 1, "b0")
    active = [r for r in all_rows if r.status == "active"]
    assert len(active) == 3
    # 文件数不变
    case_dir = stack.store._root / "workspaces" / WS / TASK / "cases" / "v1"
    md_files = list(case_dir.glob("*.md")) if case_dir.exists() else []
    assert len(md_files) == 3


async def test_commit_sweep_stale_orphans(make_stack):
    """上一轮失败重做后产出条数变化：孤儿行被置 obsolete。"""
    stack = await make_stack()
    await _make_artifact(stack.ctx)
    cases = _build_cases()  # 3 cases
    await commit_case_batch(stack.ctx, "b0", cases, version=1)

    # 模拟上一轮还产出了第 4 条孤儿（同 batch，不同 seq → 不同 case_id）
    orphan_id = uuid.uuid5(
        CASE_ID_NS, f"{TASK}|1|b0|pt-1-1|99",
    ).hex
    from tester_agent.domain import Lineage, ReviewStatus, CaseRecord, TraceRefs as TR
    rec = CaseRecord(case_id=orphan_id, point_id="pt-1-1", stage_version=1,
                     lineage=Lineage(root_case_id=orphan_id), status="active",
                     review_status=ReviewStatus.PENDING, file_path="x",
                     content_hash="h", title="orphan", trace_refs=TR())
    await TestcaseDAO(stack.db).put_batch([CaseRow.from_record(TASK, rec, batch_id="b0")])

    # 重放：sweep 应把 orphan 置 obsolete
    await commit_case_batch(stack.ctx, "b0", cases, version=1)
    orphan = await TestcaseDAO(stack.db).get(orphan_id)
    assert orphan.status == "obsolete"
    # 正常行仍 active
    for _, c in cases:
        r = await TestcaseDAO(stack.db).get(c.case_id)
        assert r.status == "active"


# ---------- case_generate_node 端到端 ----------


def _state():
    return {"point_plan": POINT_PLAN.model_dump(), "link_plan": LINK_PLAN.model_dump()}


async def test_case_generate_happy_path(make_stack):
    stack = await make_stack(script=[_mq(), _rr(), CASE_JSON])
    inc = await case_generate_node(stack.ctx, _state())

    assert inc["case_count"] == 3
    assert len(inc["case_ids"]) == 3
    assert inc["current_stage_version"] == {STAGE_CASE_GENERATE: 1}

    # artifact
    art = await ArtifactDAO(stack.db).get_active(TASK, STAGE_CASE_GENERATE)
    assert art.stage_version == 1 and art.status == "active"
    assert art.payload_dict()["case_count"] == 3
    # testcase 行
    all_rows = await TestcaseDAO(stack.db).list_by_batch(TASK, 1, "b0")
    assert len([r for r in all_rows if r.status == "active"]) == 3
    # trace 闭环
    traces = await stack.db.aquery(
        "SELECT stage, batch_id FROM retrieval_trace WHERE task_id = ?", (TASK,)
    )
    assert len(traces) == 1 and traces[0]["stage"] == STAGE_CASE_GENERATE
    # LLM 调用：mq + rr + case_generate = 3
    assert stack.llm.chat_calls == 3


async def test_case_generate_crash_replay_done_batches_skip(make_stack):
    """同 run 已有全部 done 批 + payload → 零 LLM 重放。"""
    stack = await make_stack(script=[])
    await ArtifactDAO(stack.db).put(
        ArtifactRow.create(
            id=uuid.uuid4().hex, task_id=TASK, stage=STAGE_CASE_GENERATE,
            graph_run_id=RUN, stage_version=1,
            payload={"case_count": 3, "case_ids": ["c1", "c2", "c3"]},
            origin="system", status="active", confirmed_by=None,
            progress=[{"batch_id": "b0", "node": STAGE_CASE_GENERATE,
                       "unit_ids": ["pt-1-1", "pt-1-2"], "status": "done",
                       "idem": "x", "result_ids": ["c1", "c2", "c3"]}],
        )
    )
    inc = await case_generate_node(stack.ctx, _state())
    assert inc["case_count"] == 3
    assert stack.llm.chat_calls == 0


async def test_case_generate_missing_point_plan_raises(make_stack):
    stack = await make_stack(script=[])
    with pytest.raises(Exception):
        await case_generate_node(stack.ctx, {})


# ---------- WP-31 Task 11：context_store 接线 ----------


class _RecordingJournal:
    def __init__(self) -> None:
        self.records: list[JournalRecord] = []

    async def record(self, records: list[JournalRecord]) -> None:
        self.records.extend(records)


async def _seed_active_case_row(ctx) -> None:
    content = CaseFileContent(
        case_id="seed-case-1",
        point_id="pt-seed",
        stage_version=1,
        title="历史用例X",
        priority="P1",
        preconditions=[],
        steps=[CaseStep(seq=1, action="操作", expect="结果")],
        test_data=None,
        trace_refs=TraceRefs(clause_ids=[], entry_ids=[], point_ids=["pt-seed"]),
    )
    record = CaseRecord(
        case_id=content.case_id,
        point_id=content.point_id,
        stage_version=1,
        lineage=Lineage(root_case_id=content.case_id),
        status="active",
        review_status=ReviewStatus.PENDING,
        file_path="seed.md",
        content_hash="h",
        title=content.title,
        trace_refs=content.trace_refs,
    )
    await ctx.daos.testcase.put_batch(
        [CaseRow.from_record(TASK, record, batch_id="bseed")]
    )


async def test_case_generate_item_digests_evicted_at_batch_close(make_stack):
    stack = await make_stack(script=[_mq(), _rr(), CASE_JSON])
    journal = _RecordingJournal()
    context_store = ContextStore(
        owner_type="task", owner_id=TASK, workspace_id=WS, journal=journal
    )
    stack.ctx.context_store = context_store

    await case_generate_node(stack.ctx, _state())

    digests = [
        e
        for e in context_store.entries()
        if e.entry_kind is EntryKind.ARTIFACT_DIGEST
    ]
    # 两点各一条；批次 close → 全部 EVICTED
    assert len(digests) == 2
    assert all(e.status is EntryStatus.EVICTED for e in digests)
    # journal：batch_close marker
    markers = [r for r in journal.records if r.action == JournalAction.BATCH_CLOSE]
    assert len(markers) == 1 and markers[0].reason == "batch_closed"
    # 默认不开 case_index_digest：LLM 消息无索引行
    contents = [m["content"] for m in stack.llm.calls[-1]["messages"]]
    assert not any("【已生成用例索引】" in c for c in contents)


async def test_case_index_digest_injected_when_enabled(make_stack):
    stack = await make_stack(script=[_mq(), _rr(), CASE_JSON])
    await ConfigDAO(stack.db).update_runtime({"context.case_index_digest": True})
    context_store = ContextStore(
        owner_type="task", owner_id=TASK, workspace_id=WS
    )
    stack.ctx.context_store = context_store
    await _seed_active_case_row(stack.ctx)

    await case_generate_node(stack.ctx, _state())

    sys_msgs = [
        m
        for m in stack.llm.calls[-1]["messages"]
        if m["role"] == "system"
    ]
    index = [m for m in sys_msgs if "【已生成用例索引】" in m["content"]]
    assert len(index) == 1
    assert "pt-seed:历史用例X" in index[0]["content"]

