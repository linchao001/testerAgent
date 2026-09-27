"""WP-20 coverage_check（dd §7.5④ §2.6）。

验收口径（WBS #20）：全覆盖 / 部分覆盖（补充生成兜底）/ 达上限三态；
告警事件 coverage_ready（degraded 时 warnings 非空）。
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.adapters.reme import Entry, EntryType, IndexMirror
from tester_agent.domain import (
    ClauseRef,
    LinkPlan,
    LinkRef,
    PointPlan,
    StoryRef,
    TestPoint as PointModel,
)
from tester_agent.graph.constants import (
    STAGE_CASE_GENERATE,
    STAGE_COVERAGE_CHECK,
)
from tester_agent.graph.nodes import (
    build_coverage_matrix,
    build_virtual_points,
    commit_case_batch,
    coverage_check_node,
    count_supp_rounds,
    finalize_cases,
    matrix_summary,
)
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

C1, C2, C3 = "h2-1", "h2-1-h3-1", "h2-2"


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
                 related_clause_ids=[C1]),
        StoryRef(story_id="S2", link_id="L1", title="支付", summary="支付回调",
                 hit=True, entry_id="s2", confidence=0.88, rationale="r",
                 related_clause_ids=[C2]),
    ],
    new_suggestions=[],
)

POINT_PLAN = PointPlan(points=[
    PointModel(point_id="pt-1-1", story_id="S1", title="正常下单", angle="正常",
               method="场景法", clause_ids=[C1], source_entry_ids=["b1"], priority="P0"),
    PointModel(point_id="pt-1-2", story_id="S2", title="支付回调", angle="正常",
               method="状态迁移", clause_ids=[C2], source_entry_ids=["a1"], priority="P1"),
])

CLAUSES = [
    ClauseRef(clause_id=C1, level=2, title_path=["订单"], anchor="订单",
              text_hash="x", status="active"),
    ClauseRef(clause_id=C2, level=3, title_path=["订单", "支付"], anchor="支付",
              text_hash="x", status="active"),
    ClauseRef(clause_id=C3, level=2, title_path=["退款"], anchor="退款",
              text_hash="x", status="active"),
]


def _mq():
    return json.dumps({"keyword_queries": ["订单 支付 退款"],
                       "rewrite_queries": ["下单 回调 退款"]},
                      ensure_ascii=False)


def _rr():
    return json.dumps({"scores": [{"entry_id": k, "score": v, "reason": "f"}
                                   for k, v in RERANK_SCORES.items()]}, ensure_ascii=False)


def _case(title, cid):
    return {"title": title, "priority": "P1", "preconditions": [],
            "steps": [{"seq": 1, "action": f"执行-{title}", "expect": "通过"}],
            "test_data": None,
            "trace_refs": {"clause_ids": [cid], "entry_ids": []}}


def _by_point(mapping):
    return json.dumps({"by_point": mapping}, ensure_ascii=False)


class SmartLLM:
    """按 json_schema 区分 mq/rerank/生成调用的替身（多补充轮时 rerank 走
    缓存零调用，脚本队列会错位，故按 schema 即时应答；结构对型 FakeLLM）。"""

    def __init__(self, case_responses: list[str]):
        from tester_agent.adapters.llm import LLMResult

        self._result_cls = LLMResult
        self._case_responses = list(case_responses)
        self.chat_calls = 0
        self.kinds: list[str] = []
        self.calls: list[dict] = []

    async def chat(self, messages, *, model=None, temperature=None,
                   json_schema=None, timeout=None, stream_writer=None):
        self.chat_calls += 1
        self.calls.append({"messages": messages, "json_schema": json_schema})
        required = set((json_schema or {}).get("required") or [])
        if "by_point" in required:
            kind = "case"
            content = self._case_responses.pop(0)
        elif "scores" in required:
            kind = "rerank"
            content = _rr()
        else:
            kind = "multi_query"
            content = _mq()
        self.kinds.append(kind)
        return self._result_cls(
            content=content,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            model=model or "fake-model", finish_reason="stop", retries=0,
            latency_ms=1,
        )


# 两真实点各产出用例（b0 批，覆盖 C1/C2）
B0_JSON = _by_point({
    "pt-1-1": [_case("提交订单成功", C1)],
    "pt-1-2": [_case("支付回调更新状态", C2)],
})
# 仅 pt-1-1 有例（C2 点在无例、C3 无点）
B0_PARTIAL_JSON = _by_point({"pt-1-1": [_case("提交订单成功", C1)]})
EMPTY_JSON = _by_point({})


# ---------- 纯函数 ----------


def test_build_virtual_points_deterministic():
    vps1 = build_virtual_points(CLAUSES, round_no=1)
    assert [p.point_id for p in vps1] == ["pt-sup1-1", "pt-sup1-2", "pt-sup1-3"]
    assert vps1[0].clause_ids == [C1] and vps1[0].story_id == ""
    assert vps1[0].source_entry_ids == []
    assert "订单" in vps1[0].title
    # 同输入恒等
    assert build_virtual_points(CLAUSES, round_no=1) == vps1
    # 不同轮次不同 point_id（→ case_id 确定性派生也不撞）
    vps2 = build_virtual_points(CLAUSES[:1], round_no=2)
    assert vps2[0].point_id == "pt-sup2-1"


def test_matrix_point_and_case_rows_and_uncovered():
    # pt-1-1 命中 C1 且有 active 例；pt-1-2 命中 C2 但无例；C3 无点
    from tester_agent.domain import CaseRecord, Lineage, ReviewStatus, TraceRefs as TR

    case_rows = [
        CaseRow.from_record(
            TASK,
            CaseRecord(case_id="case-1", point_id="pt-1-1", stage_version=1,
                       lineage=Lineage(root_case_id="case-1"), status="active",
                       review_status=ReviewStatus.PENDING, file_path="p",
                       content_hash="h", title="提交订单成功", trace_refs=TR()),
            batch_id="b0",
        )
    ]
    m = build_coverage_matrix(CLAUSES, POINT_PLAN.points, case_rows)
    assert m.uncovered_clauses == [C2, C3]
    point_rows = {(r.clause_id, r.object_id) for r in m.rows if r.object_type == "point"}
    assert point_rows == {(C1, "pt-1-1"), (C2, "pt-1-2")}
    case_hit = [r for r in m.rows if r.object_type == "case"]
    assert len(case_hit) == 1 and case_hit[0].object_id == "case-1"
    assert case_hit[0].evidence == "提交订单成功"
    # evidence 取对象标题
    assert any(r.object_id == "pt-1-2" and r.evidence == "支付回调" for r in m.rows)
    assert m.supplemental_rounds == 0 and m.degraded is False
    # 全表 covered 行均为 True（未覆盖只经 uncovered_clauses 表达）
    assert all(r.covered for r in m.rows)


def test_matrix_ignores_obsolete_case_and_deleted_clause():
    from tester_agent.domain import CaseRecord, Lineage, ReviewStatus, TraceRefs as TR

    obsolete = CaseRow.from_record(
        TASK,
        CaseRecord(case_id="case-old", point_id="pt-1-1", stage_version=1,
                   lineage=Lineage(root_case_id="case-old"), status="obsolete",
                   review_status=ReviewStatus.PENDING, file_path="p",
                   content_hash="h", title="旧例", trace_refs=TR()),
        batch_id="b0",
    )
    clauses = [*CLAUSES,
               ClauseRef(clause_id="h2-9", level=2, title_path=["删除节"],
                         anchor="x", text_hash="x", status="deleted")]
    m = build_coverage_matrix(clauses, POINT_PLAN.points, [obsolete])
    # obsolete 例不计例覆盖：C1 也未覆盖
    assert m.uncovered_clauses == [C1, C2, C3]
    assert "h2-9" not in m.uncovered_clauses
    assert all(r.clause_id != "h2-9" for r in m.rows)


def test_matrix_full_coverage():
    from tester_agent.domain import CaseRecord, Lineage, ReviewStatus, TraceRefs as TR

    rows = [
        CaseRow.from_record(
            TASK,
            CaseRecord(case_id=f"case-{p.point_id}", point_id=p.point_id, stage_version=1,
                       lineage=Lineage(root_case_id=f"case-{p.point_id}"), status="active",
                       review_status=ReviewStatus.PENDING, file_path="p",
                       content_hash="h", title=p.title, trace_refs=TR()),
            batch_id="b0",
        )
        for p in POINT_PLAN.points
    ]
    clauses = CLAUSES[:2]  # 仅 C1/C2，均有點有例
    m = build_coverage_matrix(clauses, POINT_PLAN.points, rows)
    assert m.uncovered_clauses == []
    summary = matrix_summary(m, clause_count=len(clauses))
    assert summary["covered_clause_count"] == 2
    assert summary["point_row_count"] == 2 and summary["case_row_count"] == 2


def test_count_supp_rounds():
    progress = [
        {"batch_id": "b0", "status": "done"},
        {"batch_id": "sup1", "status": "done"},
        {"batch_id": "sup2", "status": "done"},
    ]
    assert count_supp_rounds(progress) == 2
    assert count_supp_rounds([]) == 0
    assert count_supp_rounds([{"batch_id": "b0"}]) == 0
    assert count_supp_rounds([{"batch_id": "supx"}]) == 0


# ---------- 节点端到端 ----------


@dataclass
class Stack:
    ctx: TaskContext
    db: Database
    store: FileStore
    llm: FakeLLM
    reader: FakeReMeReader
    events: list


@pytest.fixture()
async def make_stack(tmp_path):
    handles: list[Database] = []

    async def build(*, script=None, runtime_config=None, llm=None) -> Stack:
        db_path = tmp_path / f"app-{len(handles)}.db"
        run_migrations(db_path)
        db = Database(db_path)
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=WS, name="w",
                                kb_config={"kb_id": "KB1", "mode": "sdk"})
        )
        await db.aexecute(
            "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (CONV, WS, "t", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
        )
        await TaskDAO(db).create(
            TaskRow.create(id=TASK, conversation_id=CONV, workspace_id=WS,
                           status="running", current_stage=STAGE_COVERAGE_CHECK,
                           langgraph_thread_id="t1", graph_run_id=RUN)
        )
        if runtime_config is not None:
            await ConfigDAO(db).update_runtime(runtime_config)
        store = FileStore(tmp_path / "data")
        md = "# 需求\n\n## 订单\n\n下单 支付。\n\n### 支付\n\n回调。\n\n## 退款\n\n退款规则。\n"
        await store.save_requirement(WS, TASK, md)
        await store.save_clauses_cache(WS, TASK, _clauses_cache(md))
        llm = llm or FakeLLM(script or [])
        reader = FakeReMeReader(ENTRIES)
        mirror = IndexMirror(reader)
        app = AppContext(db=db, file_store=store, llm=llm, reme_factory=None,
                         config=ConfigDAO(db))
        events: list = []

        async def emit(type_, payload):
            events.append((type_, payload))

        ctx = TaskContext(
            app=app, task=await TaskDAO(db).get(TASK), run_id=RUN,
            files=store, reader=reader, snapshot_level="off",
            mirror=mirror, agent_config={}, emit=emit,
            daos=DAOs(task=TaskDAO(db), message=MessageDAO(db),
                      artifact=ArtifactDAO(db), testcase=TestcaseDAO(db),
                      trace=TraceDAO(db)),
        )
        handles.append(db)
        return Stack(ctx=ctx, db=db, store=store, llm=llm, reader=reader,
                     events=events)

    yield build
    for db in handles:
        db.close()


def _clauses_cache(md):
    """与 intake 落盘形态一致的极简 clauses cache（start/end 字节偏移）。"""
    lines = md.split("\n")
    out = []
    pos = 0
    cur = None
    h2_count = 0
    for ln in lines:
        b = (ln + "\n").encode("utf-8")
        start = pos
        pos += len(b)
        if ln.startswith("## ") and not ln.startswith("### "):
            if cur:
                cur["end_offset"] = start
                out.append(cur)
            h2_count += 1
            cid = C1 if h2_count == 1 else C3
            cur = {"clause_id": cid, "level": 2,
                   "title_path": [ln.lstrip("#").strip()], "anchor": "x",
                   "text_hash": "x", "status": "active",
                   "start_offset": start, "end_offset": start}
        elif ln.startswith("### "):
            if cur:
                cur["end_offset"] = start
                out.append(cur)
            cur = {"clause_id": C2, "level": 3,
                   "title_path": ["订单", ln.lstrip("#").strip()], "anchor": "x",
                   "text_hash": "x", "status": "active",
                   "start_offset": start, "end_offset": start}
    if cur:
        cur["end_offset"] = len(md.encode("utf-8"))
        out.append(cur)
    return out


def _state(clauses=CLAUSES):
    return {
        "clauses": [c.model_dump() for c in clauses],
        "point_plan": POINT_PLAN.model_dump(),
        "link_plan": LINK_PLAN.model_dump(),
    }


async def _make_case_artifact(ctx, progress=None):
    await ArtifactDAO(ctx.app.db).put(
        ArtifactRow.create(
            id=uuid.uuid4().hex, task_id=TASK, stage=STAGE_CASE_GENERATE,
            graph_run_id=RUN, stage_version=1, payload={},
            origin="system", status="active", confirmed_by=None,
            progress=progress or [],
        )
    )


async def _commit(ctx, points, raw_json, *, batch_id="b0", whitelist=None):
    cases, _, _ = finalize_cases(
        json.loads(raw_json), points=points,
        whitelist=whitelist or set(),
        task_id=TASK, version=1, batch_id=batch_id,
    )
    return await commit_case_batch(ctx, batch_id, cases, version=1)


async def test_node_full_coverage_zero_llm(make_stack):
    """态一：全覆盖——零补充 LLM，coverage_ready warnings 空。"""
    stack = await make_stack(script=[])
    await _make_case_artifact(stack.ctx)
    await _commit(stack.ctx, POINT_PLAN.points, B0_JSON)

    inc = await coverage_check_node(stack.ctx, _state(CLAUSES[:2]))

    assert stack.llm.chat_calls == 0
    matrix = inc["coverage"]
    assert matrix["uncovered_clauses"] == []
    assert matrix["degraded"] is False and matrix["supplemental_rounds"] == 0
    assert inc["current_stage_version"][STAGE_COVERAGE_CHECK] == 1
    art = await ArtifactDAO(stack.db).get_active(TASK, STAGE_COVERAGE_CHECK)
    assert art.payload_dict()["uncovered_clauses"] == []
    # coverage_ready 事件：全覆盖 warnings 为空
    ready = [e for e in stack.events if e[0] == "coverage_ready"]
    assert len(ready) == 1
    assert ready[0][1]["warnings"] == []
    assert ready[0][1]["matrix_summary"]["covered_clause_count"] == 2


async def test_node_partial_covered_by_one_supplemental_round(make_stack):
    """态二：部分覆盖——sup1 一轮补齐，重算矩阵全覆盖。"""
    stack = await make_stack(script=[
        _mq(), _rr(),
        _by_point({
            "pt-sup1-1": [_case("补充-支付回调", C2)],
            "pt-sup1-2": [_case("补充-退款规则", C3)],
        }),
    ])
    await _make_case_artifact(stack.ctx)
    await _commit(stack.ctx, POINT_PLAN.points, B0_PARTIAL_JSON)

    inc = await coverage_check_node(stack.ctx, _state())

    matrix = inc["coverage"]
    assert matrix["uncovered_clauses"] == []
    assert matrix["degraded"] is False
    assert matrix["supplemental_rounds"] == 1
    # 补充批走同一写入路径：testcase 行 batch_id=sup1、point 挂虚拟点
    sup_rows = await TestcaseDAO(stack.db).list_by_batch(TASK, 1, "sup1")
    active = [r for r in sup_rows if r.status == "active"]
    assert len(active) == 2
    assert {r.point_id for r in active} == {"pt-sup1-1", "pt-sup1-2"}
    # mq + rr + case 生成各一次
    assert stack.llm.chat_calls == 3
    # 提示词按虚拟点分段
    last_messages = stack.llm.calls[-1]["messages"]
    assert "pt-sup1-1" in last_messages[-1]["content"]
    ready = [e for e in stack.events if e[0] == "coverage_ready"][0]
    assert ready[1]["warnings"] == []
    assert ready[1]["matrix_summary"]["supplemental_rounds"] == 1
    # case_generate artifact progress 挂 sup1 记录
    case_art = await ArtifactDAO(stack.db).get_active(TASK, STAGE_CASE_GENERATE)
    assert any(b.get("batch_id") == "sup1" for b in case_art.progress_list())


async def test_node_degraded_after_max_rounds_with_warning(make_stack):
    """态三：两轮补充均未产出（模型空响应）→ degraded 告警。"""
    stack = await make_stack(llm=SmartLLM([EMPTY_JSON, EMPTY_JSON]))
    await _make_case_artifact(stack.ctx)
    await _commit(stack.ctx, POINT_PLAN.points, B0_PARTIAL_JSON)

    inc = await coverage_check_node(stack.ctx, _state())

    matrix = inc["coverage"]
    assert matrix["degraded"] is True
    assert matrix["supplemental_rounds"] == 2
    assert matrix["uncovered_clauses"] == [C2, C3]
    # 两轮各一次 multi_query + 生成；第二轮 rerank 命中 run 内缓存零调用
    kinds = stack.llm.kinds
    assert kinds.count("case") == 2
    assert kinds.count("multi_query") == 2
    assert kinds.count("rerank") == 1
    ready = [e for e in stack.events if e[0] == "coverage_ready"][0]
    warnings = ready[1]["warnings"]
    assert len(warnings) == 1
    assert warnings[0]["reason"] == "uncovered_after_max_rounds"
    assert warnings[0]["uncovered_clauses"] == [C2, C3]
    assert warnings[0]["max_rounds"] == 2
    assert ready[1]["matrix_summary"]["degraded"] is True


async def test_node_max_rounds_config_override(make_stack):
    """runtime_config.coverage_max_rounds=1 → 只补一轮即降级。"""
    stack = await make_stack(
        script=[_mq(), _rr(), EMPTY_JSON],
        runtime_config={"coverage_max_rounds": 1},
    )
    await _make_case_artifact(stack.ctx)
    await _commit(stack.ctx, POINT_PLAN.points, B0_PARTIAL_JSON)

    inc = await coverage_check_node(stack.ctx, _state())

    assert inc["coverage"]["supplemental_rounds"] == 1
    assert inc["coverage"]["degraded"] is True
    assert stack.llm.chat_calls == 3


async def test_node_supplemental_crash_rebuild_zero_llm_for_done_round(make_stack):
    """崩溃恢复：sup1 已提交（progress 有记录）→ 重建历史轮零 LLM，仅跑 sup2。"""
    sup1_points = build_virtual_points([CLAUSES[1], CLAUSES[2]], round_no=1)
    stack = await make_stack(script=[
        _mq(), _rr(),
        _by_point({"pt-sup2-1": [_case("补充-退款规则", C3)]}),
    ])
    await _make_case_artifact(stack.ctx)
    await _commit(stack.ctx, POINT_PLAN.points, B0_PARTIAL_JSON)
    # 崩溃前 sup1 已完整提交：仅 C2 被补齐（pt-sup1-1 有例，C3 仍未覆盖）
    await _commit(stack.ctx, sup1_points,
                  _by_point({"pt-sup1-1": [_case("补充-支付回调", C2)]}),
                  batch_id="sup1")

    inc = await coverage_check_node(stack.ctx, _state())

    matrix = inc["coverage"]
    assert matrix["uncovered_clauses"] == []
    assert matrix["supplemental_rounds"] == 2
    # 只跑了 sup2 一轮：mq+rr+生成
    assert stack.llm.chat_calls == 3
    sup2_rows = await TestcaseDAO(stack.db).list_by_batch(TASK, 1, "sup2")
    assert len([r for r in sup2_rows if r.status == "active"]) == 1
    assert sup2_rows[0].point_id == "pt-sup2-1"


async def test_node_replay_existing_coverage_artifact_zero_llm(make_stack):
    """同 run 已有终态矩阵 → 直接回放，零 LLM/检索。"""
    stack = await make_stack(script=[])
    await _make_case_artifact(stack.ctx)
    await _commit(stack.ctx, POINT_PLAN.points, B0_JSON)
    saved = {
        "rows": [{"clause_id": C1, "object_type": "point", "object_id": "pt-1-1",
                  "covered": True, "evidence": "正常下单"}],
        "uncovered_clauses": [],
        "supplemental_rounds": 0,
        "degraded": False,
    }
    await ArtifactDAO(stack.db).put(
        ArtifactRow.create(
            id=uuid.uuid4().hex, task_id=TASK, stage=STAGE_COVERAGE_CHECK,
            graph_run_id=RUN, stage_version=1, payload=saved,
            origin="system", status="active", confirmed_by=None, progress=[],
        )
    )

    inc = await coverage_check_node(stack.ctx, _state(CLAUSES[:2]))

    assert stack.llm.chat_calls == 0
    assert inc["coverage"] == saved


async def test_node_missing_inputs_raise(make_stack):
    # 缺 clauses（有 point_plan）
    stack = await make_stack(script=[])
    with pytest.raises(Exception):
        await coverage_check_node(
            stack.ctx, {"point_plan": POINT_PLAN.model_dump()}
        )
    # 缺 point_plan
    stack2 = await make_stack(script=[])
    with pytest.raises(Exception):
        await coverage_check_node(
            stack2.ctx, {"clauses": [c.model_dump() for c in CLAUSES]}
        )
    # 缺 case_generate artifact
    stack3 = await make_stack(script=[])
    with pytest.raises(Exception):
        await coverage_check_node(stack3.ctx, _state())
