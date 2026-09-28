"""WP-18 批次执行器 + point_write 节点（dd §7.3 §7.5③ §7.4② §2.4 §8.6）。

验收口径（WBS #18）：
- "started 重做、done 跳过、cancel 边界三测"：run_in_batches 崩溃恢复语义
  （done 批跳过、started 批重做、cancel_event 置位后抛 TaskCancelled）；
- point_write 产物断言：artifact(point_write, v1, PointPlan, active, system,
  confirmed_by=null) + progress 落库 + trace 闭环 + state.point_plan。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.adapters.reme import Entry, IndexMirror
from tester_agent.domain import (
    EntryType,
    LinkPlan,
    LinkRef,
    StoryRef,
    TestPoint as PointModel,
)
from tester_agent.errors import LLMBadOutput, TaskCancelled
from tester_agent.graph.batch import (
    BatchProgress,
    deterministic_key,
    run_in_batches,
    start_cursor,
)
from tester_agent.graph.constants import STAGE_POINT_WRITE
from tester_agent.graph.nodes import (
    build_point_intent,
    finalize_point_plan,
    order_stories_by_link,
    point_write_node,
)
from tester_agent.runtime.context import AppContext, DAOs, TaskContext
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ArtifactDAO,
    ArtifactRow,
    ConfigDAO,
    MessageDAO,
    TaskDAO,
    TaskRow,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

from fakes import FakeLLM, FakeReMeReader

WS = "ws1"
TASK = "task1"
CONV = "conv1"
RUN = "run-1"


def _entry(
    eid: str, *, title: str, content: str, etype: EntryType,
    link_id: str | None = None, story_id: str | None = None,
) -> Entry:
    return Entry(
        entry_id=eid, entry_version=f"v-{eid}", title=title, content=content,
        entry_type=etype, link_id=link_id, story_id=story_id,
        updated_at="2026-09-20T00:00:00Z", raw={"summary": title},
    )


# 业务/流程/缺陷条目（point_write passage 档 allowed_types）
ENTRIES = [
    _entry("b1", title="订单业务规则", content="订单 金额 校验 库存 规则",
           etype=EntryType.BUSINESS, link_id="L1"),
    _entry("f1", title="下单主流程", content="下单 购物车 结算 提交 订单",
           etype=EntryType.FLOW_CASE, link_id="L1", story_id="S1"),
    _entry("d1", title="超卖缺陷", content="库存 并发 超卖 扣减 缺陷",
           etype=EntryType.DEFECT, link_id="L1"),
    _entry("b2", title="退款业务规则", content="退款 金额 原路 返回 规则",
           etype=EntryType.BUSINESS, link_id="L2"),
]

RERANK_SCORES = {"b1": 100, "f1": 90, "d1": 80, "b2": 70}


def _mq() -> str:
    return json.dumps(
        {"keyword_queries": ["订单 下单 流程"], "rewrite_queries": ["支付 退款"]},
        ensure_ascii=False,
    )


def _rr() -> str:
    return json.dumps(
        {"scores": [{"entry_id": k, "score": v, "reason": "fixture"}
                    for k, v in RERANK_SCORES.items()]},
        ensure_ascii=False,
    )


# LinkPlan：2 links，L1 有 2 stories、L2 有 1 story
LINK_PLAN = LinkPlan(
    links=[
        LinkRef(link_id="L1", title="订单链路", summary="下单到履约",
                hit=True, entry_id="l1", confidence=0.9,
                story_ids=["S1", "S2"]),
        LinkRef(link_id="L2", title="退款链路", summary="售后逆向",
                hit=True, entry_id="l2", confidence=0.85, story_ids=["S3"]),
    ],
    stories=[
        StoryRef(story_id="S1", link_id="L1", title="下单故事",
                 summary="购物车结算下单", hit=True, entry_id="s1",
                 confidence=0.8, rationale="r", related_clause_ids=["h2-1"]),
        StoryRef(story_id="S2", link_id="L1", title="支付故事",
                 summary="收银台与回调", hit=True, entry_id="s2",
                 confidence=0.88, rationale="r", related_clause_ids=["h2-1-h3-1"]),
        StoryRef(story_id="S3", link_id="L2", title="退款故事",
                 summary="申请退款", hit=True, entry_id="s3",
                 confidence=0.82, rationale="r", related_clause_ids=["h2-2"]),
    ],
    new_suggestions=[],
)

# 本批输出：S1 两点、S2 一点（共 3 点）
POINT_BATCH_JSON = json.dumps(
    {
        "points": [
            {"point_id": "x", "story_id": "S1", "title": "正常下单",
             "angle": "正常", "method": "场景法",
             "clause_ids": ["h2-1"], "source_entry_ids": ["b1", "f1"],
             "priority": "P0"},
            {"point_id": "x", "story_id": "S1", "title": "库存不足",
             "angle": "异常", "method": "边界值",
             "clause_ids": ["h2-1"], "source_entry_ids": ["d1"],
             "priority": "P1"},
            {"point_id": "x", "story_id": "S2", "title": "支付回调",
             "angle": "正常", "method": "状态迁移",
             "clause_ids": ["h2-1-h3-1"], "source_entry_ids": ["b1", "ghost"],
             "priority": "P2"},
        ],
        "clarifications": [],
    },
    ensure_ascii=False,
)


# ---------- 纯函数 ----------


def test_order_stories_by_link_groups_and_indexes():
    ordered, index = order_stories_by_link(LINK_PLAN)
    assert [s.story_id for s in ordered] == ["S1", "S2", "S3"]
    assert index == {"S1": 1, "S2": 2, "S3": 3}


def test_build_point_intent_title_plus_summary():
    intent = build_point_intent([LINK_PLAN.stories[0], LINK_PLAN.stories[2]])
    assert "下单故事" in intent and "购物车结算下单" in intent
    assert "退款故事" in intent


def test_finalize_deterministic_ids_whitelist_and_priority():
    pts, degraded, asserted = finalize_point_plan(
        json.loads(POINT_BATCH_JSON),
        whitelist={"b1", "f1", "d1"},
        story_index={"S1": 1, "S2": 2},
    )
    # point_id = pt-{story序号}-{批内位置}
    assert [p.point_id for p in pts] == ["pt-1-1", "pt-1-2", "pt-2-3"]
    assert [p.story_id for p in pts] == ["S1", "S1", "S2"]
    assert pts[0].priority == "P0" and pts[1].priority == "P1"
    # b1/f1/d1 白名单内保留；ghost 被剔除
    assert pts[2].source_entry_ids == ["b1"]
    assert any("ghost" in d.reason for d in degraded)
    assert "ghost" in asserted and "b1" in asserted


def test_finalize_dangling_story_id_raises():
    with pytest.raises(LLMBadOutput):
        finalize_point_plan(
            {"points": [{"story_id": "S9", "title": "t", "angle": "a",
                         "method": "m", "clause_ids": []}]},
            whitelist=set(), story_index={"S1": 1},
        )


def test_finalize_empty_title_raises():
    with pytest.raises(LLMBadOutput):
        finalize_point_plan(
            {"points": [{"story_id": "S1", "title": "", "angle": "a",
                         "method": "m", "clause_ids": []}]},
            whitelist=set(), story_index={"S1": 1},
        )


def test_finalize_nonempty_clarifications_raises():
    with pytest.raises(LLMBadOutput):
        finalize_point_plan(
            {"points": [], "clarifications": [{"question": "q"}]},
            whitelist=set(), story_index={},
        )


def test_finalize_invalid_priority_defaults_p1():
    pts, _, _ = finalize_point_plan(
        {"points": [{"story_id": "S1", "title": "t", "angle": "a",
                     "method": "m", "clause_ids": [], "priority": "P9"}]},
        whitelist=set(), story_index={"S1": 1},
    )
    assert pts[0].priority == "P1"


# ---------- BatchProgress / cursor / idem ----------


def test_start_cursor_deterministic_same_run_different_runs():
    c1 = start_cursor(TASK, RUN, STAGE_POINT_WRITE)
    c2 = start_cursor(TASK, RUN, STAGE_POINT_WRITE)
    c3 = start_cursor(TASK, "run-2", STAGE_POINT_WRITE)
    assert c1["idempotency_nonce"] == c2["idempotency_nonce"]
    assert c1["idempotency_nonce"] != c3["idempotency_nonce"]
    assert c1["next_index"] == 0


def test_deterministic_key_stable():
    nonce = start_cursor(TASK, RUN, STAGE_POINT_WRITE)["idempotency_nonce"]
    k1 = deterministic_key(TASK, RUN, STAGE_POINT_WRITE, "b0", nonce)
    k2 = deterministic_key(TASK, RUN, STAGE_POINT_WRITE, "b0", nonce)
    k3 = deterministic_key(TASK, RUN, STAGE_POINT_WRITE, "b1", nonce)
    assert k1 == k2 and k1 != k3


def test_batch_progress_mark_lifecycle():
    p = BatchProgress(STAGE_POINT_WRITE, [])
    p.mark_started("b0", ["S1", "S2"], "idem-0")
    assert p.status("b0") == "started"
    p.mark_done("b0", ["pt-1-1", "pt-1-2"])
    assert p.status("b0") == "done"
    p.mark_failed("b0")
    assert p.status("b0") == "failed"
    dumped = p.dump()
    assert dumped[0]["unit_ids"] == ["S1", "S2"]
    assert dumped[0]["result_ids"] == ["pt-1-1", "pt-1-2"]


# ---------- run_in_batches：done 跳过 / started 重做 / cancel 边界 ----------


@dataclass
class _FakeUnit:
    uid: str


@dataclass
class _FakeResult:
    rid: str


@pytest.fixture()
async def batch_ctx(tmp_path):
    """构造最小上下文：db + active artifact（可选预置 progress），测试后关库。"""
    db_path = tmp_path / "b.db"
    run_migrations(db_path)
    db = Database(db_path)
    await WorkspaceDAO(db).create(
        WorkspaceRow.create(id=WS, name="w", kb_config={"kb_id": "KB", "options": {}})
    )
    await db.aexecute(
        "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (CONV, WS, "t", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
    )
    await TaskDAO(db).create(
        TaskRow.create(id=TASK, conversation_id=CONV, workspace_id=WS,
                       status="running", current_stage=STAGE_POINT_WRITE,
                       langgraph_thread_id="t1", graph_run_id=RUN)
    )
    artifact_id = uuid.uuid4().hex
    store = FileStore(tmp_path / "data")
    app = AppContext(db=db, file_store=store, llm=FakeLLM([]),
                     reme_factory=None, config=ConfigDAO(db))
    task = await TaskDAO(db).get(TASK)

    async def make(*, artifact_progress=None):
        await ArtifactDAO(db).put(
            ArtifactRow.create(
                id=artifact_id, task_id=TASK, stage=STAGE_POINT_WRITE,
                graph_run_id=RUN, stage_version=1, payload={},
                origin="system", status="active",
                progress=artifact_progress or [],
            )
        )
        ctx = TaskContext(
            app=app, task=task, run_id=RUN, files=store,
            reader=FakeReMeReader([]), snapshot_level="off",
            mirror=IndexMirror(FakeReMeReader([])),
            daos=DAOs(task=TaskDAO(db), message=MessageDAO(db),
                      artifact=ArtifactDAO(db)),
        )
        return ctx, db, artifact_id

    yield make
    db.close()


async def test_run_in_batches_done_skip(batch_ctx):
    ctx, db, aid = await batch_ctx(
        artifact_progress=[
            {"batch_id": "b0", "node": STAGE_POINT_WRITE, "unit_ids": ["u1"],
             "status": "done", "idem": "x", "result_ids": ["r1"]}
        ],
    )
    units = [_FakeUnit("u1"), _FakeUnit("u2")]
    calls: list[str] = []

    async def worker(_ctx, batch_id, subset):
        calls.append(batch_id)
        return [_FakeResult(f"r-{u.uid}") for u in subset]

    results, cursor = await run_in_batches(
        ctx, STAGE_POINT_WRITE, units, worker, size=1, state={},
        unit_id=lambda u: u.uid, result_id=lambda r: r.rid,
    )
    # b0 已 done → 跳过，只跑 b1
    assert calls == ["b1"]
    assert [r.rid for r in results] == ["r-u2"]
    assert cursor["next_index"] == 2


async def test_run_in_batches_started_redo(batch_ctx):
    """started 批视为未落库完成，重做（不跳过）。"""
    ctx, db, aid = await batch_ctx(
        artifact_progress=[
            {"batch_id": "b0", "node": STAGE_POINT_WRITE, "unit_ids": ["u1"],
             "status": "started", "idem": "x", "result_ids": []}
        ],
    )
    units = [_FakeUnit("u1")]
    calls: list[str] = []

    async def worker(_ctx, batch_id, subset):
        calls.append(batch_id)
        return [_FakeResult("r1")]

    results, _ = await run_in_batches(
        ctx, STAGE_POINT_WRITE, units, worker, size=1, state={},
        unit_id=lambda u: u.uid, result_id=lambda r: r.rid,
    )
    assert calls == ["b0"]  # started 重做
    assert [r.rid for r in results] == ["r1"]
    # progress 最终为 done
    art = await ArtifactDAO(db).get(aid)
    assert art.progress_list()[0]["status"] == "done"
    assert art.progress_list()[0]["result_ids"] == ["r1"]


async def test_run_in_batches_cancel_boundary(batch_ctx):
    """取消事件在批边界触发：已 done 批跳过，未开始批抛 TaskCancelled。"""
    ctx, db, aid = await batch_ctx(
        artifact_progress=[
            {"batch_id": "b0", "node": STAGE_POINT_WRITE, "unit_ids": ["u1"],
             "status": "done", "idem": "x", "result_ids": ["r1"]}
        ],
    )
    ctx.cancel_event = asyncio.Event()
    ctx.cancel_event.set()  # 取消
    units = [_FakeUnit("u1"), _FakeUnit("u2")]
    calls: list[str] = []

    async def worker(_ctx, batch_id, subset):
        calls.append(batch_id)
        return [_FakeResult("r")]

    with pytest.raises(TaskCancelled):
        await run_in_batches(
            ctx, STAGE_POINT_WRITE, units, worker, size=1, state={},
            unit_id=lambda u: u.uid, result_id=lambda r: r.rid,
        )
    assert calls == []  # b0 跳过、b1 在边界被取消


async def test_run_in_batches_failure_marks_failed_and_raises(batch_ctx):
    ctx, db, aid = await batch_ctx()
    units = [_FakeUnit("u1")]

    async def worker(_ctx, batch_id, subset):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await run_in_batches(
            ctx, STAGE_POINT_WRITE, units, worker, size=1, state={},
            unit_id=lambda u: u.uid, result_id=lambda r: r.rid,
        )
    art = await ArtifactDAO(db).get(aid)
    assert art.progress_list()[0]["status"] == "failed"


# ---------- point_write_node 端到端 ----------


@pytest.fixture()
async def make_stack(tmp_path):
    handles: list[tuple[Database, Any]] = []

    async def build(*, script: list, entries: list[Entry] | None = None,
                    agent_config: dict | None = None, snapshot_level: str = "off"
                    ) -> "Stack":
        db_path = tmp_path / f"app-{len(handles)}.db"
        run_migrations(db_path)
        db = Database(db_path)
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=WS, name="ws-name",
                                kb_config={"kb_id": "KB1", "options": {}})
        )
        await db.aexecute(
            "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (CONV, WS, "t", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
        )
        await TaskDAO(db).create(
            TaskRow.create(id=TASK, conversation_id=CONV, workspace_id=WS,
                           status="running", current_stage=STAGE_POINT_WRITE,
                           langgraph_thread_id="thread-1", graph_run_id=RUN,
                           snapshot_level=snapshot_level)
        )
        store = FileStore(tmp_path / "data")
        md = (
            "# 需求\n\n## 订单链路\n\n订单 下单 支付 库存。\n\n"
            "### 支付故事\n\n支付 回调 关单。\n\n## 退款链路\n\n退款 申请。\n"
        )
        await store.save_requirement(WS, TASK, md)
        # 手动落 clauses cache（含偏移），供 point_write 读条款原文
        await store.save_clauses_cache(WS, TASK, _clauses_cache(md))
        llm = FakeLLM(script)
        reader = FakeReMeReader(entries if entries is not None else ENTRIES)
        mirror = IndexMirror(reader)
        app = AppContext(db=db, file_store=store, llm=llm, reme_factory=None,
                         config=ConfigDAO(db))
        ctx = TaskContext(
            app=app, task=await TaskDAO(db).get(TASK), run_id=RUN,
            files=store, reader=reader, snapshot_level=snapshot_level,
            mirror=mirror,
            agent_config=agent_config or {"ambiguity_check": False},
            daos=DAOs(task=TaskDAO(db), message=MessageDAO(db),
                      artifact=ArtifactDAO(db)),
        )
        handles.append((db, None))
        return Stack(ctx=ctx, db=db, store=store, llm=llm, reader=reader)

    yield build
    for db, _ in handles:
        db.close()


@dataclass
class Stack:
    ctx: TaskContext
    db: Database
    store: FileStore
    llm: FakeLLM
    reader: FakeReMeReader


def _clauses_cache(md: str) -> list[dict]:
    """复刻 intake 切分产物（含 start/end 偏移），供 read_clause 寻址。"""
    raw = md.encode("utf-8")
    # 简单按行找 ## 标题
    lines = md.split("\n")
    out = []
    cur = None
    pos = 0
    for ln in lines:
        ln_bytes = (ln + "\n").encode("utf-8")
        start = pos
        pos += len(ln_bytes)
        if ln.startswith("##") and not ln.startswith("###"):
            if cur is not None:
                cur["end_offset"] = start
                out.append(cur)
            cur = {
                "clause_id": f"h2-{len(out) + 1}",
                "level": 2, "title_path": [ln.lstrip("#").strip()],
                "anchor": ln.lstrip("#").strip()[:32],
                "text_hash": "x", "status": "active",
                "start_offset": start, "end_offset": start,
            }
        elif ln.startswith("###"):
            if cur is not None:
                cur["end_offset"] = start
                out.append(cur)
            cur = {
                "clause_id": f"h2-{len([c for c in out if c['level']==2])}-h3-1",
                "level": 3,
                "title_path": [c["title_path"][0] for c in out if c["level"] == 2][-1:]
                              + [ln.lstrip("#").strip()],
                "anchor": ln.lstrip("#").strip()[:32],
                "text_hash": "x", "status": "active",
                "start_offset": start, "end_offset": start,
            }
    if cur is not None:
        cur["end_offset"] = len(raw)
        out.append(cur)
    return out


def _state(link_plan: dict | None = None) -> dict:
    return {"link_plan": link_plan or LINK_PLAN.model_dump(), "clauses": []}


async def test_point_write_happy_path_artifact_progress_and_trace(make_stack):
    # 3 stories，size=5 → 单批 b0；脚本：mq + rr + points
    stack = await make_stack(script=[_mq(), _rr(), POINT_BATCH_JSON])
    inc = await point_write_node(stack.ctx, _state())

    assert inc["point_plan"] is not None
    pts = inc["point_plan"]["points"]
    assert [p["point_id"] for p in pts] == ["pt-1-1", "pt-1-2", "pt-2-3"]
    assert inc["current_stage_version"] == {STAGE_POINT_WRITE: 1}
    assert inc["batch_cursor"][STAGE_POINT_WRITE]["next_index"] == 3

    # artifact：active / v1 / system / confirmed_by=null，payload 含 points
    art = await ArtifactDAO(stack.db).get_active(TASK, STAGE_POINT_WRITE)
    assert art.stage_version == 1 and art.status == "active"
    assert art.origin == "system" and art.confirmed_by is None
    assert art.graph_run_id == RUN
    payload = art.payload_dict()
    assert len(payload["points"]) == 3
    # progress：b0 done
    prog = art.progress_list()
    assert prog[0]["batch_id"] == "b0" and prog[0]["status"] == "done"
    assert prog[0]["unit_ids"] == ["S1", "S2", "S3"]
    assert set(prog[0]["result_ids"]) == {"pt-1-1", "pt-1-2", "pt-2-3"}

    # trace：闭环已写（referenced/hallucinated 回填）
    traces = await stack.db.aquery(
        "SELECT stage, batch_id, injected_ids, referenced_ids, hallucinated_ids "
        "FROM retrieval_trace WHERE task_id = ?", (TASK,)
    )
    assert len(traces) == 1
    assert traces[0]["stage"] == STAGE_POINT_WRITE
    assert traces[0]["batch_id"] == "b0"
    # ghost 是幻觉（不在白名单）
    assert "ghost" in (traces[0]["hallucinated_ids"] or "")
    # LLM 调用：1 次 multi_query + 1 次 rerank + 1 次 point_write = 3
    assert stack.llm.chat_calls == 3


async def test_point_write_crash_replay_full_payload_zero_llm(make_stack):
    """同 run 已有完整 payload 的 artifact → 直接回放，零 LLM/检索。"""
    stack = await make_stack(script=[])  # 空脚本：若调 LLM 会报错
    # 预置完整 artifact
    await ArtifactDAO(stack.db).put(
        ArtifactRow.create(
            id=uuid.uuid4().hex, task_id=TASK, stage=STAGE_POINT_WRITE,
            graph_run_id=RUN, stage_version=1,
            payload={"points": [{"point_id": "pt-1-1", "story_id": "S1",
                                 "title": "t", "angle": "a", "method": "m",
                                 "clause_ids": [], "source_entry_ids": [],
                                 "priority": "P1"}]},
            origin="system", status="active", confirmed_by=None,
            progress=[{"batch_id": "b0", "node": STAGE_POINT_WRITE,
                       "unit_ids": ["S1"], "status": "done", "idem": "x",
                       "result_ids": ["pt-1-1"]}],
        )
    )
    inc = await point_write_node(stack.ctx, _state())
    assert inc["point_plan"]["points"][0]["point_id"] == "pt-1-1"
    assert stack.llm.chat_calls == 0  # 零 LLM


async def test_point_write_crash_mid_batch_reuses_progress_skips_done(make_stack):
    """崩溃在批次中途：artifact 存在但 payload 空、progress 有 done 批 →
    重跑时跳过 done 批（不调 LLM 重做已完成批）。"""
    # 3 stories / size=5 = 单批；预置 b0 done + 空 payload
    stack = await make_stack(script=[])
    await ArtifactDAO(stack.db).put(
        ArtifactRow.create(
            id=uuid.uuid4().hex, task_id=TASK, stage=STAGE_POINT_WRITE,
            graph_run_id=RUN, stage_version=1, payload={},
            origin="system", status="active", confirmed_by=None,
            progress=[{"batch_id": "b0", "node": STAGE_POINT_WRITE,
                       "unit_ids": ["S1", "S2", "S3"], "status": "done",
                       "idem": "x", "result_ids": ["pt-1-1"]}],
        )
    )
    inc = await point_write_node(stack.ctx, _state())
    # 空 payload → 重跑批次，但 b0 done → worker 不被调用 → 无 LLM
    assert stack.llm.chat_calls == 0
    # points 为空（worker 未产出），payload 回填为 {"points": []}
    assert inc["point_plan"]["points"] == []


async def test_point_write_whitelist_degraded_recorded(make_stack):
    """source_entry_ids 含非白名单 → 剔除 + trace.degraded 记录。"""
    stack = await make_stack(script=[_mq(), _rr(), POINT_BATCH_JSON])
    await point_write_node(stack.ctx, _state())
    traces = await stack.db.aquery(
        "SELECT degraded FROM retrieval_trace WHERE task_id = ?", (TASK,)
    )
    degraded = json.loads(traces[0]["degraded"] or "[]")
    steps = [d["step"] for d in degraded]
    assert "point_write.source_whitelist" in steps
    assert any("ghost" in d["reason"] for d in degraded)


async def test_point_write_missing_link_plan_raises(make_stack):
    stack = await make_stack(script=[])
    with pytest.raises(Exception):
        await point_write_node(stack.ctx, {})
