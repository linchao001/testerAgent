"""context.policy：确定性打分、裁剪次序、T3 目标漂移、tombstone、程序化摘要。"""

from __future__ import annotations

import pytest

from tester_agent.context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
)
from tester_agent.context.policy import (
    DUP_THRESHOLD,
    GOAL_DRIFT_DEFAULT_WINDOW,
    GOAL_OVERLAP_FLOOR,
    ScoreInput,
    bigram_jaccard,
    char_bigrams,
    eviction_order,
    is_goal_drift,
    kind_weight,
    make_tombstone,
    programmatic_digest,
    score,
)


def _entry(eid="e1", **kw) -> ContextEntry:
    base = dict(
        entry_id=eid,
        partition=ContextPartition.P2,
        entry_kind=EntryKind.ARTIFACT_DIGEST,
        content="支付模块正常流测试用例设计",
        digest="支付用例",
        created_at="2026-09-28T00:00:00.000Z",
        step_seq=1,
    )
    base.update(kw)
    return ContextEntry(**base)


def _si(e, **kw) -> ScoreInput:
    base = dict(
        entry=e,
        current_step_seq=2,
        current_turn_seq=0,
        goal="支付模块测试",
        referenced_ids=frozenset(),
        window_contents=[],
    )
    base.update(kw)
    return ScoreInput(**base)


def test_bigram_jaccard_basic():
    assert bigram_jaccard("支付模块", "支付模块") == 1.0
    assert bigram_jaccard("", "x") == 0.0
    val = bigram_jaccard("支付模块登录", "支付模块下单")
    assert 0 < val < 1


def test_char_bigrams_strips_whitespace():
    assert char_bigrams("ab cd") == {"ab", "bc", "cd"}


def test_recency_factor_decays_with_step_distance():
    # 仅距离不同（内容/目标相同 → 其他因子相等）
    near = score(_si(_entry("near", step_seq=2)))
    far = score(_si(_entry("far", step_seq=1)))
    # recency 项：near=1.0 权重 0.35；far=0.5 权重 0.35 → 差 0.175
    assert near - far == pytest.approx(0.175)


def test_referenced_boost():
    e = _entry()
    plain = score(_si(e, referenced_ids=frozenset()))
    boosted = score(_si(e, referenced_ids=frozenset({"e1"})))
    assert boosted - plain == pytest.approx(0.30)


def test_goal_overlap_floor_boundary():
    # 与目标完全无关 → overlap 记 0
    e = _entry(content="量子纠缠拓扑学xxxxxxxx", step_seq=2)
    s = score(_si(e, goal="支付模块测试"))
    # 同条目但 goal 高重叠
    e2 = _entry("e2", content="支付模块测试用例", step_seq=2)
    s2 = score(_si(e2, goal="支付模块测试"))
    assert s2 > s
    assert GOAL_OVERLAP_FLOOR == 0.05
    # floor 以下（有一点字符碰撞但极低）记 0：构造与目标共享 ≤1 双字母的短串
    tiny_overlap = bigram_jaccard("支付", "xxxxxxx模块yyyyyyy")
    assert tiny_overlap < GOAL_OVERLAP_FLOOR


def test_kind_weights_table():
    assert kind_weight(EntryKind.PLAN) == 0.9
    assert kind_weight(EntryKind.DECISION) == 0.9
    assert kind_weight(EntryKind.OUTLINE_DIGEST) == 0.9
    assert kind_weight(EntryKind.GOAL) == 0.9
    assert kind_weight(EntryKind.ARTIFACT_DIGEST) == 0.7
    assert kind_weight(EntryKind.REFLECTION) == 0.6
    assert kind_weight(EntryKind.TOOL_RESULT) == 0.5
    assert kind_weight(EntryKind.CHAT_TURN) == 0.3


def test_dup_penalty():
    e = _entry(step_seq=2)
    clean = score(_si(e))
    duped = score(_si(e, window_contents=["支付模块正常流测试用例设计" + "后缀"]))
    assert DUP_THRESHOLD == 0.85
    assert clean - duped == pytest.approx(0.5)


def test_score_is_rounded_deterministic():
    s1 = score(_si(_entry()))
    s2 = score(_si(_entry()))
    assert s1 == s2
    assert round(s1, 6) == s1


def test_pinned_entries_never_in_eviction_order():
    hi = _entry("hi", pinned=True, content="支付", step_seq=1)
    lo = _entry("lo", content="无关内容zzzzzz", step_seq=1)
    order = eviction_order(
        [hi, lo],
        current_step_seq=9,
        current_turn_seq=0,
        goal="支付模块测试",
        referenced_ids=frozenset(),
        window_contents=[],
    )
    ids = [e.entry_id for e, _ in order]
    assert "hi" not in ids
    assert ids == ["lo"]


def test_eviction_order_low_score_first_then_chronological_tiebreak():
    a = _entry("a", content="支付模块测试用例", created_at="2026-09-28T00:00:01.000Z", step_seq=2)
    b = _entry("b", content="支付模块测试用例", created_at="2026-09-28T00:00:02.000Z", step_seq=2)
    order = eviction_order(
        [b, a],
        current_step_seq=2,
        current_turn_seq=0,
        goal="支付模块测试用例",
        referenced_ids=frozenset(),
        window_contents=[],
    )
    # 同分 → (created_at, entry_id) 升序：a 先于 b
    assert [e.entry_id for e, _ in order] == ["a", "b"]


def test_goal_drift_requires_three_conditions():
    old = _entry(
        "old", content="完全无关的旧讨论xxxxxxxx",
        created_at="2026-09-28T00:00:00.000Z", step_seq=1,
    )
    # 非 pinned + overlap 0 + step 距离 > W
    assert is_goal_drift(
        old, current_step_seq=1 + GOAL_DRIFT_DEFAULT_WINDOW + 1,
        current_turn_seq=0, goal="支付模块测试",
        referenced_ids=frozenset(), window_contents=[],
    )
    # 距离不够
    assert not is_goal_drift(
        old, current_step_seq=2, current_turn_seq=0, goal="支付模块测试",
        referenced_ids=frozenset(), window_contents=[],
    )
    # pinned 不算
    pinned = old.model_copy(update={"pinned": True})
    assert not is_goal_drift(
        pinned, current_step_seq=99, current_turn_seq=0, goal="支付模块测试",
        referenced_ids=frozenset(), window_contents=[],
    )
    # 与目标有重叠不算漂移
    related = _entry("rel", content="支付模块测试补充说明", step_seq=1)
    assert not is_goal_drift(
        related, current_step_seq=99, current_turn_seq=0, goal="支付模块测试",
        referenced_ids=frozenset(), window_contents=[],
    )


def test_tombstone_variants():
    p2 = _entry()
    t = make_tombstone(p2)
    assert t.startswith("〔已归档 artifact #e1：")
    assert "支付用例" in t
    assert "（全文：journal）" in t
    p1 = _entry(
        "kb1", partition=ContextPartition.P1, entry_kind=EntryKind.KB_BLOCK,
        digest="登录链路知识",
    )
    t1 = make_tombstone(p1)
    assert "retrieve_kb 重取" in t1


def test_tombstone_picks_first_available_ref():
    e = _entry("x")
    from tester_agent.context.models import EntryRefs

    assert "journal" in make_tombstone(e)
    e2 = e.model_copy(update={"refs": EntryRefs(payload_ref="art-9")})
    assert "art-9" in make_tombstone(e2)
    e3 = e.model_copy(update={"refs": EntryRefs(trace_id="tr-9")})
    assert "tr-9" in make_tombstone(e3)


def test_programmatic_digest_first_line_truncated():
    assert programmatic_digest("第一行要点\n第二行") == "第一行要点"
    long = "字" * 100
    d = programmatic_digest(long)
    assert len(d) == 80
    # 空白回退为空串安全
    assert programmatic_digest("") == ""
