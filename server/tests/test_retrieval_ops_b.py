"""WP-11 检索算子 B 单测：rerank / passage_extract / assemble / RetrievalCache。

验收口径（work-breakdown WP-11）：
- rerank_cutoff / budget_cut 正确 —— TestRerank::test_cutoff_*、
  TestAssemble::test_budget_*；
- 锚点位置断言 —— TestAssemble::test_anchor_order_*。

另覆盖：>30 分桶（桶数/桶内召回分 top10/单桶失败降级）、LLM 失败→规则分、
缓存命中跳过 LLM 与 get_entry、版本换键失效、结构化切分 1~2 段与原文序、
800 字固定窗口、index_line 不拉正文、dedup 防御、token 估算口径、
RetrievalCache run 分区清空与 LRU 逐出。
"""

from __future__ import annotations

import json

import pytest

from tester_agent.adapters.reme import Entry, IndexMirror
from tester_agent.domain import Candidate, EntryType, InjectedItem, RetrievalConfig
from tester_agent.errors import ValidationError
from tester_agent.graph.retrieval import (
    ORDERING_STRATEGY,
    PASSAGE_WINDOW_CHARS,
    RetrievalCache,
    RetrievalTraceBuilder,
    assemble,
    entry_key,
    estimate_tokens,
    passage_extract,
    recall_key,
    rerank,
)
from tester_agent.graph.retrieval.ops_b import _select_passage, _split_segments

from fakes import FakeLLM, FakeReMeReader


# ---------- 构造辅助 ----------


def mk_cand(
    eid: str,
    *,
    score: float = 1.0,
    channel: str = "raw:0",
    title: str | None = None,
    version: str = "v1",
    kept: bool = True,
    etype: EntryType = EntryType.BUSINESS,
    error: str | None = None,
) -> Candidate:
    return Candidate(
        entry_id=eid,
        entry_version=version,
        title=title if title is not None else eid,
        score=score,
        source_channel=channel,
        entry_type=etype,
        kept=kept,
        error=error,
    )


def mk_cfg(**kw) -> RetrievalConfig:
    base = dict(
        stage="point_write",
        query_paths=4,
        recall_topk=40,
        inject_limit=20,
        inject_form="passage",
        allowed_types=[EntryType.BUSINESS],
        token_budget=16_000,
    )
    base.update(kw)
    return RetrievalConfig(**base)


def mk_item(eid: str, *, version: str = "v1", passage: str = "正文片段") -> InjectedItem:
    return InjectedItem(
        entry_id=eid, entry_version=version, title=eid, tokens_est=0, position=0,
        passage=passage,
    )


def scores_payload(pairs: list[tuple[str, float]]) -> str:
    return json.dumps(
        {"scores": [{"entry_id": eid, "score": s, "reason": "r"} for eid, s in pairs]},
        ensure_ascii=False,
    )


# ---------- token 估算（dd §8.2 口径） ----------


class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_pure_cjk(self):
        assert estimate_tokens("登录功能") == 4

    def test_pure_ascii_words(self):
        assert estimate_tokens("hello world") == 3  # ceil(2 * 1.3)

    def test_mixed(self):
        assert estimate_tokens("登录 login") == 4  # ceil(2 + 1.3)


# ---------- rerank ----------


class TestRerank:
    async def test_llm_scores_applied_and_sorted(self):
        cands = [mk_cand("e1"), mk_cand("e2"), mk_cand("e3")]
        llm = FakeLLM([scores_payload([("e1", 10), ("e2", 99), ("e3", 50)])])
        sink = RetrievalTraceBuilder()
        rows = await rerank(llm, cands, "意图", 10, sink=sink)
        assert [c.entry_id for c in rows] == ["e2", "e3", "e1"]
        assert rows[0].score == 99
        assert llm.chat_calls == 1
        assert sink.aux_usage["calls"] == 1
        assert sink.latencies["rerank"] >= 0
        assert [c.entry_id for c in sink.step_candidates["rerank"]] == ["e2", "e3", "e1"]

    async def test_cutoff_marks_rerank_cutoff(self):
        """验收：超出 limit 的候选 kept=False + drop_reason='rerank_cutoff'。"""
        cands = [mk_cand(f"e{i}") for i in range(5)]
        llm = FakeLLM([scores_payload([(f"e{i}", float(100 - i)) for i in range(5)])])
        rows = await rerank(llm, cands, "意图", 2)
        kept = [c for c in rows if c.kept]
        cut = [c for c in rows if not c.kept]
        assert [c.entry_id for c in kept] == ["e0", "e1"]
        assert len(cut) == 3
        assert all(c.drop_reason == "rerank_cutoff" for c in cut)

    async def test_llm_failure_falls_back_to_rule_score(self):
        cands = [mk_cand("e1", title="登录入口"), mk_cand("e2", title="其他")]
        llm = FakeLLM([RuntimeError("boom")])
        sink = RetrievalTraceBuilder()
        rows = await rerank(llm, cands, "登录", 10, sink=sink)
        # 规则分：e1 标题命中 bigram「登录」×3
        assert rows[0].entry_id == "e1" and rows[0].score == 3.0
        assert rows[1].score == 0.0
        assert [(d.step, d.reason, d.fallback) for d in sink.degraded] == [
            ("rerank", "llm_failed", "rule_score")
        ]

    async def test_llm_bad_output_degraded(self):
        cands = [mk_cand("e1")]
        sink = RetrievalTraceBuilder()
        await rerank(FakeLLM(["not json"]), cands, "意图", 10, sink=sink)
        assert sink.degraded[0].reason == "llm_bad_output"
        assert sink.degraded[0].fallback == "rule_score"

    async def test_llm_empty_scores_is_bad_output(self):
        sink = RetrievalTraceBuilder()
        await rerank(FakeLLM(['{"scores": []}']), [mk_cand("e1")], "意图", 10, sink=sink)
        assert sink.degraded[0].reason == "llm_bad_output"

    async def test_partial_coverage_keeps_rule_score_without_degraded(self):
        cands = [mk_cand("e1", title="登录"), mk_cand("e2", title="登录")]
        llm = FakeLLM([scores_payload([("e1", 88)])])
        sink = RetrievalTraceBuilder()
        rows = await rerank(llm, cands, "登录", 10, sink=sink)
        assert sink.degraded == []
        assert {c.entry_id: c.score for c in rows} == {"e1": 88.0, "e2": 3.0}

    async def test_unknown_or_invalid_scores_ignored(self):
        cands = [mk_cand("e1", title="登录"), mk_cand("e2", title="退出")]
        payload = json.dumps(
            {"scores": [
                {"entry_id": "ghost", "score": 99},
                {"entry_id": "e1", "score": "bad"},
                {"entry_id": "e2", "score": 7},
            ]}
        )
        rows = await rerank(FakeLLM([payload]), cands, "登录", 10)
        got = {c.entry_id: c.score for c in rows}
        assert got["e2"] == 7.0
        assert got["e1"] == 3.0  # 非法分数忽略 → 规则分

    async def test_bucketed_over_threshold(self):
        """>30 条按主通道分桶：11 raw + 10 keyword + 10 rewrite → 3 次调用，
        raw 桶按召回分取前 10（召回分最低者 r10 不送 LLM，保留规则分）。"""
        cands = (
            [mk_cand(f"r{i}", channel="raw:0", score=100.0 - i) for i in range(11)]
            + [mk_cand(f"k{i}", channel="keyword:1", score=50.0 - i) for i in range(10)]
            + [mk_cand(f"w{i}", channel="rewrite:2", score=10.0 - i) for i in range(10)]
        )
        llm = FakeLLM([
            scores_payload([(f"r{i}", 900.0) for i in range(10)]),
            scores_payload([(f"k{i}", 800.0) for i in range(10)]),
            scores_payload([(f"w{i}", 700.0) for i in range(10)]),
        ])
        rows = await rerank(llm, cands, "zzz 无命中", 31)
        assert llm.chat_calls == 3
        for call in llm.calls:
            body = call["messages"][-1]["content"]
            assert body.count('"entry_id"') == 10
        assert '"entry_id": "r10"' not in llm.calls[0]["messages"][-1]["content"]
        got = {c.entry_id: c.score for c in rows}
        assert got["r0"] == 900.0
        assert got["r10"] == 0.0  # 桶外 → 规则分（意图无命中为 0）
        assert got["k5"] == 800.0 and got["w9"] == 700.0

    async def test_bucket_single_bucket_failure_degrades_only_that_bucket(self):
        cands = (
            [mk_cand(f"r{i}", channel="raw:0", score=100.0 - i) for i in range(11)]
            + [mk_cand(f"k{i}", channel="keyword:1", score=50.0 - i) for i in range(10)]
            + [mk_cand(f"w{i}", channel="rewrite:2", score=10.0 - i) for i in range(10)]
        )
        llm = FakeLLM([
            RuntimeError("raw bucket boom"),
            scores_payload([(f"k{i}", 800.0) for i in range(10)]),
            scores_payload([(f"w{i}", 700.0) for i in range(10)]),
        ])
        sink = RetrievalTraceBuilder()
        rows = await rerank(llm, cands, "zzz 无命中", 31, sink=sink)
        assert [(d.reason, d.fallback) for d in sink.degraded] == [
            ("llm_failed", "rule_score")
        ]
        got = {c.entry_id: c.score for c in rows}
        assert got["r0"] == 0.0  # 失败桶 → 规则分
        assert got["k0"] == 800.0  # 他桶不受影响

    async def test_under_threshold_single_call(self):
        cands = [mk_cand(f"e{i}") for i in range(30)]
        llm = FakeLLM([scores_payload([(f"e{i}", 1.0) for i in range(30)])])
        await rerank(llm, cands, "意图", 30)
        assert llm.chat_calls == 1

    async def test_dropped_candidates_pass_through(self):
        err = mk_cand("", kept=False, error="boom", channel="keyword:1")
        normal = mk_cand("e1", title="登录")
        llm = FakeLLM([scores_payload([("e1", 9)])])
        rows = await rerank(llm, [err, normal], "登录", 10)
        body = llm.calls[0]["messages"][-1]["content"]
        assert "boom" not in body and body.count('"entry_id"') == 1
        assert rows[-1] is err and rows[-1].error == "boom" and rows[-1].drop_reason is None

    async def test_blank_intent_skips_llm(self):
        cands = [mk_cand("e2"), mk_cand("e1")]
        llm = FakeLLM([])
        rows = await rerank(llm, cands, "  ", 1)
        assert llm.chat_calls == 0
        assert rows[0].entry_id == "e1"  # 同分（0）按 entry_id 升序
        assert rows[1].drop_reason == "rerank_cutoff"

    async def test_cache_hit_skips_llm(self):
        cache = RetrievalCache()
        cache.start_run("t1", "run1")
        llm = FakeLLM([scores_payload([("e1", 66), ("e2", 33)])])
        await rerank(llm, [mk_cand("e1"), mk_cand("e2")], "意图", 10,
                     cache=cache, task_id="t1")
        assert llm.chat_calls == 1
        # 第二轮：新 Candidate 对象、同 id/version/intent → 全命中，零 LLM 调用
        rows = await rerank(llm, [mk_cand("e1"), mk_cand("e2")], "意图", 10,
                            cache=cache, task_id="t1")
        assert llm.chat_calls == 1
        assert {c.entry_id: c.score for c in rows} == {"e1": 66.0, "e2": 33.0}

    async def test_cache_keyed_by_version_and_intent(self):
        cache = RetrievalCache()
        cache.start_run("t1", "run1")
        llm = FakeLLM([scores_payload([("e1", 66)]), scores_payload([("e1", 77)])])
        await rerank(llm, [mk_cand("e1")], "意图", 10, cache=cache, task_id="t1")
        rows = await rerank(llm, [mk_cand("e1", version="v2")], "意图", 10,
                            cache=cache, task_id="t1")
        assert llm.chat_calls == 2  # 版本变化 → miss
        assert rows[0].score == 77.0

    async def test_cache_requires_task_id(self):
        with pytest.raises(ValidationError):
            await rerank(FakeLLM([]), [mk_cand("e1")], "意图", 10,
                         cache=RetrievalCache())

    async def test_type_weights_injection(self):
        cands = [
            mk_cand("e1", title="登录", etype=EntryType.BUSINESS),
            mk_cand("e2", title="登录", etype=EntryType.API),
        ]
        weights = {EntryType.API: 10.0}
        # LLM 脚本为空 → 触发降级走规则分，验证 ×类型权重 口径
        rows = await rerank(FakeLLM([]), cands, "登录", 10, type_weights=weights)
        assert {c.entry_id: c.score for c in rows} == {"e1": 3.0, "e2": 30.0}


# ---------- passage_extract ----------


SEC_CONTENT = (
    "# 登录\n登录相关说明文字。\n\n"
    "# 退款\n退款流程：先申请，再审核，最后原路退回。\n\n"
    "# 售后\n售后政策说明。"
)


def mk_entry(eid: str, content: str, *, version: str = "v1", title: str | None = None,
             summary: str = "") -> Entry:
    return Entry(
        entry_id=eid,
        entry_version=version,
        title=title or eid,
        content=content,
        entry_type=EntryType.BUSINESS,
        link_id=None,
        story_id=None,
        updated_at=None,
        raw={"summary": summary},
    )


class TestSplitSegments:
    def test_atx_sections(self):
        segs = _split_segments(SEC_CONTENT)
        assert len(segs) == 3
        assert segs[0].startswith("# 登录")

    def test_paragraphs_when_no_heading(self):
        segs = _split_segments("苹果香蕉橘子。\n\n退款流程审核说明。")
        assert segs == ["苹果香蕉橘子。", "退款流程审核说明。"]

    def test_no_structure_single_segment(self):
        assert _split_segments("一整段没有结构的文字") == ["一整段没有结构的文字"]


class TestSelectPassage:
    def test_picks_best_overlap_single(self):
        passage = _select_passage(SEC_CONTENT, "退款 审核")
        assert "# 退款" in passage
        assert "# 登录" not in passage and "# 售后" not in passage

    def test_two_segments_keep_original_order(self):
        passage = _select_passage(SEC_CONTENT, "登录 售后")
        assert "# 登录" in passage and "# 售后" in passage
        assert "# 退款" not in passage
        assert passage.index("# 登录") < passage.index("# 售后")  # 原文序而非分数序

    def test_no_structure_falls_to_window(self):
        content = "密" * 1000
        passage = _select_passage(content, "任意")
        assert passage == content[:PASSAGE_WINDOW_CHARS]
        assert len(passage) == PASSAGE_WINDOW_CHARS


class TestPassageExtract:
    async def test_index_line_uses_mirror_summary_without_get(self):
        entries = [
            mk_entry("lk1", "正文", title="链路一", summary="链路一摘要"),
        ]
        from tester_agent.domain import EntryType as ET

        entries[0].entry_type = ET.LINK_INDEX
        reader = FakeReMeReader(entries)
        mirror = IndexMirror(reader)
        cands = [mk_cand("lk1", title="链路一")]
        items = await passage_extract(cands, reader, "index_line", mirror=mirror)
        assert reader.get_calls == 0  # 不拉正文
        assert reader.tree_calls == 1  # 镜像首载
        assert items[0].passage == "链路一\n链路一摘要"
        assert items[0].title == "链路一"

    async def test_index_line_without_mirror_or_miss_falls_to_title(self):
        reader = FakeReMeReader([])
        items = await passage_extract([mk_cand("e1", title="标题一")], reader, "index_line")
        assert items[0].passage == "标题一"
        # 镜像存在但未收录该条目 → 仅标题
        mirror = IndexMirror(FakeReMeReader([]))
        items2 = await passage_extract(
            [mk_cand("ghost", title="幽灵")], reader, "index_line", mirror=mirror
        )
        assert items2[0].passage == "幽灵"
        assert reader.get_calls == 0

    async def test_passage_form_fetches_and_splits(self):
        reader = FakeReMeReader([mk_entry("e1", SEC_CONTENT)])
        cands = [mk_cand("e1", title="退款文档")]
        items = await passage_extract(cands, reader, "passage", intent="退款 审核")
        assert reader.get_calls == 1
        assert "# 退款" in (items[0].passage or "")
        assert "# 登录" not in (items[0].passage or "")

    async def test_passage_get_failure_marks_candidate_only(self):
        reader = FakeReMeReader([mk_entry("ok", SEC_CONTENT)], fail_get=None)

        from tester_agent.errors import NotFoundError

        async def fail_for_bad(entry_id: str):
            if entry_id == "bad":
                raise NotFoundError("无此条目")
            return await FakeReMeReader.get_entry(reader, entry_id)

        reader.get_entry = fail_for_bad  # type: ignore[method-assign]
        cands = [mk_cand("bad"), mk_cand("ok")]
        items = await passage_extract(cands, reader, "passage", intent="退款")
        assert [it.entry_id for it in items] == ["ok"]
        bad, ok = cands
        assert bad.kept is False and "无此条目" in (bad.error or "")
        assert ok.kept is True and ok.error is None

    async def test_passage_cache_hit_skips_get(self):
        cache = RetrievalCache()
        cache.start_run("t1", "run1")
        reader = FakeReMeReader([mk_entry("e1", SEC_CONTENT)])
        await passage_extract([mk_cand("e1")], reader, "passage", intent="退款",
                              cache=cache, task_id="t1")
        assert reader.get_calls == 1
        items = await passage_extract([mk_cand("e1")], reader, "passage", intent="退款",
                                      cache=cache, task_id="t1")
        assert reader.get_calls == 1  # 命中缓存
        assert items and "# 退款" in (items[0].passage or "")
        # 版本变化 → miss
        await passage_extract([mk_cand("e1", version="v2")], reader, "passage",
                              intent="退款", cache=cache, task_id="t1")
        assert reader.get_calls == 2

    async def test_dropped_candidates_produce_no_items(self):
        reader = FakeReMeReader([mk_entry("e1", SEC_CONTENT)])
        cands = [mk_cand("", kept=False, error="x"), mk_cand("e1")]
        items = await passage_extract(cands, reader, "passage", intent="退款")
        assert [it.entry_id for it in items] == ["e1"]

    async def test_invalid_form_and_cache_task_id_validation(self):
        reader = FakeReMeReader([])
        with pytest.raises(ValidationError):
            await passage_extract([mk_cand("e1")], reader, "weird")
        with pytest.raises(ValidationError):
            await passage_extract([mk_cand("e1")], reader, "passage",
                                  cache=RetrievalCache())


# ---------- assemble ----------


class TestAssemble:
    async def test_anchor_order_five_items(self):
        """验收：锚点位置——第1名首位、第2名末位、第3名次位、第4名次末位、其余填中间。"""
        cands = [mk_cand(eid, score=s) for eid, s in
                 [("a", 5.0), ("b", 4.0), ("c", 3.0), ("d", 2.0), ("e", 1.0)]]
        items = [mk_item(eid) for eid in "abcde"]
        out = await assemble(cands, items, mk_cfg())
        assert [it.entry_id for it in out.items] == ["a", "c", "e", "d", "b"]
        assert [it.position for it in out.items] == [0, 1, 2, 3, 4]
        assert out.injected == ["a", "c", "e", "d", "b"]
        assert out.truncated is False
        assert all(c.kept for c in cands)

    @pytest.mark.parametrize("n,expect", [
        (1, ["a"]),
        (2, ["a", "b"]),
        (3, ["a", "c", "b"]),
        (4, ["a", "c", "d", "b"]),
        (6, ["a", "c", "e", "f", "d", "b"]),
    ])
    async def test_anchor_order_parametric(self, n, expect):
        ids = list("abcdef")[:n]
        cands = [mk_cand(eid, score=float(n - i)) for i, eid in enumerate(ids)]
        out = await assemble(cands, [mk_item(eid) for eid in ids], mk_cfg())
        assert [it.entry_id for it in out.items] == expect

    async def test_inject_limit_cut_marks_budget_cut(self):
        """验收：超 inject_limit → 尾部候选 kept=False + drop_reason='budget_cut'。"""
        cands = [mk_cand(eid, score=float(4 - i)) for i, eid in enumerate("abcd")]
        items = [mk_item(eid) for eid in "abcd"]
        out = await assemble(cands, items, mk_cfg(inject_limit=2))
        assert [it.entry_id for it in out.items] == ["a", "b"]
        assert out.truncated is True
        assert {c.entry_id: c.drop_reason for c in cands} == {
            "a": None, "b": None, "c": "budget_cut", "d": "budget_cut",
        }
        assert all(c.kept == (c.drop_reason is None) for c in cands)

    async def test_token_budget_cut(self):
        """验收：累计 tokens 超 token_budget → 尾部 budget_cut；恰好等于预算保留。"""
        cands = [mk_cand(eid, score=float(3 - i)) for i, eid in enumerate("abc")]
        items = [mk_item(eid, passage="登录功能") for eid in "abc"]  # 各 4 tokens
        out = await assemble(cands, items, mk_cfg(token_budget=8))
        assert [it.entry_id for it in out.items] == ["a", "b"]  # 4+4=8 恰好保留
        assert out.token_est == 8
        assert cands[2].drop_reason == "budget_cut"

    async def test_tokens_est_recomputed_centrally(self):
        cands = [mk_cand("a")]
        item = mk_item("a", passage="登录 login")
        item.tokens_est = 999  # extract 占位值应被覆盖
        out = await assemble(cands, [item], mk_cfg())
        assert out.items[0].tokens_est == 4

    async def test_dedup_keeps_highest_score_position(self):
        """防御分支：两候选同 (id,version) → 只留高分 item，低分候选记 dedup。"""
        high = mk_cand("a", score=9.0)
        low = mk_cand("a", score=1.0)
        cands = [high, low, mk_cand("b", score=5.0)]
        items = [mk_item("a"), mk_item("a"), mk_item("b")]
        out = await assemble(cands, items, mk_cfg())
        assert [it.entry_id for it in out.items] == ["a", "b"]
        assert low.kept is False and low.drop_reason == "dedup"
        assert high.kept is True

    async def test_dedup_orphan_item_marks_nothing(self):
        """单候选 + 孤儿重复 item（无对应第二候选）→ 不误标候选。"""
        only = mk_cand("a", score=9.0)
        out = await assemble([only], [mk_item("a"), mk_item("a")], mk_cfg())
        assert len(out.items) == 1
        assert only.kept is True and only.drop_reason is None

    async def test_outcome_snapshots_sink_and_ordering_strategy(self):
        sink = RetrievalTraceBuilder()
        sink.add_degraded("rerank", "llm_failed", "rule_score")
        sink.latencies["rerank"] = 7
        cands = [mk_cand("a")]
        out = await assemble(cands, [mk_item("a")], mk_cfg(), sink=sink)
        assert sink.ordering_strategy == ORDERING_STRATEGY == "anchor-v1"
        assert out.latencies["rerank"] == 7 and "assemble" in out.latencies
        assert [d.step for d in out.degraded] == ["rerank"]

    async def test_pipeline_chain_rerank_extract_assemble(self):
        """链路串测：rerank(LLM) → passage_extract → assemble 全链留痕。"""
        reader = FakeReMeReader([
            mk_entry("e1", SEC_CONTENT, title="退款文档"),
            mk_entry("e2", "无关内容另一段。", title="杂项"),
        ])
        cands = [mk_cand("e1", title="退款文档"), mk_cand("e2", title="杂项")]
        sink = RetrievalTraceBuilder()
        llm = FakeLLM([scores_payload([("e1", 90), ("e2", 10)])])
        rows = await rerank(llm, cands, "退款", 10, sink=sink)
        items = await passage_extract(rows, reader, "passage", intent="退款", sink=sink)
        out = await assemble(rows, items, mk_cfg(inject_limit=1), sink=sink)
        assert out.injected == ["e1"]
        assert out.truncated is True
        assert {s for s in sink.latencies} == {"rerank", "passage_extract", "assemble"}
        assert [c.drop_reason for c in rows if not c.kept] == ["budget_cut"]


# ---------- RetrievalCache（dd §8.5） ----------


class TestRetrievalCache:
    def test_same_run_rerun_is_noop(self):
        cache = RetrievalCache()
        cache.start_run("t1", "run1")
        cache.put("t1", "k", "v")
        cache.start_run("t1", "run1")  # 同 run 重入
        assert cache.get("t1", "k") == "v"

    def test_new_run_clears_only_that_task(self):
        cache = RetrievalCache()
        cache.start_run("t1", "run1")
        cache.start_run("t2", "run1")
        cache.put("t1", "k", "v1")
        cache.put("t2", "k", "v2")
        cache.start_run("t1", "run2")  # t1 新 run
        assert cache.get("t1", "k") is None
        assert cache.get("t2", "k") == "v2"  # 他任务分区不受影响

    def test_lru_eviction_and_get_promotion(self):
        cache = RetrievalCache(capacity=3)
        for task in ("t",):
            cache.start_run(task, "r")
        cache.put("t", "a", 1)
        cache.put("t", "b", 2)
        cache.put("t", "c", 3)
        assert cache.get("t", "a") == 1  # 提升 a 为最近用
        cache.put("t", "d", 4)  # 逐出最久未用的 b
        assert cache.get("t", "b") is None
        assert cache.get("t", "a") == 1 and cache.get("t", "d") == 4
        assert len(cache) == 3

    def test_same_business_key_isolated_across_tasks(self):
        cache = RetrievalCache()
        cache.start_run("t1", "r1")
        cache.start_run("t2", "r1")
        cache.put("t1", "k", "v1")
        assert cache.get("t2", "k") is None

    def test_invalid_capacity(self):
        with pytest.raises(ValueError):
            RetrievalCache(capacity=0)


class TestCacheKeys:
    def test_recall_key_types_order_normalized(self):
        k1 = recall_key("ws", "kb", "q", 10, ["b", "a"])
        k2 = recall_key("ws", "kb", "q", 10, ["a", "b"])
        assert k1 == k2
        assert k1 != recall_key("ws", "kb", "q", 11, ["a", "b"])
        assert k1 != recall_key("ws", "kb", "q", 10, None)

    def test_entry_key_contains_version(self):
        k1 = entry_key("rerank", "e1", "v1", "意图")
        k2 = entry_key("e1", "v2", "意图") if False else entry_key("rerank", "e1", "v2", "意图")
        assert k1 != k2
        assert k1 != entry_key("extract", "e1", "v1", "意图")  # kind 隔离
