"""LLM 调用窗口的唯一组装入口（spec §4.5/§5，I2 确定性/I3 钉住）。

四步：① scope 过滤（跨条目/跨阶段物理排除，无 tombstone）→ ② 分区装配
（P0 冻结前缀 / P1 挂载序 / P2 pinned+活跃+墓碑）→ ③ 预算裁剪（T2 尾部
截断 + T3 目标漂移前置，pinned 放行 budget_override）→ ④ 消息序列 + Report。

裁剪只改"本次及未来窗口读什么"；persist_evictions=False（playground）时
连 store 也不触碰（零写入）。
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from .budget import PROFILES, ProfileBudget, validate_budget
from .models import (
    AssemblyReport,
    ContextPartition,
    EntryKind,
    EntryStatus,
    EvictionRecord,
    ProfileName,
)
from .policy import eviction_order, is_goal_drift
from .scopes import ExecScope, current_scope, visible_in_window
from .store import ContextStore
from .tokens import estimate_tokens

_TOMBSTONE_LIMIT = 20


@dataclass(frozen=True)
class AssemblyResult:
    messages: list[BaseMessage]
    report: AssemblyReport


def _entry_tokens(entries) -> int:
    return sum(e.tokens_est for e in entries)


async def assemble(
    store: ContextStore,
    profile: ProfileName,
    *,
    p0_messages: list[BaseMessage],
    model_window: int,
    scope: ExecScope | None = None,
    goal: str | None = None,
    referenced_ids: frozenset[str] = frozenset(),
    persist_evictions: bool = True,
    recent_turns: int = 6,
    goal_drift_window: int = 5,
    budget_override: ProfileBudget | None = None,
) -> AssemblyResult:
    budget = budget_override or PROFILES[profile]
    validate_budget(budget, model_window=model_window)
    s = scope or current_scope()
    goal_text = goal if goal is not None else (store.goal_text() or "")

    visible = [
        e
        for e in store.entries()
        if visible_in_window(e, profile=profile, scope_=s)
    ]
    p1_active = [
        e for e in visible
        if e.partition is ContextPartition.P1 and e.status is EntryStatus.ACTIVE
    ]
    p2_active = [
        e for e in visible
        if e.partition is ContextPartition.P2 and e.status is EntryStatus.ACTIVE
    ]
    p2_demoted = [
        e for e in visible
        if e.partition is ContextPartition.P2 and e.status is EntryStatus.DEMOTED
    ]

    cuts: list[EvictionRecord] = []
    overrides: list[str] = []

    # ---------- ③ 裁剪（frozen 时整体跳过） ----------
    if store.frozen:
        p1_kept = sorted(
            p1_active,
            key=lambda e: (e.step_seq, e.created_at, e.entry_id),
            reverse=True,
        )
        p2_kept = list(p2_active)
    else:
        p1_kept, p1_cut = _cut_p1(p1_active, budget.capacity(ContextPartition.P1))
        cuts.extend(p1_cut)
        p2_kept, p2_cut, p2_overrides = _cut_p2(
            p2_active,
            profile=profile,
            capacity=budget.capacity(ContextPartition.P2),
            scope=s,
            goal=goal_text,
            referenced_ids=referenced_ids,
            recent_turns=recent_turns,
            drift_window=goal_drift_window,
        )
        cuts.extend(p2_cut)
        overrides.extend(p2_overrides)

    if persist_evictions:
        for rec in cuts:
            await store.demote(rec.entry_id, rec.reason)

    # ---------- ② 装配消息 ----------
    messages: list[BaseMessage] = list(p0_messages)
    included: list[str] = []

    if p1_kept:
        block = "\n\n---\n\n".join(e.content for e in p1_kept)
        messages.append(SystemMessage(content=f"【业务知识】\n\n{block}"))
        included.extend(e.entry_id for e in p1_kept)

    p2_rendered = sorted(
        p2_kept,
        key=lambda e: (
            e.turn_seq if e.turn_seq else e.step_seq,
            e.created_at,
            e.entry_id,
        ),
    )
    for e in p2_rendered:
        messages.append(_render_p2(e))
        included.append(e.entry_id)

    demoted_shown: list[str] = []
    if p2_demoted:
        ordered = sorted(p2_demoted, key=lambda e: (e.created_at, e.entry_id))
        head = ordered[:_TOMBSTONE_LIMIT]
        lines = [e.content for e in head]
        if len(ordered) > _TOMBSTONE_LIMIT:
            lines.append(f"其余 {len(ordered) - _TOMBSTONE_LIMIT} 条已归档")
        messages.append(SystemMessage(content="\n".join(lines)))
        demoted_shown = [e.entry_id for e in head]

    p0_tokens = sum(estimate_tokens(str(m.content)) for m in p0_messages)
    tombstone_tokens = (
        estimate_tokens("\n".join(e.content for e in p2_demoted[:_TOMBSTONE_LIMIT]))
        if p2_demoted
        else 0
    )
    report = AssemblyReport(
        profile=profile,
        policy_version=store.policy_version,
        p0_version=store.p0_version,
        per_partition_tokens={
            "P0": p0_tokens,
            "P1": _entry_tokens(p1_kept),
            "P2": _entry_tokens(p2_kept) + tombstone_tokens,
        },
        included=included,
        demoted_shown=demoted_shown,
        evicted_in_assembly=cuts,
        budget_overrides=overrides,
        frozen=store.frozen,
    )
    return AssemblyResult(messages=messages, report=report)


def _render_p2(e) -> BaseMessage:
    if e.entry_kind is EntryKind.CHAT_TURN:
        if e.role == "user":
            return HumanMessage(content=e.content)
        if e.role == "assistant":
            return AIMessage(content=e.content)
    return SystemMessage(content=e.content)


def _cut_p1(p1_active, capacity: int):
    """P1：按挂载新近度降序尾部截断（与 ops_b 不装箱哲学一致）。"""
    ranked = sorted(
        p1_active,
        key=lambda e: (e.step_seq, e.created_at, e.entry_id),
        reverse=True,
    )
    kept = []
    cuts: list[EvictionRecord] = []
    used = 0
    for e in ranked:
        if used + e.tokens_est <= capacity:
            kept.append(e)
            used += e.tokens_est
        else:
            cuts.append(EvictionRecord(entry_id=e.entry_id, reason="budget_cut"))
    return kept, cuts


def _cut_p2(
    p2_active,
    *,
    profile: ProfileName,
    capacity: int,
    scope: ExecScope,
    goal: str,
    referenced_ids: frozenset[str],
    recent_turns: int,
    drift_window: int,
):
    current_turn = scope.turn_seq or max(
        (e.turn_seq for e in p2_active if e.turn_seq), default=0
    )
    turn_floor = current_turn - recent_turns + 1

    def _is_protected(e) -> bool:
        return e.pinned or (
            e.entry_kind is EntryKind.CHAT_TURN
            and bool(e.turn_seq)
            and e.turn_seq >= turn_floor
        )

    protected = [e for e in p2_active if _is_protected(e)]
    candidates = [e for e in p2_active if not _is_protected(e)]
    window_contents = [e.content for e in protected]

    # CHAT 硬窗口：最近 K 轮之外的对话记录结构性出窗（不依赖预算/漂移启发式）
    hard_cut = [
        e for e in candidates
        if profile is ProfileName.CHAT and e.entry_kind is EntryKind.CHAT_TURN
    ]
    hard_cut_ids = {e.entry_id for e in hard_cut}
    candidates = [e for e in candidates if e.entry_id not in hard_cut_ids]

    order = eviction_order(
        candidates,
        current_step_seq=scope.step_seq,
        current_turn_seq=current_turn,
        goal=goal,
        referenced_ids=referenced_ids,
        window_contents=window_contents,
    )
    ranked_desc = [e for e, _ in reversed(order)]

    drift_ids = {
        e.entry_id
        for e in candidates
        if is_goal_drift(
            e,
            current_step_seq=scope.step_seq,
            current_turn_seq=current_turn,
            goal=goal,
            referenced_ids=referenced_ids,
            window_contents=window_contents,
            window=drift_window,
        )
    }

    cuts: list[EvictionRecord] = []
    cuts.extend(
        EvictionRecord(entry_id=eid, reason="chat_window") for eid in sorted(hard_cut_ids)
    )
    used = sum(e.tokens_est for e in protected)
    kept_extra = []
    truncated = False
    for e in ranked_desc:
        if e.entry_id in drift_ids:
            # T3 漂移独立归因，不触发尾部截断、不占预算
            cuts.append(EvictionRecord(entry_id=e.entry_id, reason="goal_drift"))
            continue
        if not truncated and used + e.tokens_est <= capacity:
            kept_extra.append(e)
            used += e.tokens_est
        else:
            # 尾部截断（不做装箱式跳项，对齐 ops_b.assemble 口径）：本条起全部出窗
            truncated = True
            cuts.append(EvictionRecord(entry_id=e.entry_id, reason="budget_cut"))

    kept = protected + kept_extra
    overrides = (
        [e.entry_id for e in protected if e.pinned]
        if used > capacity
        else []
    )
    return kept, cuts, overrides
