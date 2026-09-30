"""token 估算（WP-30 自 retrieval/ops_b 平移，口径不变）。

中英混合：``len(cjk_chars) + len(ascii_words) * 1.3``，ceil 取整（保守预算口径）。
ops_b 保留 re-export，既有调用路径零行为变化。
"""

from __future__ import annotations

import math
import re

_CJK_CHAR = re.compile(r"[一-鿿]")
_ASCII_WORD = re.compile(r"[a-z0-9_]+")


def estimate_tokens(text: str) -> int:
    """``len(cjk_chars) + len(ascii_words) * 1.3``，ceil 取整（保守预算口径）。"""
    if not text:
        return 0
    cjk = len(_CJK_CHAR.findall(text.lower()))
    ascii_words = len(_ASCII_WORD.findall(text.lower()))
    return math.ceil(cjk + ascii_words * 1.3)
