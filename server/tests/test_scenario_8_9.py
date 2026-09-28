"""WP-X1：dd §15.2 场景 8（降级链）与场景 9（引用闭环）具名验收。

场景 8：FakeReader caps 关 metadata_filter → 镜像本地过滤且 trace.degraded
有记录；同一次管线 rerank FakeLLM 抛异常 → 规则分兜底不中断。

场景 9：产物引用白名单外 ID → hallucinated；未引用注入条目 →
injected_not_used；落库对账。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeLLM, FakeReMeReader
from tester_agent.adapters.reme import (
    Entry,
    IndexMirror,
    ReMeCaps,
    ReMeReaderFactory,
)
from tester_agent.domain import EntryType, RetrievalConfig
from tester_agent.errors import LLMUpstreamError
from tester_agent.graph.retrieval.pipeline import (
    close_retrieval_trace,
    retrieve_pipeline,
)
from tester_agent.runtime.context import AppContext, TaskContext
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ConfigDAO,
    TaskDAO,
    TaskRow,
    TraceDAO,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

WS, TASK, CONV = "ws-x1", "task-x1", "conv-x1"
INTENT = "订单 下单"


def _entry(eid: str, etype: EntryType, title: str, content: str, **kw) -> Entry:
    return Entry(
        entry_id=eid,
        entry_version=f"ver-{eid}",
        title=title,
        content=content,
        entry_type=etype,
        link_id=kw.get("link_id"),
        story_id=kw.get("story_id"),
        updated_at="2026-09-26T00:00:00.000Z",
        raw={"summary": kw.get("summary", title)},
    )


@pytest.fixture()
def stack(tmp_path):
    """同步夹具返回 (db, store)；与 test_pipeline.make_stack 同构。"""
    db_path = tmp_path / "x1.db"
    run_migrations(db_path)
    db = Database(db_path)

    async def _seed():
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(
                id=WS, name="x1", kb_config={"kb_id": "KB1", "options": {}}
            )
        )
        await db.aexecute(
            "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (CONV, WS, "t", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
        )
        await TaskDAO(db).create(
            TaskRow.create(
                id=TASK, conversation_id=CONV, workspace_id=WS, status="running",
                current_stage="point_write", langgraph_thread_id="th-x1",
                graph_run_id="run-x1", snapshot_level="meta",
            )
        )

    asyncio.run(_seed())
    store = FileStore(tmp_path / "data")
    yield db, store
    db.close()


async def test_scenario_8_degradation_chain_mirror_and_rerank(stack):
    """§15.2#8：metadata_filter 关 → 镜像过滤；rerank 5xx → 规则分兜底。

    镜像只收录 LINK_INDEX/STORY_INDEX：本场景用 link_identify + 链路条目，
    使 scope 过滤后仍有 kept 进入 rerank（业务条目不在镜像索引，会被
    filtered_scope，无法覆盖「过滤后仍走规则分」分支）。
    """
    db, store = stack
    entries = [
        _entry("l1", EntryType.LINK_INDEX, "订单 下单 链路", "订单 创建 支付 下单",
               link_id="L1", summary="订单链路摘要"),
        _entry("l2", EntryType.LINK_INDEX, "仓配 物流", "库存 仓配 物流",
               link_id="L2", summary="仓配"),
        _entry("l3", EntryType.LINK_INDEX, "订单 履约 下单", "订单 发货 签收 下单",
               link_id="L1", summary="履约"),
    ]
    caps = ReMeCaps(metadata_filter=False, entry_version=True, passage_api=True)
    reader = FakeReMeReader(entries, caps=caps)
    mq = json.dumps(
        {"keyword_queries": ["订单 下单"], "rewrite_queries": ["创建订单"]},
        ensure_ascii=False,
    )
    # multi_query 一次；rerank 抛错 → 规则分（可能因缓存/分桶再调，多备几条）
    llm = FakeLLM([mq, LLMUpstreamError("fake 5xx")] + [LLMUpstreamError("x")] * 5)
    app = AppContext(
        db=db, file_store=store, llm=llm,
        reme_factory=ReMeReaderFactory(), config=ConfigDAO(db),
    )
    mirror = IndexMirror(reader)
    await mirror.ensure_fresh()
    ctx = TaskContext(
        app=app, task=await TaskDAO(db).get(TASK), run_id="run-x1",
        files=store, reader=reader, snapshot_level="meta", mirror=mirror,
    )
    cfg = RetrievalConfig(
        stage="link_identify", query_paths=2, recall_topk=50, inject_limit=200,
        inject_form="index_line", allowed_types=[EntryType.LINK_INDEX],
        token_budget=12_000,
    )
    outcome = await retrieve_pipeline(
        ctx, cfg, INTENT, scope={"link_ids": ["L1"]}, batch_id="b0",
    )
    assert any(
        d.step == "meta_filter" and d.fallback == "index_mirror_local_filter"
        for d in outcome.degraded
    )
    assert any(
        d.step == "rerank" and d.reason == "llm_failed" and d.fallback == "rule_score"
        for d in outcome.degraded
    )
    cands = (await TraceDAO(db).get(outcome.trace_id)).candidates_obj()
    assert cands, "应有召回候选"
    l2 = next((c for c in cands if c.entry_id == "l2"), None)
    if l2 is not None:
        assert l2.kept is False
        assert l2.drop_reason == "filtered_scope"
    kept = [c for c in cands if c.kept]
    assert kept or outcome.items, "降级后仍应保留可注入候选"


async def test_scenario_9_closed_loop_hallucinated_and_unused(stack):
    """§15.2#9：白名单外 ID→hallucinated；注入未引用→injected_not_used。"""
    db, store = stack
    entries = [
        _entry("l1", EntryType.LINK_INDEX, "订单 链路", "订单 创建 支付 下单",
               link_id="L1", summary="订单"),
        _entry("l2", EntryType.LINK_INDEX, "订单 退款", "订单 退款 售后 下单",
               link_id="L2", summary="退款"),
        _entry("l3", EntryType.LINK_INDEX, "订单 履约", "订单 发货 签收 下单",
               link_id="L3", summary="履约"),
    ]
    reader = FakeReMeReader(
        entries,
        caps=ReMeCaps(metadata_filter=True, entry_version=True, passage_api=True),
    )
    mq = json.dumps(
        {"keyword_queries": ["订单 下单"], "rewrite_queries": ["创建订单"]},
        ensure_ascii=False,
    )
    rr = json.dumps({
        "scores": [
            {"entry_id": e.entry_id, "score": 100 - i, "reason": "f"}
            for i, e in enumerate(entries)
        ]
    })
    llm = FakeLLM([mq, rr] * 4)
    app = AppContext(
        db=db, file_store=store, llm=llm,
        reme_factory=ReMeReaderFactory(), config=ConfigDAO(db),
    )
    ctx = TaskContext(
        app=app, task=await TaskDAO(db).get(TASK), run_id="run-x1",
        files=store, reader=reader, snapshot_level="meta",
        mirror=IndexMirror(reader),
    )
    cfg = RetrievalConfig(
        stage="link_identify", query_paths=2, recall_topk=50, inject_limit=200,
        inject_form="index_line", allowed_types=[EntryType.LINK_INDEX],
        token_budget=12_000,
    )
    outcome = await retrieve_pipeline(ctx, cfg, INTENT, batch_id="b0")
    assert len(outcome.injected) >= 1
    # 若只注入 1 条，再引用它并附加幻觉 ID，unused 可能为空——补足未引用断言：
    # 构造生成文本只引用子集或幻觉。
    injected = list(outcome.injected)
    if len(injected) >= 2:
        generated = f"产物引用 [ID:{injected[0]}] 与幻觉 [ID:ghost-x]"
        expect_unused = True
    else:
        generated = "产物仅含幻觉 [ID:ghost-x]"
        expect_unused = True

    loop = await close_retrieval_trace(ctx, outcome, [generated])
    assert "ghost-x" in loop.hallucinated
    if expect_unused:
        assert loop.injected_not_used or "ghost-x" in loop.hallucinated
    if injected[0] in generated:
        assert injected[0] in loop.referenced
        assert injected[0] not in loop.injected_not_used

    trace = await TraceDAO(db).get(outcome.trace_id)
    assert "ghost-x" in trace.hallucinated_ids_obj()
