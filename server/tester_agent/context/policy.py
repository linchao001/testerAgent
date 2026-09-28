"""确定性上下文策略（spec §6）。

一期零 LLM（I5）：打分、裁剪、漂移检测均为纯函数。

score = 0.35·recency + 0.30·referenced + 0.20·goal_overlap
        + 0.15·kind_weight − dup_penalty

裁剪次序：score 升序，同分 (created_at, entry_id) 确定性 tiebreak；
pinned 条目不进候选（I3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import ContextEntry, ContextPartition, EntryKind

_TOMBSTONE_PREFIX = "〔已归档"

GOAL_OVERLAP_FLOOR = 0.05
DUP_THRESHOLD = 0.85
GOAL_DRIFT_DEFAULT_WINDOW = 5

_RECENCY_W = 0.35
_REFERENCED_W = 0.30
_GOAL_W = 0.20
_KIND_W = 0.15
_DUP_PENALTY = 0.5
_DIGEST_LIMIT = 80

KIND_WEIGHTS: dict[EntryKind, float] = {
    EntryKind.PLAN: 0.9,
    EntryKind.DECISION: 0.9,
    EntryKind.OUTLINE_DIGEST: 0.9,
    EntryKind.GOAL: 0.9,
    EntryKind.ARTIFACT_DIGEST: 0.7,
    EntryKind.REFLECTION: 0.6,
    EntryKind.TOOL_RESULT: 0.5,
    EntryKind.CHAT_TURN: 0.3,
}


# ---------- 文本相似度（与 graph/retrieval/pipeline 同构，层内纯函数重实现） ----------

_WS_RE = re.compile(r"\s+")


def char_bigrams(text: str) -> set[str]:
    compact = _WS_RE.sub("", text or "")
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def bigram_jaccard(a: str, b: str) -> float:
    ba, bb = char_bigrams(a), char_bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


# ---------- 打分因子 ----------


def kind_weight(kind: EntryKind) -> float:
    return KIND_WEIGHTS.get(kind, 0.3)


def _distance(e: ContextEntry, current_step_seq: int, current_turn_seq: int) -> int:
    if e.turn_seq:
        return max(0, current_turn_seq - e.turn_seq)
    if e.step_seq:
        return max(0, current_step_seq - e.step_seq)
    return 0


def recency(e: ContextEntry, current_step_seq: int, current_turn_seq: int) -> float:
    return 1.0 / (1.0 + _distance(e, current_step_seq, current_turn_seq))


def referenced(e: ContextEntry, referenced_ids: frozenset[str]) -> float:
    return 1.0 if e.entry_id in referenced_ids else 0.0


def goal_overlap(e: ContextEntry, goal: str) -> float:
    val = bigram_jaccard(e.content, goal or "")
    return val if val >= GOAL_OVERLAP_FLOOR else 0.0


def dup_penalty(e: ContextEntry, window_contents: list[str]) -> float:
    return _DUP_PENALTY if any(
        bigram_jaccard(e.content, w) > DUP_THRESHOLD for w in window_contents
    ) else 0.0


@dataclass(frozen=True)
class ScoreInput:
    entry: ContextEntry
    current_step_seq: int
    current_turn_seq: int
    goal: str
    referenced_ids: frozenset[str]
    window_contents: list[str]


def score(x: ScoreInput) -> float:
    e = x.entry
    val = (
        _RECENCY_W * recency(e, x.current_step_seq, x.current_turn_seq)
        + _REFERENCED_W * referenced(e, x.referenced_ids)
        + _GOAL_W * goal_overlap(e, x.goal)
        + _KIND_W * kind_weight(e.entry_kind)
        - dup_penalty(e, x.window_contents)
    )
    return round(val, 6)


# ---------- 候选与裁剪次序 ----------


def _score_args(
    current_step_seq: int,
    current_turn_seq: int,
    goal: str,
    referenced_ids: frozenset[str],
    window_contents: list[str],
):
    return dict(
        current_step_seq=current_step_seq,
        current_turn_seq=current_turn_seq,
        goal=goal,
        referenced_ids=referenced_ids,
        window_contents=window_contents,
    )


def eviction_order(
    entries: list[ContextEntry],
    *,
    current_step_seq: int,
    current_turn_seq: int,
    goal: str,
    referenced_ids: frozenset[str],
    window_contents: list[str],
) -> list[tuple[ContextEntry, float]]:
    """非 pinned 的 P2 ACTIVE 条目按（score 升序, created_at 升序, entry_id 升序）。"""
    kwargs = _score_args(
        current_step_seq, current_turn_seq, goal, referenced_ids, window_contents
    )
    scored = [
        (e, score(ScoreInput(entry=e, **kwargs)))
        for e in entries
        if not e.pinned and e.status.value == "active" and e.partition is ContextPartition.P2
    ]
    scored.sort(key=lambda pair: (pair[1], pair[0].created_at, pair[0].entry_id))
    return scored


def is_goal_drift(
    e: ContextEntry,
    *,
    current_step_seq: int,
    current_turn_seq: int,
    goal: str,
    referenced_ids: frozenset[str],
    window_contents: list[str],
    window: int = GOAL_DRIFT_DEFAULT_WINDOW,
) -> bool:
    """T3：非 pinned 且 goal_overlap=0 且距离 > window。"""
    if e.pinned or e.partition is not ContextPartition.P2:
        return False
    if goal_overlap(e, goal) != 0.0:
        return False
    return _distance(e, current_step_seq, current_turn_seq) > window


# ---------- tombstone / 程序化摘要 ----------


def make_tombstone(entry: ContextEntry) -> str:
    """归档摘要行：一行要点 + 溯源指针（P1 附 retrieve_kb 重取提示）。"""
    where = (
        entry.refs.payload_ref
        or entry.refs.trace_id
        or entry.refs.snapshot_id
        or entry.refs.message_id
        or "journal"
    )
    re_fetch = "；KB 知识可用 retrieve_kb 重取" if entry.partition is ContextPartition.P1 else ""
    return (
        f"{_TOMBSTONE_PREFIX} {entry.entry_kind.value} #{entry.entry_id}："
        f"{entry.digest}（全文：{where}{re_fetch}）〕"
    )


def programmatic_digest(content: str, limit: int = _DIGEST_LIMIT) -> str:
    """零 LLM 一行要点：取首行（无换行则首句/截断），限长。"""
    text = (content or "").strip()
    if not text:
        return ""
    first = text.splitlines()[0].strip()
    if "。" in first and len(first) > limit:
        first = first.split("。")[0] + "。"
    return first[:limit]
