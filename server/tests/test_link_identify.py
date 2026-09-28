"""WP-17 link_identify 节点（dd §7.5② §7.4① §2.3 §20.3 §8.6）。

验收口径（WBS #17）：
- "FakeLLM 脚本驱动产物断言"：index_line 档检索 → LLM LinkPlan →
  artifact(active, v1, confirmed_by=null) 落库；
- "幻觉 entry 降 hit=false"：模型对不在注入白名单的 entry_id 断言 hit →
  服务端改写为 new-link/new-story、记 trace.degraded、hallucinated_ids 回填。

另覆盖：临时 ID 确定性改写与归属映射、镜像权威 ID 覆盖模型值、entry_version
回填、link.story_ids 聚合、悬空 link 引用/坏结构 LLM_BAD_OUTPUT、澄清
interrupt/answer 二次生成、零注入全新增、崩溃重放零 LLM 重复用产物、
daos 缺失报错、知识块 [ID]/归属渲染。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.adapters.reme import Entry, IndexMirror
from tester_agent.domain import EntryType, InjectedItem
from tester_agent.errors import AppError, LLMBadOutput
from tester_agent.graph.constants import STAGE_LINK_IDENTIFY
from tester_agent.graph.nodes import (
    build_link_intent,
    finalize_link_plan,
    intake_node,
    link_identify_node,
    render_knowledge_block,
)
from tester_agent.graph.state import TaskState
from tester_agent.graph.wrap import wrap
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

# 条款 anchor 含空格分词的业务词：FakeReMeReader 以空白分词做词面打分
MD = (
    "# 订单与退款需求\n"
    "\n"
    "## 订单链路\n"
    "\n"
    "订单 下单 支付 主链路 正向 履约。\n"
    "\n"
    "### 支付故事\n"
    "\n"
    "支付 收银台 回调 关单。\n"
    "\n"
    "## 退款链路\n"
    "\n"
    "退款 售后 逆向 链路 退货。\n"
)

CLAUSE_IDS = ["h2-1", "h2-1-h3-1", "h2-2"]


def _entry(
    eid: str, *, title: str, content: str, link_id: str, story_id: str | None,
    summary: str,
) -> Entry:
    return Entry(
        entry_id=eid,
        entry_version=f"v-{eid}",
        title=title,
        content=content,
        entry_type=EntryType.LINK_INDEX,
        link_id=link_id,
        story_id=story_id,
        updated_at="2026-09-20T00:00:00Z",
        raw={"summary": summary},
    )


ENTRIES = [
    _entry("l1", title="订单链路", content="订单 创建 支付 履约 主链路 正向",
           link_id="L1", story_id=None, summary="订单链路：下单到履约的正向主链路"),
    _entry("l2", title="退款链路", content="退款 售后 逆向 链路 退货",
           link_id="L2", story_id=None, summary="退款链路：售后逆向流程"),
    _entry("s1", title="下单故事", content="订单 创建 购物车 结算 提交",
           link_id="L1", story_id="S1", summary="下单故事：购物车结算到订单创建"),
    _entry("s2", title="支付故事", content="支付 收银台 回调 关单",
           link_id="L1", story_id="S2", summary="支付故事：收银台与支付回调"),
]

RERANK_SCORES = {"l1": 100, "s1": 85, "s2": 80, "l2": 70}


def _mq() -> str:
    return json.dumps(
        {"keyword_queries": ["订单 支付 链路"], "rewrite_queries": ["下单 退款 流程"]},
        ensure_ascii=False,
    )


def _rr(scores: dict | None = None) -> str:
    scores = RERANK_SCORES if scores is None else scores
    return json.dumps(
        {"scores": [{"entry_id": k, "score": v, "reason": "fixture"}
                    for k, v in scores.items()]},
        ensure_ascii=False,
    )


# 正常 LinkPlan：2 hit link + 1 new link；2 hit story + 1 new story（归属新 link）
PLAN_JSON = json.dumps(
    {
        "links": [
            {"link_id": "L1", "title": "订单链路", "summary": "下单到履约正向主链路",
             "hit": True, "entry_id": "l1", "confidence": 0.92},
            {"link_id": "L2", "title": "退款链路", "summary": "售后逆向流程",
             "hit": True, "entry_id": "l2", "confidence": 0.85},
            {"link_id": "tmp-invoice", "title": "发票链路", "summary": "订单完成后开票",
             "hit": False, "entry_id": None, "confidence": 0.0},
        ],
        "stories": [
            {"story_id": "S1", "link_id": "L1", "title": "下单故事",
             "summary": "购物车结算下单", "hit": True, "entry_id": "s1",
             "confidence": 0.8, "rationale": "下单是支付前置",
             "related_clause_ids": ["h2-1"]},
            {"story_id": "S2", "link_id": "L1", "title": "支付故事",
             "summary": "收银台与回调", "hit": True, "entry_id": "s2",
             "confidence": 0.88, "rationale": "需求描述支付回调",
             "related_clause_ids": ["h2-1-h3-1"]},
            {"story_id": "tmp-story-inv", "link_id": "tmp-invoice",
             "title": "开票故事", "summary": "支付完成后申请开票", "hit": False,
             "entry_id": None, "confidence": 0.0,
             "rationale": "需求新增发票诉求", "related_clause_ids": ["h2-1"]},
        ],
        "new_suggestions": [
            {"suggested_link_title": "发票链路", "suggested_story_title": "开票故事",
             "reason": "需求新增开票能力", "source_clause_ids": ["h2-1"]}
        ],
        "clarifications": [],
    },
    ensure_ascii=False,
)

CLARIF_JSON = json.dumps(
    {"links": [], "stories": [], "new_suggestions": [],
     "clarifications": [
         {"question": "发票是普票还是专票？", "options": ["普票", "专票"]}]},
    ensure_ascii=False,
)

ANSWERS = [{"id": "q-1", "answer": "仅电子普票"}]


# ---------- 纯函数 ----------


def test_build_link_intent_title_path_plus_anchor():
    intent = build_link_intent(
        [
            {"clause_id": "h2-1", "title_path": ["订单链路"],
             "anchor": "订单 下单 支付 主链路"},
            {"clause_id": "root", "title_path": [], "anchor": "无标题摘要"},
        ]
    )
    assert intent == "订单链路：订单 下单 支付 主链路\n无标题摘要"


async def test_render_knowledge_block_attribution():
    reader = FakeReMeReader(ENTRIES)
    mirror = IndexMirror(reader)
    await mirror.ensure_fresh()
    items = [
        InjectedItem(entry_id="l1", entry_version="v-l1", title="订单链路",
                     tokens_est=10, position=0, passage="订单链路\n摘要L1"),
        InjectedItem(entry_id="s2", entry_version="v-s2", title="支付故事",
                     tokens_est=8, position=1, passage="支付故事\n摘要S2"),
    ]
    block = render_knowledge_block(items, mirror)
    assert block.startswith("<knowledge>") and block.rstrip().endswith("</knowledge>")
    line_l1 = block.split("\n")[1]
    assert line_l1.startswith("[ID:l1]") and "链路：L1" in line_l1
    assert "故事ID" not in line_l1
    line_s2 = [ln for ln in block.split("\n") if ln.startswith("[ID:s2]")][0]
    assert "故事：L1" in line_s2 and "故事ID：S2" in line_s2

    # 镜像为空：仅 [ID] 标签，无归属（§8.4 降级路径不报错）
    plain = render_knowledge_block(items, None)
    assert plain.split("\n")[1] == "[ID:l1]"


def test_finalize_hits_new_ids_versions_and_story_aggregation():
    plan, degraded, asserted = finalize_link_plan(
        json.loads(PLAN_JSON),
        whitelist={"l1", "l2", "s1", "s2"},
        entry_versions={"l1": "v-l1", "l2": "v-l2", "s1": "v-s1", "s2": "v-s2"},
    )
    assert [l.link_id for l in plan.links] == ["L1", "L2", "new-link-1"]
    assert [l.hit for l in plan.links] == [True, True, False]
    assert plan.links[0].entry_id == "l1" and plan.links[0].entry_version == "v-l1"
    assert plan.links[2].entry_id is None and plan.links[2].entry_version is None
    assert [s.story_id for s in plan.stories] == ["S1", "S2", "new-story-1"]
    # 新故事归属经模型临时 link_id 改写到服务端 new-link-1
    assert plan.stories[2].link_id == "new-link-1"
    assert plan.stories[0].entry_version == "v-s1"
    # link.story_ids 聚合（按故事输出序）
    assert plan.links[0].story_ids == ["S1", "S2"]
    assert plan.links[2].story_ids == ["new-story-1"]
    assert plan.links[1].story_ids == []
    assert len(plan.new_suggestions) == 1
    assert degraded == []
    assert set(asserted) == {"l1", "l2", "s1", "s2"}


async def test_finalize_mirror_authoritative_id_overrides_model():
    reader = FakeReMeReader(ENTRIES)
    mirror = IndexMirror(reader)
    await mirror.ensure_fresh()
    obj = {
        "links": [
            {"link_id": "模型瞎写的LX", "title": "订单链路", "summary": "x",
             "hit": True, "entry_id": "l1", "confidence": 0.9}
        ],
        "stories": [
            {"story_id": "模型瞎写的S9", "link_id": "模型瞎写的LX", "title": "支付故事",
             "summary": "x", "hit": True, "entry_id": "s2", "confidence": 0.8,
             "rationale": "r", "related_clause_ids": []}
        ],
        "new_suggestions": [],
    }
    plan, _, _ = finalize_link_plan(
        obj, whitelist={"l1", "s2"},
        entry_versions={"l1": "v-l1", "s2": "v-s2"}, mirror=mirror,
    )
    # 镜像权威：最终 ID 取索引树归属；故事 link_id 经映射改写
    assert plan.links[0].link_id == "L1"
    assert plan.stories[0].story_id == "S2"
    assert plan.stories[0].link_id == "L1"
    assert plan.links[0].story_ids == ["S2"]


def test_finalize_hallucinated_link_and_story_downgraded():
    obj = {
        "links": [
            {"link_id": "L2", "title": "幽灵链路", "summary": "模型幻觉",
             "hit": True, "entry_id": "ghost-link", "confidence": 0.8},
            {"link_id": "tmp-x", "title": "全新链路", "summary": "真新增",
             "hit": False, "entry_id": None, "confidence": 0.0},
        ],
        "stories": [
            {"story_id": "G1", "link_id": "L2", "title": "幽灵故事", "summary": "x",
             "hit": True, "entry_id": "ghost-story", "confidence": 0.7,
             "rationale": "r", "related_clause_ids": []},
            {"story_id": "tmp-s", "link_id": "tmp-x", "title": "新故事", "summary": "x",
             "hit": False, "entry_id": None, "confidence": 0.0,
             "rationale": "r", "related_clause_ids": []},
        ],
        "new_suggestions": [],
    }
    plan, degraded, asserted = finalize_link_plan(
        obj, whitelist=set(), entry_versions={}
    )
    # 白名单为空：两条"命中"全部降级，编号在 links/stories 各自序列内连续
    assert [l.link_id for l in plan.links] == ["new-link-1", "new-link-2"]
    assert all(not l.hit and l.entry_id is None for l in plan.links)
    assert [s.story_id for s in plan.stories] == ["new-story-1", "new-story-2"]
    assert plan.stories[0].link_id == "new-link-1"
    reasons = [d.reason for d in degraded]
    assert "entry_not_injected:ghost-link" in reasons
    assert "entry_not_injected:ghost-story" in reasons
    assert all(d.step == "link_identify.entry_whitelist"
               and d.fallback == "downgrade_hit_to_new" for d in degraded)
    assert set(asserted) == {"ghost-link", "ghost-story"}


def test_finalize_dangling_story_link_and_bad_structure_raise():
    base = {
        "links": [
            {"link_id": "L1", "title": "订单链路", "summary": "x", "hit": True,
             "entry_id": "l1", "confidence": 0.9}
        ],
        "stories": [
            {"story_id": "S2", "link_id": "不存在的L9", "title": "支付故事",
             "summary": "x", "hit": True, "entry_id": "s2", "confidence": 0.8,
             "rationale": "r", "related_clause_ids": []}
        ],
        "new_suggestions": [],
    }
    with pytest.raises(LLMBadOutput, match="link_id"):
        finalize_link_plan(base, whitelist={"l1", "s2"}, entry_versions={})

    with pytest.raises(LLMBadOutput):
        finalize_link_plan({"stories": [], "new_suggestions": []},
                           whitelist=set(), entry_versions={})

    bad_conf = dict(base)
    bad_conf["stories"] = [
        {"story_id": "S2", "link_id": "L1", "title": "支付故事", "summary": "x",
         "hit": True, "entry_id": "s2", "confidence": "高",
         "rationale": "r", "related_clause_ids": []}
    ]
    with pytest.raises(LLMBadOutput, match="confidence"):
        finalize_link_plan(bad_conf, whitelist={"l1", "s2"}, entry_versions={})

    bad_title = dict(base)
    bad_title["links"][0]["title"] = "  "
    with pytest.raises(LLMBadOutput, match="title"):
        finalize_link_plan(bad_title, whitelist={"l1", "s2"}, entry_versions={})


# ---------- 图级 / 节点级测试 ----------


@dataclass
class Stack:
    ctx: TaskContext
    db: Database
    store: FileStore
    llm: FakeLLM
    reader: FakeReMeReader
    saver: AsyncSqliteSaver
    conn: aiosqlite.Connection


def _graph(stack: Stack):
    """单测专用线性图：intake → link_identify（生产拓扑为控制环）。"""
    g = StateGraph(TaskState)
    g.add_node("intake", wrap(intake_node, name="intake"))
    g.add_node(STAGE_LINK_IDENTIFY, wrap(link_identify_node, name=STAGE_LINK_IDENTIFY))
    g.add_edge(START, "intake")
    g.add_edge("intake", STAGE_LINK_IDENTIFY)
    g.add_edge(STAGE_LINK_IDENTIFY, END)
    return g.compile(checkpointer=stack.saver)


def _cfg(stack: Stack, thread: str = "th-1") -> dict:
    return {"configurable": {"thread_id": thread, "ctx": stack.ctx}}


@pytest.fixture()
async def make_stack(tmp_path):
    handles: list[tuple[Database, aiosqlite.Connection]] = []

    async def build(
        *,
        script: list,
        entries: list[Entry] | None = None,
        agent_config: dict | None = None,
        snapshot_level: str = "off",
    ) -> Stack:
        db_path = tmp_path / f"app-{len(handles)}.db"
        run_migrations(db_path)
        db = Database(db_path)

        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=WS, name="ws-name",
                                kb_config={"kb_id": "KB1", "mode": "sdk"})
        )
        await db.aexecute(
            "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (CONV, WS, "t", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
        )
        await TaskDAO(db).create(
            TaskRow.create(
                id=TASK, conversation_id=CONV, workspace_id=WS, status="running",
                current_stage="intake", langgraph_thread_id="thread-1",
                graph_run_id=RUN, snapshot_level=snapshot_level,
            )
        )
        store = FileStore(tmp_path / "data")
        await store.save_requirement(WS, TASK, MD)
        llm = FakeLLM(script)
        reader = FakeReMeReader(entries if entries is not None else ENTRIES)
        mirror = IndexMirror(reader)
        app = AppContext(
            db=db, file_store=store, llm=llm, reme_factory=None,
            config=ConfigDAO(db),
        )
        ctx = TaskContext(
            app=app, task=await TaskDAO(db).get(TASK), run_id=RUN,
            files=store, reader=reader, snapshot_level=snapshot_level,
            mirror=mirror,
            agent_config=agent_config if agent_config is not None
            else {"ambiguity_check": False},
            daos=DAOs(
                task=TaskDAO(db), message=MessageDAO(db),
                artifact=ArtifactDAO(db),
            ),
        )
        conn = await aiosqlite.connect(
            str(tmp_path / f"ckpt-{len(handles)}.db"), check_same_thread=False
        )
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        handles.append((db, conn))
        return Stack(ctx=ctx, db=db, store=store, llm=llm, reader=reader,
                     saver=saver, conn=conn)

    yield build
    for db, conn in handles:
        db.close()
        await conn.close()


async def _active_artifact(db: Database) -> ArtifactRow:
    return await ArtifactDAO(db).get_active(TASK, STAGE_LINK_IDENTIFY)


async def _traces(db: Database) -> list:
    return await db.aquery(
        "SELECT stage, stage_version, injected_ids, referenced_ids, "
        "hallucinated_ids, weak_ref_ids, degraded FROM retrieval_trace "
        "WHERE task_id = ? ORDER BY created_at ASC, id ASC",
        (TASK,),
    )


def _main_call(llm: FakeLLM) -> dict:
    """link_identify 生成调用（messages 含 <knowledge> 的那次）。"""
    calls = [
        c for c in llm.calls
        if any("<knowledge>" in m["content"] for m in c["messages"])
    ]
    assert len(calls) == 1
    return calls[0]


async def test_happy_path_artifact_v1_and_trace_closed(make_stack):
    stack = await make_stack(script=[_mq(), _rr(), PLAN_JSON])
    graph = _graph(stack)
    result = await graph.ainvoke({}, _cfg(stack))

    state = await graph.aget_state(_cfg(stack))
    assert state.next == ()  # intake + link_identify 完成
    plan = result["link_plan"]
    assert [l["link_id"] for l in plan["links"]] == ["L1", "L2", "new-link-1"]
    assert [s["story_id"] for s in plan["stories"]] == ["S1", "S2", "new-story-1"]
    assert result["current_stage_version"] == {STAGE_LINK_IDENTIFY: 1}

    # artifact：active / v1 / system / confirmed_by=null（dd §7.5② 3）
    art = await _active_artifact(stack.db)
    assert art.stage_version == 1 and art.status == "active"
    assert art.origin == "system" and art.confirmed_by is None
    assert art.graph_run_id == RUN
    payload = art.payload_dict()
    assert payload["stories"][2]["link_id"] == "new-link-1"
    assert payload["links"][0]["story_ids"] == ["S1", "S2"]

    # trace：注入 4 条全被引用、无幻觉；stage_version 与 artifact 对齐
    (trace,) = await _traces(stack.db)
    assert trace["stage"] == STAGE_LINK_IDENTIFY and trace["stage_version"] == 1
    injected = json.loads(trace["injected_ids"])
    assert set(injected) == {"l1", "l2", "s1", "s2"}
    assert set(json.loads(trace["referenced_ids"])) == {"l1", "l2", "s1", "s2"}
    assert json.loads(trace["hallucinated_ids"]) == []

    # 知识块：标签 + 镜像归属；user 消息三件套；system 拼共享铁律
    call = _main_call(stack.llm)
    msgs = call["messages"]
    assert "[ID:l1]" in msgs[1]["content"] and "链路：L1" in msgs[1]["content"]
    assert "<requirement_clauses>" in msgs[1]["content"]
    assert "<requirement_summary>" in msgs[1]["content"]
    assert "订单与退款需求" in msgs[1]["content"]
    assert "铁律" in msgs[0]["content"] and "new_suggestions" in msgs[0]["content"]
    assert call["json_schema"] is not None
    # 意图经 raw 路送入检索（条款标题路径 + anchor）
    assert any("订单链路" in q and "退款" in q for q in stack.reader.search_queries)
    # intake 歧义检测关闭：仅 multi_query + rerank + 主生成三次调用
    assert stack.llm.chat_calls == 3


async def test_hallucinated_entry_downgraded_hit_false(make_stack):
    """WBS 验收：幻觉 entry 降 hit=false + degraded + hallucinated_ids 回填。"""
    ghost_plan = json.dumps(
        {
            "links": [
                {"link_id": "L1", "title": "订单链路", "summary": "真命中",
                 "hit": True, "entry_id": "l1", "confidence": 0.9},
                {"link_id": "L99", "title": "幽灵链路", "summary": "不存在的条目",
                 "hit": True, "entry_id": "ent_ghost", "confidence": 0.8},
            ],
            "stories": [
                {"story_id": "S1", "link_id": "L1", "title": "下单故事",
                 "summary": "x", "hit": True, "entry_id": "s1", "confidence": 0.7,
                 "rationale": "r", "related_clause_ids": ["h2-1"]},
            ],
            "new_suggestions": [],
            "clarifications": [],
        },
        ensure_ascii=False,
    )
    stack = await make_stack(script=[_mq(), _rr(), ghost_plan])
    graph = _graph(stack)
    await graph.ainvoke({}, _cfg(stack))

    art = await _active_artifact(stack.db)
    links = art.payload_dict()["links"]
    ghost = next(l for l in links if l["title"] == "幽灵链路")
    assert ghost["hit"] is False and ghost["entry_id"] is None
    assert ghost["link_id"] == "new-link-1"  # 新增序列独立编号：首个降级项即 new-link-1
    ok = next(l for l in links if l["title"] == "订单链路")
    assert ok["hit"] is True and ok["link_id"] == "L1"

    (trace,) = await _traces(stack.db)
    assert "ent_ghost" in json.loads(trace["hallucinated_ids"])
    assert "l1" in json.loads(trace["referenced_ids"])
    degraded = json.loads(trace["degraded"])
    assert any(
        d["step"] == "link_identify.entry_whitelist"
        and d["reason"] == "entry_not_injected:ent_ghost"
        and d["fallback"] == "downgrade_hit_to_new"
        for d in degraded
    )


async def test_clarification_interrupt_then_resume_generates(make_stack):
    # 第一次管线 mq+rr 后出澄清；恢复轮重跑管线（rerank 缓存命中零调用），
    # multi_query 仍调用一次 + 二次主生成
    stack = await make_stack(script=[_mq(), _rr(), CLARIF_JSON, _mq(), PLAN_JSON])
    graph = _graph(stack)
    cfg = _cfg(stack)
    await graph.ainvoke({}, cfg)

    state = await graph.aget_state(cfg)
    assert state.next == (STAGE_LINK_IDENTIFY,)  # 函数式中断挂在本节点
    payload = state.tasks[0].interrupts[0].value
    assert payload["node"] == STAGE_LINK_IDENTIFY
    assert payload["questions"][0]["id"] == "q-1"
    msgs = await MessageDAO(stack.db).list_by_task(TASK, kind="clarification_qa")
    assert len(msgs) == 1
    mp = json.loads(msgs[0].payload)
    assert mp["node"] == STAGE_LINK_IDENTIFY and mp["answered"] is False
    # 澄清期间绝不落半成品产物
    assert await _active_artifact(stack.db) is None

    await graph.ainvoke(Command(resume=ANSWERS), cfg)
    state = await graph.aget_state(cfg)
    assert state.next == ()
    art = await _active_artifact(stack.db)
    assert art is not None and art.payload_dict()["links"]
    mp2 = json.loads(
        (await MessageDAO(stack.db).list_by_task(TASK, kind="clarification_qa"))[0].payload
    )
    assert mp2["answered"] is True and mp2["answers"] == ANSWERS
    # mq1 rr1 clarif mq2 plan = 5 次（rerank 第二次缓存命中，不调 LLM）
    assert stack.llm.chat_calls == 5
    # 两轮管线各留一行 trace
    assert len(await _traces(stack.db)) == 2


async def test_bad_llm_output_fails_node_without_artifact(make_stack):
    stack = await make_stack(script=[_mq(), _rr(), "不是 JSON"])
    graph = _graph(stack)
    with pytest.raises(AppError) as ei:
        await graph.ainvoke({}, _cfg(stack))
    assert ei.value.code == "LLM_BAD_OUTPUT"
    state = await graph.aget_state(_cfg(stack))
    assert state.next == (STAGE_LINK_IDENTIFY,)
    assert await _active_artifact(stack.db) is None

    # 结构合法但缺 links 同样失败
    stack2 = await make_stack(script=[_mq(), _rr(), json.dumps({"stories": []})])
    g2 = _graph(stack2)
    with pytest.raises(LLMBadOutput):
        await g2.ainvoke({}, _cfg(stack2, thread="th-2"))
    assert await ArtifactDAO(stack2.db).get_active(TASK, STAGE_LINK_IDENTIFY) is None


async def test_zero_injection_all_new_plan(make_stack):
    """知识库无召回：rerank 零候选不调 LLM；任何 hit 断言都降级为新增。"""
    plan = json.dumps(
        {
            "links": [
                {"link_id": "x", "title": "全新链路", "summary": "无对应知识",
                 "hit": True, "entry_id": "l1", "confidence": 0.9}
            ],
            "stories": [],
            "new_suggestions": [],
            "clarifications": [],
        },
        ensure_ascii=False,
    )
    # mq 变体无词面命中，且库内条目与 raw 意图（订单/退款词面）零重叠
    # → 零候选 → rerank 无调用，脚本仅 [mq, plan]
    mq = json.dumps({"keyword_queries": ["无关词汇"], "rewrite_queries": ["其他内容"]})
    unrelated = [_entry("z1", title="库存盘点", content="库存 盘点 仓库 调拨",
                        link_id="Z9", story_id=None, summary="库存盘点与调拨")]
    stack = await make_stack(script=[mq, plan], entries=unrelated)
    graph = _graph(stack)
    await graph.ainvoke({}, _cfg(stack))
    art = await _active_artifact(stack.db)
    (link,) = art.payload_dict()["links"]
    assert link["hit"] is False and link["link_id"] == "new-link-1"
    (trace,) = await _traces(stack.db)
    assert json.loads(trace["injected_ids"]) == []
    assert "l1" in json.loads(trace["hallucinated_ids"])


async def test_replay_same_run_reuses_artifact_zero_llm(make_stack):
    stack = await make_stack(script=[])  # 任何 LLM 调用都会抛错
    seeded = ArtifactRow.create(
        id="art-seeded", task_id=TASK, stage=STAGE_LINK_IDENTIFY,
        graph_run_id=RUN, stage_version=1,
        payload={"links": [], "stories": [], "new_suggestions": []},
    )
    await ArtifactDAO(stack.db).put(seeded)

    out = await link_identify_node(
        stack.ctx, {"clauses": [{"clause_id": cid, "title_path": [], "anchor": "x"}
                                for cid in CLAUSE_IDS]}
    )
    assert out["link_plan"] == {"links": [], "stories": [], "new_suggestions": []}
    assert out["current_stage_version"] == {STAGE_LINK_IDENTIFY: 1}
    assert stack.llm.chat_calls == 0
    # 未造第二版本
    assert (await ArtifactDAO(stack.db).next_version(TASK, STAGE_LINK_IDENTIFY)) == 2


async def test_missing_artifact_dao_raises(make_stack):
    stack = await make_stack(script=[])
    stack.ctx.daos.artifact = None
    with pytest.raises(AppError, match="artifact"):
        await link_identify_node(stack.ctx, {"clauses": [{"clause_id": "root",
                                                           "title_path": [],
                                                           "anchor": "x"}]})


async def test_missing_clauses_raises(make_stack):
    stack = await make_stack(script=[])
    with pytest.raises(AppError, match="clauses"):
        await link_identify_node(stack.ctx, {})
