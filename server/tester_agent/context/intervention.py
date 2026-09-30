"""运行时干预执行器（spec §14 / plan WP-33）。

双通道（API / 会话工具）汇入唯一 ``execute(store, cmd)``：
selector 确定性解析 → 安全矩阵（P0 拒 / pinned forget 需 confirm /
终态写拒）→ store 状态动作 + journal ``reason=manual:*``。

叶子层：不 import runtime/graph/store；幂等与 SSE 在 API 边界处理。
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..errors import AppError, TaskStateConflict, ValidationError
from .budget import ProfileBudget, validate_budget
from .models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryStatus,
    ProfileName,
)
from .store import ContextStore

SourceResolver = Callable[[ContextEntry], str | None | Awaitable[str | None]]

_WRITE_ACTIONS: frozenset[str] = frozenset(
    {
        "pin",
        "unpin",
        "forget",
        "refresh",
        "set_goal",
        "budget",
        "freeze",
        "unfreeze",
        "recall",
    }
)
_SELECTOR_PREFIXES = frozenset(
    {"id", "kind", "step", "recent", "item", "batch", "all"}
)
_RECENT_RE = re.compile(r"^\d+$")


class ContextCommandError(ValidationError):
    """干预拒识（命中 0/多、P0、非法 selector）→ 422。"""

    code = "VALIDATION_CONTEXT"
    http_status = 422


class ConfirmRequiredError(AppError):
    """pinned forget 需二次确认。"""

    code = "CONFIRM_REQUIRED"
    http_status = 422


class ContextAction(StrEnum):
    PIN = "pin"
    UNPIN = "unpin"
    FORGET = "forget"
    REFRESH = "refresh"
    SET_GOAL = "set_goal"
    BUDGET = "budget"
    SHOW = "show"
    RECALL = "recall"
    FREEZE = "freeze"
    UNFREEZE = "unfreeze"


class ContextCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ContextAction
    selector: str = ""
    arg: str | None = None
    confirm: bool = False
    reason: str | None = None


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    action: str
    selector: str = ""
    affected: list[str] = Field(default_factory=list)
    affected_meta: dict[str, str] = Field(default_factory=dict)
    entries: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ""


def parse_selector(selector: str) -> tuple[str, str | None]:
    """解析 ``id:|kind:|step:|recent:|item:|batch:|all``；失败 → 422。"""
    raw = (selector or "").strip()
    if not raw:
        raise ContextCommandError(
            "selector 不能为空",
            details={"selector": selector},
        )
    if raw == "all":
        return "all", None
    if ":" not in raw:
        raise ContextCommandError(
            f"非法 selector：{raw}",
            details={"selector": raw, "candidates": _near_kinds(raw)},
        )
    prefix, _, rest = raw.partition(":")
    prefix = prefix.strip().lower()
    rest = rest.strip()
    if prefix not in _SELECTOR_PREFIXES or prefix == "all":
        raise ContextCommandError(
            f"非法 selector 前缀：{prefix}",
            details={"selector": raw, "candidates": _near_kinds(raw)},
        )
    if not rest and prefix != "all":
        raise ContextCommandError(
            f"selector 缺少取值：{raw}",
            details={"selector": raw},
        )
    if prefix == "recent" and not _RECENT_RE.match(rest):
        raise ContextCommandError(
            f"recent 须为正整数：{rest}",
            details={"selector": raw},
        )
    return prefix, rest


def resolve_entries(store: ContextStore, selector: str) -> list[ContextEntry]:
    kind, value = parse_selector(selector)
    entries = store.entries()
    if kind == "all":
        return list(entries)
    if kind == "id":
        try:
            return [store.get(value or "")]
        except KeyError:
            return []
    if kind == "kind":
        return [e for e in entries if e.entry_kind.value == value]
    if kind == "step":
        return [e for e in entries if e.step_id == value]
    if kind == "item":
        return [e for e in entries if e.item_key == value]
    if kind == "batch":
        return [e for e in entries if e.batch_id == value]
    # recent:n — 按 (created_at, entry_id) 降序取 n（跳过 P0）
    n = int(value or "0")
    non_p0 = [e for e in entries if e.partition is not ContextPartition.P0]
    ordered = sorted(
        non_p0, key=lambda e: (e.created_at, e.entry_id), reverse=True
    )
    return ordered[:n]


async def execute(
    store: ContextStore,
    cmd: ContextCommand,
    *,
    operator: str = "api",
    source_resolver: SourceResolver | None = None,
    model_window: int = 128_000,
) -> CommandResult:
    """确定性执行干预指令；operator 仅记账，不参与裁决。"""
    del operator  # journal reason 用 manual:*；operator 预留给 API 审计扩展
    action = cmd.action
    if store.closed and action.value in _WRITE_ACTIONS:
        raise TaskStateConflict(
            "上下文已关闭，拒绝写干预",
            details={"owner_id": store.owner_id, "action": action.value},
        )

    manual_reason = f"manual:{cmd.reason or action.value}"

    if action is ContextAction.SHOW:
        hits = resolve_entries(store, cmd.selector or "all")
        return CommandResult(
            action=action.value,
            selector=cmd.selector,
            affected=[e.entry_id for e in hits],
            entries=[_entry_brief(e) for e in hits],
            message=f"show {len(hits)} entries",
        )

    if action is ContextAction.SET_GOAL:
        text = (cmd.arg or "").strip()
        if not text:
            raise ContextCommandError("set_goal 需要 arg 文本")
        eid = await store.set_goal(text, reason=manual_reason)
        return CommandResult(
            action=action.value,
            affected=[eid],
            message="goal updated",
        )

    if action is ContextAction.BUDGET:
        budget, profile = _parse_budget_arg(cmd.arg)
        validate_budget(budget, model_window=model_window)
        store.budget_overrides[profile] = budget
        await store.note_policy(
            reason=manual_reason,
            digest=f"budget {profile.value} p0={budget.p0} p1={budget.p1} p2={budget.p2}",
        )
        return CommandResult(
            action=action.value,
            affected=[profile.value],
            message=f"budget override {profile.value}",
        )

    if action is ContextAction.FREEZE:
        store.frozen = True
        await store.note_policy(reason=manual_reason, digest="frozen")
        return CommandResult(action=action.value, message="frozen")

    if action is ContextAction.UNFREEZE:
        store.frozen = False
        await store.note_policy(reason=manual_reason, digest="unfrozen")
        return CommandResult(action=action.value, message="unfrozen")

    if action is ContextAction.RECALL:
        # 一期：执行器侧仅登记意图；实际 retrieve_pipeline 由上层注入回调（可选）
        return CommandResult(
            action=action.value,
            selector=cmd.selector,
            message="recall deferred to caller",
        )

    # 以下均需 selector
    if not (cmd.selector or "").strip():
        raise ContextCommandError(f"{action.value} 需要 selector")

    hits = resolve_entries(store, cmd.selector)
    if not hits:
        raise ContextCommandError(
            f"selector 命中 0 条：{cmd.selector}",
            details={
                "selector": cmd.selector,
                "candidates": _near_candidates(store, cmd.selector),
            },
        )

    if any(e.partition is ContextPartition.P0 for e in hits):
        raise ContextCommandError(
            "P0 条目不可干预",
            details={
                "selector": cmd.selector,
                "entry_ids": [e.entry_id for e in hits if e.partition is ContextPartition.P0],
            },
        )

    if action is ContextAction.FORGET:
        pinned = [e for e in hits if e.pinned]
        if pinned and not cmd.confirm:
            raise ConfirmRequiredError(
                "遗忘 pinned 条目需要 confirm=true",
                details={
                    "candidates": [_entry_brief(e) for e in pinned],
                    "selector": cmd.selector,
                },
            )
        affected: list[str] = []
        for e in hits:
            if e.status is EntryStatus.ACTIVE:
                await store.demote(e.entry_id, manual_reason)
                affected.append(e.entry_id)
            elif e.status is EntryStatus.DEMOTED:
                await store.evict(e.entry_id, manual_reason)
                affected.append(e.entry_id)
        return CommandResult(
            action=action.value,
            selector=cmd.selector,
            affected=affected,
            message=f"forget {len(affected)}",
        )

    if action is ContextAction.PIN:
        affected = []
        for e in hits:
            if e.status is EntryStatus.ACTIVE:
                await store.pin(e.entry_id, reason=manual_reason)
                affected.append(e.entry_id)
        return CommandResult(
            action=action.value, selector=cmd.selector, affected=affected
        )

    if action is ContextAction.UNPIN:
        affected = []
        for e in hits:
            await store.unpin(e.entry_id, reason=manual_reason)
            affected.append(e.entry_id)
        return CommandResult(
            action=action.value, selector=cmd.selector, affected=affected
        )

    if action is ContextAction.REFRESH:
        affected = []
        meta: dict[str, str] = {}
        for e in hits:
            content = await _resolve_content(e, source_resolver)
            if content is None:
                meta[e.entry_id] = "source_missing"
                affected.append(e.entry_id)
                continue
            await store.reactivate(
                e.entry_id, content=content, reason=manual_reason
            )
            affected.append(e.entry_id)
        return CommandResult(
            action=action.value,
            selector=cmd.selector,
            affected=affected,
            affected_meta=meta,
            message=f"refresh {len(affected)}",
        )

    raise ContextCommandError(f"未支持的 action：{action.value}")


async def _resolve_content(
    entry: ContextEntry, resolver: SourceResolver | None
) -> str | None:
    if resolver is None:
        return None
    out = resolver(entry)
    if hasattr(out, "__await__"):
        return await out  # type: ignore[misc]
    return out  # type: ignore[return-value]


def _parse_budget_arg(arg: str | None) -> tuple[ProfileBudget, ProfileName]:
    if not arg or not str(arg).strip():
        raise ContextCommandError("budget 需要 arg JSON")
    try:
        data = json.loads(arg)
    except json.JSONDecodeError as exc:
        raise ContextCommandError(
            "budget arg 须为 JSON",
            details={"arg": arg},
        ) from exc
    try:
        profile = ProfileName(str(data.get("profile", "chat")))
        budget = ProfileBudget(
            p0=int(data["p0"]), p1=int(data["p1"]), p2=int(data["p2"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContextCommandError(
            "budget arg 须含 profile/p0/p1/p2",
            details={"arg": arg},
        ) from exc
    return budget, profile


def _entry_brief(e: ContextEntry) -> dict[str, Any]:
    return {
        "entry_id": e.entry_id,
        "entry_kind": e.entry_kind.value,
        "partition": e.partition.value,
        "status": e.status.value,
        "pinned": e.pinned,
        "digest": e.digest,
    }


def _near_kinds(hint: str) -> list[str]:
    h = hint.lower()
    out = []
    for k in EntryKind:
        if h in k.value or k.value in h:
            out.append(f"kind:{k.value}")
    return out[:8]


def _near_candidates(store: ContextStore, selector: str) -> list[dict[str, Any]]:
    """拼写邻近：按 kind 前缀或 entry_id 子串给出候选。"""
    raw = selector.strip()
    needle = raw.split(":", 1)[-1].lower() if ":" in raw else raw.lower()
    hits = []
    for e in store.entries():
        if needle and (
            needle in e.entry_id.lower()
            or needle in e.entry_kind.value
            or needle in (e.digest or "").lower()
        ):
            hits.append(_entry_brief(e))
        if len(hits) >= 8:
            break
    return hits
