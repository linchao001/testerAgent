"""WP-10 检索算子 A 单测：multi_query / parallel_recall / meta_filter。

验收口径（work-breakdown WP-10）：
- 单路失败不阻他路 —— error 候选垫底、他路候选完整（TestParallelRecall）；
- filtered_* 裁剪留痕 —— kept=False + drop_reason 精确断言（TestMetaFilter）。

另覆盖：raw 路恒在/降级记痕、(entry_id, entry_version) 并集去重与 channel 聚合、
scope 落型校验、镜像降级路径、entry_version 本地 hash、并发上限、CancelledError
传播、RetrievalTraceBuilder 累加口径。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tester_agent.adapters.reme import IndexMirror, local_entry_version
from tester_agent.domain import Candidate, DegradedStep, EntryType, QueryVariant
from tester_agent.errors import (
    KbUnreachable,
    LLMBadOutput,
    RateLimitedError,
    ValidationError,
)
from tester_agent.context.retrieval import (
    RetrievalTraceBuilder,
    meta_filter,
    multi_query,
    normalize_scope,
    parallel_recall,
)
from tester_agent.context.retrieval.ops import lexical_score

from fakes import FakeLLM, FakeReMeReader


def mk_entry(
    eid: str,
    title: str,
    content: str,
    *,
    etype: EntryType = EntryType.BUSINESS,
    link_id: str | None = None,
    story_id: str | None = None,
    version: str = "v1",
) -> object:
    from tester_agent.adapters.reme import Entry

    return Entry(
        entry_id=eid,
        entry_version=version,
        title=title,
        content=content,
        entry_type=etype,
        link_id=link_id,
        story_id=story_id,
        updated_at="2026-09-27T00:00:00Z",
        raw={"summary": title},
    )


def mk_reader(entries, **kwargs) -> FakeReMeReader:
    return FakeReMeReader(entries, **kwargs)


def mk_cand(eid: str, *, etype: EntryType = EntryType.BUSINESS, **kw) -> Candidate:
    base = dict(
        entry_id=eid,
        entry_version="v1",
        title=eid,
        score=1.0,
        source_channel="raw:0",
        entry_type=etype,
        kept=True,
    )
    base.update(kw)
    return Candidate(**base)


# ---------------- multi_query ----------------


class TestMultiQuery:
    async def test_raw_first_and_llm_paths(self):
        llm = FakeLLM(
            [json.dumps({"keyword_queries": ["退款 流程"], "rewrite_queries": ["退款超时如何处理"]})]
        )
        sink = RetrievalTraceBuilder()
        variants = await multi_query(llm, "退款超时审核", 3, sink=sink)

        assert [v.channel for v in variants] == ["raw", "keyword", "rewrite"]
        assert variants[0].text == "退款超时审核"
        assert [v.text for v in variants[1:]] == ["退款 流程", "退款超时如何处理"]
        assert llm.chat_calls == 1
        assert llm.calls[0]["json_schema"] is not None
        assert sink.latencies["multi_query"] >= 0
        assert sink.aux_usage == {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5}
        assert sink.degraded == []

    async def test_llm_failure_raw_only_degraded(self):
        llm = FakeLLM([RateLimitedError("限流")])
        sink = RetrievalTraceBuilder()
        variants = await multi_query(llm, "退款超时", 3, sink=sink)

        assert [v.channel for v in variants] == ["raw"]
        assert sink.degraded == [
            DegradedStep(step="multi_query", reason="llm_failed", fallback="raw_only")
        ]
        # 失败调用也计入 aux.calls（发起次数口径），token 未知不累加
        assert sink.aux_usage["calls"] == 1
        assert sink.aux_usage["prompt_tokens"] == 0

    async def test_bad_output_degraded(self):
        for bad in ("not json at all", json.dumps({"keyword_queries": [], "rewrite_queries": []})):
            llm = FakeLLM([bad])
            sink = RetrievalTraceBuilder()
            variants = await multi_query(llm, "意图", 3, sink=sink)
            assert [v.channel for v in variants] == ["raw"]
            assert sink.degraded == [
                DegradedStep(step="multi_query", reason="llm_bad_output", fallback="raw_only")
            ]

    async def test_client_bad_output_maps_reason(self):
        llm = FakeLLM([LLMBadOutput("重请后仍坏")])
        sink = RetrievalTraceBuilder()
        await multi_query(llm, "意图", 2, sink=sink)
        assert sink.degraded[0].reason == "llm_bad_output"

    async def test_partial_success_no_degraded(self):
        llm = FakeLLM([json.dumps({"keyword_queries": ["a", "b"], "rewrite_queries": []})])
        variants = await multi_query(llm, "意图", 4)
        assert len(variants) == 3  # raw + 2 keyword
        assert all(v.channel in ("raw", "keyword") for v in variants)

    async def test_dedup_and_interleave_order(self):
        llm = FakeLLM(
            [json.dumps({"keyword_queries": ["退款", "refund flow"], "rewrite_queries": ["退款"]})]
        )
        variants = await multi_query(llm, "订单退款超时处理", 4)
        assert [v.text for v in variants] == ["订单退款超时处理", "退款", "refund flow"]
        assert [v.channel for v in variants] == ["raw", "keyword", "keyword"]

    async def test_variant_duplicate_of_raw_skipped(self):
        llm = FakeLLM(
            [json.dumps({"keyword_queries": ["订单退款超时处理"], "rewrite_queries": ["换个说法"]})]
        )
        variants = await multi_query(llm, "订单退款超时处理", 3)
        assert [v.channel for v in variants] == ["raw", "rewrite"]
        assert variants[1].text == "换个说法"

    async def test_n1_no_llm_call(self):
        llm = FakeLLM([])
        variants = await multi_query(llm, "意图", 1)
        assert [v.channel for v in variants] == ["raw"]
        assert llm.chat_calls == 0

    async def test_blank_intent_no_llm_call(self):
        llm = FakeLLM([])
        variants = await multi_query(llm, "   ", 3)
        assert len(variants) == 1 and variants[0].channel == "raw"
        assert llm.chat_calls == 0


# ---------------- parallel_recall ----------------


class FlakyReader:
    """按 query 文本注入失败的包装 reader（单路容错验证用）。"""

    def __init__(self, inner, fail_on: set[str], exc: Exception | None = None):
        self._inner = inner
        self._fail_on = fail_on
        self._exc = exc or KbUnreachable("boom")
        self.caps = inner.caps

    async def search(self, query, *, top_k, types=None, scope=None):
        if query in self._fail_on:
            raise self._exc
        return await self._inner.search(query, top_k=top_k, types=types, scope=scope)

    async def get_entry(self, entry_id):
        return await self._inner.get_entry(entry_id)

    async def list_index_tree(self):
        return await self._inner.list_index_tree()


class TrackingReader:
    """记录 search 并发峰值的包装 reader。"""

    def __init__(self, inner):
        self._inner = inner
        self.in_flight = 0
        self.max_in_flight = 0
        self.caps = inner.caps

    async def search(self, query, *, top_k, types=None, scope=None):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            return await self._inner.search(query, top_k=top_k, types=types, scope=scope)
        finally:
            self.in_flight -= 1

    async def get_entry(self, entry_id):
        return await self._inner.get_entry(entry_id)

    async def list_index_tree(self):
        return await self._inner.list_index_tree()


def recall_entries():
    return [
        mk_entry("e1", "退款流程说明", "用户申请退款后进入审核，超时未审核自动升级", link_id="L1", story_id="S1"),
        mk_entry("e2", "退款超时缺陷记录", "退款 超时 缺陷：审核卡死", etype=EntryType.DEFECT, link_id="L1", story_id="S2"),
        mk_entry("e3", "支付接口文档", "支付网关对接说明，含退款回调", etype=EntryType.API, link_id="L2", story_id="S3"),
    ]


class TestParallelRecall:
    async def test_union_dedup_and_channel_merge(self):
        reader = mk_reader(recall_entries())
        queries = [QueryVariant(channel="raw", text="退款"), QueryVariant(channel="keyword", text="退款 超时")]
        rows = await parallel_recall(reader, queries, 10)

        e1 = [c for c in rows if c.entry_id == "e1"]
        assert len(e1) == 1
        assert e1[0].source_channel == "keyword:1,raw:0"  # 排序逗号拼接
        assert e1[0].kept is True and e1[0].error is None
        # 并集保留最高分
        s_raw = lexical_score("退款", "退款流程说明", "用户申请退款后进入审核，超时未审核自动升级")
        s_kw = lexical_score("退款 超时", "退款流程说明", "用户申请退款后进入审核，超时未审核自动升级")
        assert e1[0].score == max(s_raw, s_kw) == s_kw  # keyword 路多命中"超时"

    async def test_order_score_desc_then_entry_id(self):
        reader = mk_reader(recall_entries())
        rows = await parallel_recall(reader, [QueryVariant(channel="raw", text="退款")], 10)
        ok = [c for c in rows if c.kept]
        scores = [c.score for c in ok]
        assert scores == sorted(scores, reverse=True)
        # 标题命中 3 倍权重：e1（标题含退款）在 e3（仅正文含退款回调）之前
        ids = [c.entry_id for c in ok]
        assert ids.index("e1") < ids.index("e3")

    async def test_single_path_failure_error_candidate(self):
        reader = FlakyReader(mk_reader(recall_entries()), fail_on={"退款 超时"})
        queries = [QueryVariant(channel="raw", text="退款"), QueryVariant(channel="keyword", text="退款 超时")]
        rows = await parallel_recall(reader, queries, 10)

        errs = [c for c in rows if c.error is not None]
        assert len(errs) == 1
        assert errs[0].source_channel == "keyword:1"
        assert errs[0].kept is False
        assert "boom" in errs[0].error
        # 他路不受影响（验收：单路失败不阻他路）
        ok = [c for c in rows if c.error is None]
        assert {c.entry_id for c in ok} == {"e1", "e2", "e3"}
        # error 候选垫底
        assert rows[-1].error is not None

    async def test_all_paths_fail(self):
        reader = FlakyReader(mk_reader(recall_entries()), fail_on={"q1", "q2"})
        rows = await parallel_recall(
            reader,
            [QueryVariant(channel="raw", text="q1"), QueryVariant(channel="keyword", text="q2")],
            10,
        )
        assert len(rows) == 2
        assert {c.source_channel for c in rows} == {"raw:0", "keyword:1"}
        assert all(c.error for c in rows)

    async def test_cancelled_error_propagates(self):
        reader = FlakyReader(mk_reader(recall_entries()), fail_on={"退款"}, exc=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await parallel_recall(reader, [QueryVariant(channel="raw", text="退款")], 10)

    async def test_types_server_side_filter(self):
        reader = mk_reader(recall_entries())  # caps.metadata_filter=True
        rows = await parallel_recall(
            reader, [QueryVariant(channel="raw", text="退款")], 10, types=["business"]
        )
        assert {c.entry_type for c in rows} == {EntryType.BUSINESS}

    async def test_scope_server_side_filter(self):
        reader = mk_reader(recall_entries())
        rows = await parallel_recall(
            reader,
            [QueryVariant(channel="raw", text="退款")],
            10,
            scope={"link_ids": ["L1"]},
        )
        assert {c.entry_id for c in rows} == {"e1", "e2"}  # e3 属 L2 被服务端剔除

    async def test_caps_off_server_ignores_types(self):
        # metadata_filter=False：服务端忽略 types（WP-08 约定），类型过滤由 meta_filter 兜底
        reader = mk_reader(recall_entries())
        reader.caps = reader.caps.model_copy(update={"metadata_filter": False})
        rows = await parallel_recall(
            reader, [QueryVariant(channel="raw", text="退款")], 10, types=["business"]
        )
        assert {EntryType.DEFECT, EntryType.BUSINESS} <= {c.entry_type for c in rows}

    async def test_version_fallback_local_hash(self):
        entries = [mk_entry("e1", "退款流程", "内容", version="v1")]
        reader = mk_reader(entries)
        reader.caps = reader.caps.model_copy(update={"entry_version": False})
        rows = await parallel_recall(reader, [QueryVariant(channel="raw", text="退款")], 10)
        assert rows[0].entry_version == local_entry_version("内容")
        assert rows[0].entry_version.startswith("h-")

    async def test_topk_respected(self):
        reader = mk_reader(recall_entries() + [mk_entry("e4", "退款索引", "索引", etype=EntryType.LINK_INDEX, link_id="L1")])
        rows = await parallel_recall(reader, [QueryVariant(channel="raw", text="退款")], 2)
        assert len([c for c in rows if c.kept]) == 2

    async def test_concurrency_semaphore(self):
        reader = TrackingReader(mk_reader(recall_entries()))
        queries = [QueryVariant(channel="raw", text="退款"), QueryVariant(channel="keyword", text="退款 超时")]
        await parallel_recall(reader, queries, 10, concurrency=1)
        assert reader.max_in_flight == 1

        reader2 = TrackingReader(mk_reader(recall_entries()))
        await parallel_recall(reader2, queries, 10, concurrency=2)
        assert reader2.max_in_flight == 2

    async def test_invalid_scope_raises(self):
        reader = mk_reader(recall_entries())
        queries = [QueryVariant(channel="raw", text="q")]
        for bad in ({"bogus": []}, {"link_ids": "L1"}, {"story_ids": [1]}):
            with pytest.raises(ValidationError):
                await parallel_recall(reader, queries, 10, scope=bad)

    async def test_empty_queries(self):
        assert await parallel_recall(mk_reader([]), [], 10) == []

    async def test_sink_records(self):
        reader = mk_reader(recall_entries())
        sink = RetrievalTraceBuilder()
        rows = await parallel_recall(reader, [QueryVariant(channel="raw", text="退款")], 10, sink=sink)
        assert sink.step_candidates["parallel_recall"] == rows
        assert sink.latencies["parallel_recall"] >= 0


# ---------------- meta_filter ----------------


def mirror_reader():
    """含两级索引树 + 业务条目的 reader（镜像降级路径验证）。"""
    return mk_reader(
        [
            mk_entry("ix-link-l1", "退款链路", "链路", etype=EntryType.LINK_INDEX, link_id="L1"),
            mk_entry("ix-story-s1", "退款故事一", "故事", etype=EntryType.LINK_INDEX, link_id="L1", story_id="S1"),
            mk_entry("ix-story-s2", "退款故事二", "故事", etype=EntryType.LINK_INDEX, link_id="L1", story_id="S2"),
            mk_entry("ix-link-l2", "支付链路", "链路", etype=EntryType.LINK_INDEX, link_id="L2"),
            mk_entry("biz-1", "退款规则", "业务规则正文", link_id="L1", story_id="S1"),
        ]
    )


class TestMetaFilter:
    async def test_filtered_type(self):
        cands = [mk_cand("b1"), mk_cand("d1", etype=EntryType.DEFECT)]
        out = await meta_filter(cands, types=["business"])
        assert out[0].kept is True and out[0].drop_reason is None
        assert out[1].kept is False and out[1].drop_reason == "filtered_type"

    async def test_mirror_scope_filtered_scope(self):
        mirror = IndexMirror(mirror_reader())
        cands = [
            mk_cand("ix-link-l1", etype=EntryType.LINK_INDEX),
            mk_cand("ix-link-l2", etype=EntryType.LINK_INDEX),
            mk_cand("ix-story-s2", etype=EntryType.LINK_INDEX),
            mk_cand("biz-1"),  # 不在镜像：scope 白名单语义 → 剔除
        ]
        out = await meta_filter(
            cands, scope={"link_ids": ["L1"]}, mirror=mirror, sink=RetrievalTraceBuilder()
        )
        # 仅给 link_ids 约束：L1 的故事条目随之放行（WP-08 matches 语义），L2 剔除
        assert [c.drop_reason for c in out] == [
            None,
            "filtered_scope",
            None,
            "filtered_scope",
        ]
        assert [c.kept for c in out] == [True, False, True, False]

    async def test_mirror_story_scope(self):
        mirror = IndexMirror(mirror_reader())
        cands = [
            mk_cand("ix-story-s1", etype=EntryType.LINK_INDEX),
            mk_cand("ix-story-s2", etype=EntryType.LINK_INDEX),
        ]
        out = await meta_filter(cands, scope={"story_ids": ["S2"]}, mirror=mirror)
        assert out[0].drop_reason == "filtered_scope"
        assert out[1].kept is True

    async def test_mirror_empty_whitelist_filters_all_in_mirror(self):
        mirror = IndexMirror(mirror_reader())
        cands = [mk_cand("ix-link-l1", etype=EntryType.LINK_INDEX)]
        out = await meta_filter(cands, scope={"link_ids": []}, mirror=mirror)
        assert out[0].kept is False and out[0].drop_reason == "filtered_scope"

    async def test_no_mirror_scope_passthrough(self):
        # mirror=None：scope 已服务端过滤（能力可用）或跳过过滤（镜像空降级）→ 放行
        cands = [mk_cand("a"), mk_cand("b", etype=EntryType.API)]
        out = await meta_filter(cands, scope={"link_ids": ["L9"]})
        assert all(c.kept for c in out)

    async def test_type_priority_over_scope(self):
        mirror = IndexMirror(mirror_reader())
        cands = [mk_cand("ix-link-l2", etype=EntryType.LINK_INDEX)]  # 类型+归属双不合规
        out = await meta_filter(cands, types=["business"], scope={"link_ids": ["L1"]}, mirror=mirror)
        assert out[0].drop_reason == "filtered_type"

    async def test_error_rows_untouched(self):
        err = mk_cand("", kept=False, error="boom", drop_reason=None)
        out = await meta_filter([err], types=["business"])
        assert out[0].kept is False and out[0].drop_reason is None

    async def test_mutates_in_place_and_returns_same_list(self):
        cands = [mk_cand("b1"), mk_cand("d1", etype=EntryType.DEFECT)]
        out = await meta_filter(cands, types=["business"])
        assert out is cands
        assert cands[1].kept is False

    async def test_invalid_scope_raises(self):
        for bad in ({"bogus": []}, {"link_ids": "L1"}, "L1"):
            with pytest.raises(ValidationError):
                await meta_filter([], scope=bad)

    async def test_sink_records(self):
        sink = RetrievalTraceBuilder()
        cands = [mk_cand("b1")]
        await meta_filter(cands, types=["business"], sink=sink)
        assert sink.step_candidates["meta_filter"] == cands
        assert sink.latencies["meta_filter"] >= 0


# ---------------- normalize_scope / lexical_score / TraceBuilder ----------------


class TestScopeAndScoring:
    def test_normalize_scope_none_and_empty(self):
        assert normalize_scope(None) == (None, None)
        assert normalize_scope({}) == (None, None)
        assert normalize_scope({"link_ids": []}) == (set(), None)
        assert normalize_scope({"link_ids": ["A", "A"], "story_ids": ["S1"]}) == ({"A"}, {"S1"})
        assert normalize_scope({"link_ids": ("A",)}) == ({"A"}, None)

    def test_lexical_score_weights(self):
        assert lexical_score("退款", "退款流程", "无关正文") == 3.0
        assert lexical_score("退款", "无命中", "提到退款一次") == 1.0
        assert lexical_score("refund api", "refund API 文档", "调用 api") > 0
        assert lexical_score("", "任意", "任意") == 0.0

    def test_trace_builder_accumulation(self):
        sink = RetrievalTraceBuilder()
        sink.add_aux_usage({"prompt_tokens": 3, "completion_tokens": 4})
        sink.add_aux_usage({"prompt_tokens": 1})
        assert sink.aux_usage == {"calls": 2, "prompt_tokens": 4, "completion_tokens": 4}
        sink.record("s1", latency_ms=7, candidates=[])
        sink.record("s1", latency_ms=9)
        assert sink.latencies == {"s1": 9}
        assert sink.step_candidates == {"s1": []}


# ---------------- 链路串测（caps-on 全能力 happy path） ----------------


class TestChainHappyPath:
    async def test_multi_query_recall_filter_funnel(self):
        llm = FakeLLM(
            [json.dumps({"keyword_queries": ["退款 超时"], "rewrite_queries": ["退款审核流程"]})]
        )
        reader = mk_reader(recall_entries())
        sink = RetrievalTraceBuilder()

        variants = await multi_query(llm, "退款超时审核", 3, sink=sink)
        assert len(variants) == 3

        rows = await parallel_recall(
            reader, variants, 10, types=["business", "defect"], sink=sink
        )
        # api 条目已被服务端类型过滤剔除
        assert EntryType.API not in {c.entry_type for c in rows if c.kept}

        kept = await meta_filter(rows, types=["business", "defect"], sink=sink)
        assert all(c.kept for c in kept if c.error is None)
        assert set(sink.step_candidates) == {"parallel_recall", "meta_filter"}
        assert set(sink.latencies) == {"multi_query", "parallel_recall", "meta_filter"}
        assert sink.aux_usage["calls"] == 1
