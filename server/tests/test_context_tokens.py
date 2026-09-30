"""context.tokens：token 估算（自 ops_b 平移，口径必须完全一致）。"""

from __future__ import annotations

import math

from tester_agent.context.tokens import estimate_tokens
from tester_agent.context.retrieval.ops_b import estimate_tokens as ops_b_estimate_tokens


def test_empty_is_zero():
    assert estimate_tokens("") == 0


def test_pure_cjk_counts_chars():
    assert estimate_tokens("你好世界") == 4


def test_ascii_words_count_times_1_3_ceil():
    # 2 个 ascii 词：2 * 1.3 = 2.6 → ceil 3
    assert estimate_tokens("hello world") == 3


def test_mixed_cjk_ascii():
    # 2 CJK + 1 ascii 词 * 1.3 = 3.3 → ceil 4
    assert estimate_tokens("你好 abc") == 4


def test_digits_and_underscores_are_words():
    assert estimate_tokens("a1 b2") == 3


def test_case_insensitive():
    assert estimate_tokens("ABC def") == 3


def test_consistent_with_ops_b_reexport():
    # ops_b 必须 re-export 同一实现（平移后既有调用零行为变化）
    assert ops_b_estimate_tokens is estimate_tokens or (
        ops_b_estimate_tokens("中文 mixed text x") == estimate_tokens("中文 mixed text x")
    )
    samples = ["", "你好", "hello world 你好", "a" * 100, "边界值 等价类 boundary"]
    for s in samples:
        assert ops_b_estimate_tokens(s) == estimate_tokens(s)
        assert estimate_tokens(s) >= 0
        assert isinstance(estimate_tokens(s), int)


def test_never_below_naive_floor_for_ascii():
    # 每个 ascii 词至少贡献 1（ceil(1.3)=2 单词情形除外——1 词 ceil(1.3)=2）
    assert estimate_tokens("word") == 2
    assert math.ceil(1.3) == 2
