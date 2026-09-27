"""WP-12 验收：retrieve_pipeline 编排 + trace 两写 + snapshot 三档 + 引用闭环。

覆盖验收口径：
- "full 档按偏移读单行"：逐行 read_snapshot_line 还原 JSONL（含中文 UTF-8
  字节长度）、偏移连续、char_offset/byte_length 回填、DB items 无正文；
- "三档行为表测试"：off/meta/full 三档的 DB 行/文件/items 差异参数化断言。

另覆盖：caps 三态降级（镜像本地过滤/镜像空跳过/本地版本/本地切分）、
run 分区缓存语义（同 run recall/rerank 命中、异 run 清空）、截断端到端、
close_loop（hallucinated/referenced/weak）、funnel_counts、档位校验。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.adapters.reme import (
    Entry,
    IndexMirror,
    ReMeCaps,
    ReMeReaderFactory,
)
from tester_agent.domain import Candidate, EntryType, RetrievalConfig
from tester_agent.errors import ValidationError
from tester_agent.graph.retrieval.pipeline import (
    ClosedLoop,
    close_loop,
    close_retrieval_trace,
    funnel_counts,
    parse_ids,
    retrieve_pipeline,
)
from tester_agent.runtime.context import AppContext, TaskContext
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ConfigDAO,
    SnapshotDAO,
    TaskDAO,
    TaskRow,
    TraceDAO,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

from fakes import FakeLLM, FakeReMeReader

WS = "ws1"
TASK = "task1"
CONV = "conv1"
INTENT = "订单 链路"


# ---------- 辅助构造 ----------


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
        raw={"summary": kw.get("summary", "")},
    )


def _link_entries() -> list[Entry]:
    return [
        _entry("l1", EntryType.LINK_INDEX, "订单链路", "订单 创建 支付 内容",
               link_id="L1", summary="订单链路一句话摘要"),
        _entry("l2", EntryType.LINK_INDEX, "退款链路", "订单 退款 售后 内容",
               link_id="L2", summary="退款链路一句话摘要"),
        _entry("l3", EntryType.LINK_INDEX, "履约链路", "订单 发货 签收 内容",
               link_id="L3", summary="履约链路一句话摘要"),
    ]


def _link_cfg(**kw) -> RetrievalConfig:
    base = dict(
        stage="link_identify", query_paths=3, recall_topk=50, inject_limit=200,
        inject_form="index_line", allowed_types=[EntryType.LINK_INDEX],
        token_budget=12_000,
    )
    base.update(kw)
    return RetrievalConfig(**base)


@dataclass
class Stack:
    ctx: TaskContext
    db: Database
    store: FileStore
    reader: FakeReMeReader
    llm: FakeLLM


@pytest.fixture()
def make_stack(tmp_path):
    handles: list[Database] = []

    async def build(
        *,
        entries: list[Entry] | None = None,
        caps: ReMeCaps | None = None,
        fail_tree: bool = False,
        fail_get: bool = False,
        level: str = "meta",
        run_id: str = "run-1",
    ) -> Stack:
        entries = _link_entries() if entries is None else entries
        caps = caps or ReMeCaps(metadata_filter=True, entry_version=True, passage_api=True)

        db_path = tmp_path / f"app-{len(handles)}.db"
        run_migrations(db_path)
        db = Database(db_path)
        handles.append(db)

        await WorkspaceDAO(db).create(
            WorkspaceRow.create(
                id=WS, name="ws-name",
                kb_config={"kb_id": "KB1", "mode": "sdk"},
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
                current_stage="link_identify", langgraph_thread_id="thread-1",
                graph_run_id=run_id, snapshot_level=level,
            )
        )

        store = FileStore(tmp_path / "data")
        reader = FakeReMeReader(
            entries, caps=caps, fail_tree=fail_tree or None, fail_get=fail_get or None
        )
        mirror = IndexMirror(reader)

        multi = json.dumps(
            {"keyword_queries": ["订单"], "rewrite_queries": ["下单"]}
        )
        rerank_resp = json.dumps(
            {"scores": [
                {"entry_id": e.entry_id, "score": 100 - i}
                for i, e in enumerate(entries)
            ]}
        )
        llm = FakeLLM([multi, rerank_resp] * 8)

        app = AppContext(
            db=db, file_store=store, llm=llm,
            reme_factory=ReMeReaderFactory(), config=ConfigDAO(db),
        )
        ctx = TaskContext(
            app=app, task=await TaskDAO(db).get(TASK), run_id=run_id,
            files=store, reader=reader, snapshot_level=level, mirror=mirror,
        )
        return Stack(ctx=ctx, db=db, store=store, reader=reader, llm=llm)

    yield build
    for db in handles:
        db.close()


async def _snapshot_count(db: Database) -> int:
    rows = await db.aquery(
        "SELECT COUNT(*) AS n FROM context_snapshot WHERE task_id = ?", (TASK,)
    )
    return rows[0]["n"]


async def _trace_count(db: Database) -> int:
    rows = await db.aquery(
        "SELECT COUNT(*) AS n FROM retrieval_trace WHERE task_id = ?", (TASK,)
    )
    return rows[0]["n"]


# ---------- ① 三档行为表（核心验收） ----------


@pytest.mark.parametrize("level", ["off", "meta", "full"])
async def test_snapshot_level_behavior_table(make_stack, level):
    stack = await make_stack(level=level)
    ctx = stack.ctx

    outcome = await retrieve_pipeline(ctx, _link_cfg(), INTENT, batch_id="b1")
    assert outcome.items, "三个 link 条目应全部注入"
    assert len(outcome.injected) == 3
    # 内存 items 保留 passage（节点生成需要）
    assert all(it.passage for it in outcome.items)

    # trace 恒写恰好一行
    assert await _trace_count(stack.db) == 1

    if level == "off":
        assert await _snapshot_count(stack.db) == 0
        assert outcome.snapshot_id is None
        return

    assert await _snapshot_count(stack.db) == 1
    snap = await SnapshotDAO(stack.db).get(outcome.snapshot_id)
    # DB items 恒无正文
    stored_items = snap.items_obj()
    assert len(stored_items) == 3
    assert all(it.passage is None for it in stored_items)
    assert snap.usage_obj()["aux"]["retrieval"]["calls"] == 2  # multi_query + rerank

    if level == "meta":
        assert snap.snapshot_path is None
        assert all(it.char_offset is None and it.byte_length is None for it in stored_items)
    else:
        assert snap.snapshot_path is not None
        assert snap.snapshot_path.endswith(".jsonl")
        assert snap.snapshot_path.startswith("snapshots/link_identify/v1/")
        assert all(it.char_offset is not None and it.byte_length is not None
                   for it in stored_items)


# ---------- ② full 档按偏移读单行（核心验收） ----------


async def test_full_read_single_line_by_offset(make_stack):
    stack = await make_stack(level="full")
    outcome = await retrieve_pipeline(stack.ctx, _link_cfg(), INTENT, batch_id="b1")

    snap = await SnapshotDAO(stack.db).get(outcome.snapshot_id)
    path = snap.snapshot_path

    prev_end = 0
    for pos, item in enumerate(outcome.items):
        # 按偏移读单行
        line = await stack.store.read_snapshot_line(
            WS, TASK, path, item.char_offset, item.byte_length
        )
        # 恰好一行：不含行尾换行，UTF-8 字节长度对账（含中文 3 字节/字）
        assert "\n" not in line
        assert len(line.encode("utf-8")) == item.byte_length
        obj = json.loads(line)
        assert obj["entry_id"] == item.entry_id
        assert obj["passage"] == item.passage  # 全文在文件
        # 偏移连续：首行 0；其后 off = 上一(off+len+1)
        if pos == 0:
            assert item.char_offset == 0
        else:
            assert item.char_offset == prev_end
        prev_end = item.char_offset + item.byte_length + 1

    # DB items 有偏移但无正文
    for db_item in snap.items_obj():
        assert db_item.passage is None
        assert db_item.char_offset is not None


async def test_full_snapshot_cjk_passage_roundtrip(make_stack):
    stack = await make_stack(level="full")
    outcome = await retrieve_pipeline(stack.ctx, _link_cfg(), INTENT, batch_id="b1")
    snap = await SnapshotDAO(stack.db).get(outcome.snapshot_id)

    item = outcome.items[0]
    line = await stack.store.read_snapshot_line(
        WS, TASK, snap.snapshot_path, item.char_offset, item.byte_length
    )
    obj = json.loads(line)
    assert "订单链路一句话摘要" in obj["passage"]


# ---------- ③ trace 两写回填 ----------


async def test_trace_two_phase_write(make_stack):
    stack = await make_stack(level="meta")
    outcome = await retrieve_pipeline(stack.ctx, _link_cfg(), INTENT, batch_id="b1")

    # 第一次写：append 拿 id，referenced 尚空，query_variant 各路文本（raw 在首）
    trace = await TraceDAO(stack.db).get(outcome.trace_id)
    assert trace.node == "link_identify"
    assert trace.stage_version == 1
    assert trace.batch_id == "b1"
    payload = json.loads(trace.query_variant)
    assert payload["queries"][0] == {"channel": "raw", "text": INTENT}
    assert trace.injected_ids_obj() == outcome.injected
    assert trace.referenced_ids_obj() == []
    assert trace.hallucinated_ids_obj() == []
    # candidates 最终全集：3 条全 kept
    cands = trace.candidates_obj()
    assert len(cands) == 3
    assert all(c.kept for c in cands)

    # 第二次写：生成结束回填
    generated = "用例正文引用 [ID:l1]，另一条 [ID:ghost] 是幻觉"
    loop = await close_retrieval_trace(stack.ctx, outcome, [generated])
    assert loop.model_dump() == ClosedLoop(
        referenced=["l1"],
        injected_not_used=["l2", "l3"],
        hallucinated=["ghost"],
        weak_refs=["l1"],  # 产物文本与 l1 passage 无 bigram 重叠 → weak
    ).model_dump()

    trace2 = await TraceDAO(stack.db).get(outcome.trace_id)
    assert trace2.referenced_ids_obj() == ["l1"]
    assert trace2.hallucinated_ids_obj() == ["ghost"]
    assert trace2.weak_ref_ids_obj() == ["l1"]


# ---------- ④ close_loop 纯函数 ----------


def test_parse_ids():
    assert parse_ids("参见 [ID:a1] 与 [ID: b2 ]，坏标签 [ID:]") == {"a1", "b2"}
    assert parse_ids("无标签文本") == set()


def test_close_loop_hallucinated_and_unused():
    texts = ["正文 [ID:e1]", "另一篇 [ID:e2] [ID:fake]"]
    loop = close_loop(texts, {"e1", "e2", "e3"})
    assert loop.referenced == ["e1", "e2"]
    assert loop.hallucinated == ["fake"]
    assert loop.injected_not_used == ["e3"]
    assert loop.weak_refs == []  # 无 passages 无法判定


def test_close_loop_strong_reference():
    passage = "登录成功后系统跳转至首页工作台"
    text = f"用例：{passage}，并标注 [ID:e1]"
    loop = close_loop([text], {"e1"}, passages={"e1": passage})
    assert "e1" not in loop.weak_refs  # 文本几乎复用 passage，Jaccard 高


def test_close_loop_weak_reference():
    passage = "登录成功后系统跳转至首页工作台"
    text = "完全无关的内容：火山鹦鹉麒麟貔貅，引用 [ID:e1]"
    loop = close_loop([text], {"e1"}, passages={"e1": passage})
    assert loop.weak_refs == ["e1"]


def test_close_loop_missing_passage_not_weak():
    text = "完全无关的内容，引用 [ID:e1]"
    loop = close_loop([text], {"e1"}, passages={})
    assert loop.weak_refs == []


def test_close_loop_empty_generation():
    loop = close_loop([], {"e1", "e2"})
    assert loop.referenced == []
    assert loop.hallucinated == []
    assert loop.injected_not_used == ["e1", "e2"]


# ---------- ⑤ caps 降级路径（dd §8.4） ----------


async def test_fallback_mirror_local_filter(make_stack):
    # metadata_filter=False：fake 忽略 scope；镜像非空 → 本地过滤。
    # 镜像树只从 LINK_INDEX 条目派生：放入 link 条目填充镜像（point 阶段会被
    # 类型过滤），b1 不在镜像索引 → filtered_scope。
    entries = _link_entries() + [
        _entry("b1", EntryType.BUSINESS, "业务规则", "订单 业务 内容"),
    ]
    cfg = RetrievalConfig(
        stage="point_write", query_paths=2, recall_topk=40, inject_limit=20,
        inject_form="passage", allowed_types=[EntryType.BUSINESS],
        token_budget=16_000,
    )
    stack = await make_stack(
        entries=entries,
        caps=ReMeCaps(metadata_filter=False, entry_version=True, passage_api=True),
    )
    outcome = await retrieve_pipeline(
        stack.ctx, cfg, INTENT,
        scope={"link_ids": ["L999"]},  # b1 不在镜像索引 → filtered_scope
    )
    cands = (await TraceDAO(stack.db).get(outcome.trace_id)).candidates_obj()
    b1 = next(c for c in cands if c.entry_id == "b1")
    assert b1.kept is False
    assert b1.drop_reason == "filtered_scope"
    reasons = {(d.step, d.fallback) for d in outcome.degraded}
    assert ("meta_filter", "index_mirror_local_filter") in reasons


async def test_fallback_skip_filter_when_mirror_empty(make_stack):
    # metadata_filter=False 且镜像空（list_index_tree 失败）→ 跳过归属过滤
    entries = [_entry("b1", EntryType.BUSINESS, "业务规则", "订单 业务 内容")]
    cfg = RetrievalConfig(
        stage="point_write", query_paths=2, recall_topk=40, inject_limit=20,
        inject_form="passage", allowed_types=[EntryType.BUSINESS],
        token_budget=16_000,
    )
    stack = await make_stack(
        entries=entries, fail_tree=True,
        caps=ReMeCaps(metadata_filter=False, entry_version=True, passage_api=True),
    )
    outcome = await retrieve_pipeline(
        stack.ctx, cfg, INTENT, scope={"link_ids": ["L999"]},
    )
    cands = (await TraceDAO(stack.db).get(outcome.trace_id)).candidates_obj()
    assert cands[0].kept is True  # 未做归属裁决
    assert cands[0].drop_reason is None
    reasons = {(d.step, d.fallback) for d in outcome.degraded}
    assert ("meta_filter", "skip_filter_all_to_rerank") in reasons


async def test_fallback_local_entry_version(make_stack):
    stack = await make_stack(
        caps=ReMeCaps(metadata_filter=True, entry_version=False, passage_api=True),
    )
    outcome = await retrieve_pipeline(stack.ctx, _link_cfg(), INTENT)
    cands = (await TraceDAO(stack.db).get(outcome.trace_id)).candidates_obj()
    assert all(c.entry_version.startswith("h-") for c in cands)
    assert any(d.step == "entry_version" for d in outcome.degraded)


async def test_fallback_local_passage_split(make_stack):
    content = "# 接口说明\n订单创建接口的细节\n\n# 异常处理\n超时重试规则"
    entries = [_entry("a1", EntryType.API, "订单创建接口", content)]
    cfg = RetrievalConfig(
        stage="case_generate", query_paths=2, recall_topk=30, inject_limit=15,
        inject_form="passage", allowed_types=[EntryType.API], token_budget=12_000,
    )
    stack = await make_stack(
        entries=entries,
        caps=ReMeCaps(metadata_filter=True, entry_version=True, passage_api=False),
    )
    get_before = stack.reader.get_calls
    outcome = await retrieve_pipeline(stack.ctx, cfg, INTENT)
    assert stack.reader.get_calls > get_before  # 拉正文本地切分
    assert any(
        d.step == "passage_extract" and d.fallback == "fetch_full_then_local_split"
        for d in outcome.degraded
    )
    assert outcome.items and outcome.items[0].passage


# ---------- ⑥ run 分区缓存语义（dd §8.5） ----------


async def test_cache_hit_same_run(make_stack):
    stack = await make_stack(level="meta")
    ctx = stack.ctx

    out1 = await retrieve_pipeline(ctx, _link_cfg(), INTENT, batch_id="b1")
    searches_1 = stack.reader.search_calls
    llm_1 = stack.llm.chat_calls
    assert searches_1 == 3  # 3 路 query 各一次 search

    # 同 run 第二次管线：recall 全命中（0 search）；rerank 分缓存（0 rerank LLM）；
    # multi_query 不缓存 → 仍 1 次 LLM
    out2 = await retrieve_pipeline(ctx, _link_cfg(), INTENT, batch_id="b2")
    assert stack.reader.search_calls == searches_1
    assert stack.llm.chat_calls == llm_1 + 1
    assert out2.injected == out1.injected


async def test_cache_cleared_new_run(make_stack):
    stack = await make_stack(level="meta")
    ctx = stack.ctx
    await retrieve_pipeline(ctx, _link_cfg(), INTENT, batch_id="b1")
    searches_1 = stack.reader.search_calls

    ctx.run_id = "run-2"
    await retrieve_pipeline(ctx, _link_cfg(), INTENT, batch_id="b1")
    assert stack.reader.search_calls == searches_1 + 3  # 分区清空重新召回


# ---------- ⑦ 截断端到端 ----------


async def test_rerank_cutoff_end_to_end(make_stack):
    stack = await make_stack(level="full")
    cfg = _link_cfg(inject_limit=2)
    outcome = await retrieve_pipeline(stack.ctx, cfg, INTENT, batch_id="b1")

    # rerank_cutoff 不属 assemble 的 truncated（dd §8.2：truncated 仅指
    # assemble 预算/硬上限截断）；裁剪以候选 drop_reason 为准
    assert outcome.truncated is False
    # rerank 分 100/99/98（脚本按序）→ l3 被 cutoff
    assert outcome.injected == ["l1", "l2"]
    cands = (await TraceDAO(stack.db).get(outcome.trace_id)).candidates_obj()
    cutoff = [c for c in cands if c.drop_reason == "rerank_cutoff"]
    assert [c.entry_id for c in cutoff] == ["l3"]
    # full 快照文件只含注入的 2 行
    snap = await SnapshotDAO(stack.db).get(outcome.snapshot_id)
    assert len(snap.items_obj()) == 2


# ---------- ⑧ snapshot 元数据落库 ----------


async def test_snapshot_metadata_fields(make_stack):
    stack = await make_stack(level="meta")
    outcome = await retrieve_pipeline(stack.ctx, _link_cfg(), INTENT)
    snap = await SnapshotDAO(stack.db).get(outcome.snapshot_id)

    assert snap.prompt_template_ver == "2026-09-26.1"  # prompts 包模板版本（dd §20.7）
    assert snap.budget == 12_000
    assert snap.truncated == 0
    assert snap.total_tokens_est == outcome.token_est
    assert snap.total_tokens_est > 0
    lat = snap.latencies_obj()
    assert lat["ordering_strategy"] == "anchor-v1"
    assert {
        "multi_query", "parallel_recall", "meta_filter",
        "rerank", "passage_extract", "assemble",
    } <= set(lat)
    aux = snap.usage_obj()["aux"]["retrieval"]
    assert aux["calls"] == 2
    assert aux["prompt_tokens"] == 20
    assert aux["completion_tokens"] == 10


# ---------- ⑨ funnel_counts ----------


def test_funnel_counts():
    cands = [
        Candidate(
            entry_id="a", entry_version="v1", title="t", score=1.0,
            source_channel="raw:0", entry_type=EntryType.API, kept=True,
        ),
        Candidate(
            entry_id="b", entry_version="v1", title="t", score=1.0,
            source_channel="raw:0", entry_type=EntryType.API, kept=False,
            drop_reason="rerank_cutoff",
        ),
        Candidate(
            entry_id="", entry_version="", title="", score=0.0,
            source_channel="raw:0", entry_type=EntryType.LINK_INDEX,
            kept=False, error="boom",
        ),
    ]
    assert funnel_counts(cands) == {
        "total": 3, "kept": 1, "errors": 1,
        "dropped": {"rerank_cutoff": 1},
    }


# ---------- ⑩ 其他 ----------


async def test_invalid_snapshot_level(make_stack):
    stack = await make_stack(level="off")
    stack.ctx.snapshot_level = "bogus"
    with pytest.raises(ValidationError):
        await retrieve_pipeline(stack.ctx, _link_cfg(), INTENT)


async def test_pipeline_empty_results(make_stack):
    stack = await make_stack(entries=[], level="meta")
    outcome = await retrieve_pipeline(stack.ctx, _link_cfg(), INTENT)
    assert outcome.items == []
    assert outcome.injected == []
    assert outcome.truncated is False
    # trace 仍写；snapshot meta 仍写（items 空）
    assert await _trace_count(stack.db) == 1
    snap = await SnapshotDAO(stack.db).get(outcome.snapshot_id)
    assert snap.items_obj() == []
