"""控制环 T1 钩子（plan §6.2）：step/plan/reflect/confirm → ContextStore。

纯异步函数，store 由调用方注入；store 为 None 时调用方应跳过（开关/旧夹具）。
"""

from __future__ import annotations

from typing import Any

from ...context._time import utcnow_iso
from ...context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryRefs,
    EntryStatus,
    Phase,
)
from ...context.policy import programmatic_digest
from ...context.tokens import estimate_tokens
from ...domain import AgentPlan, PlanStep, PlanStepKind
from ..wrap import map_phase

_OUTLINE_KINDS = frozenset(
    {
        "coverage_design",
        "link_identify",
        PlanStepKind.COVERAGE_DESIGN.value,
    }
)


def _step_window(runtime_config: dict | None) -> int:
    cfg = runtime_config or {}
    try:
        return max(0, int(cfg.get("context.step_window", 1)))
    except (TypeError, ValueError):
        return 1


async def on_step_completed(
    store,
    *,
    step: PlanStep,
    step_seq: int,
    artifact_id: str,
    artifact_kind: str,
    payload: Any,
    step_window: int = 1,
) -> None:
    """step 结束：窗口外 P1 demote + 挂 ARTIFACT_DIGEST。"""
    cutoff = step_seq - step_window
    await store.bulk_demote(
        lambda e: (
            e.partition is ContextPartition.P1
            and e.status is EntryStatus.ACTIVE
            and e.step_seq <= cutoff
        ),
        "step_window",
    )
    phase = map_phase(step.kind) or Phase.SHARED
    summary = _payload_summary(artifact_kind, payload, artifact_id)
    await store.append(
        ContextEntry(
            entry_id=f"artifact:{artifact_id}",
            partition=ContextPartition.P2,
            entry_kind=EntryKind.ARTIFACT_DIGEST,
            content=summary,
            digest=programmatic_digest(summary),
            tokens_est=estimate_tokens(summary),
            refs=EntryRefs(payload_ref=artifact_id),
            phase=phase if phase is not Phase.SHARED else Phase.DESIGN,
            step_id=step.step_id,
            step_seq=step_seq,
            created_at=utcnow_iso(),
        )
    )


async def on_plan_updated(
    store,
    *,
    plan: AgentPlan,
    plan_artifact_id: str,
) -> None:
    """replan/新 plan：旧 PLAN demote(superseded)，新 PLAN pinned。"""
    await store.bulk_demote(
        lambda e: (
            e.entry_kind is EntryKind.PLAN and e.status is EntryStatus.ACTIVE
        ),
        "superseded",
    )
    text = plan.goal or plan.plan_id
    await store.append(
        ContextEntry(
            entry_id=f"artifact:{plan_artifact_id}",
            partition=ContextPartition.P2,
            entry_kind=EntryKind.PLAN,
            content=text,
            digest=programmatic_digest(text),
            tokens_est=estimate_tokens(text),
            pinned=True,
            refs=EntryRefs(payload_ref=plan_artifact_id),
            phase=Phase.SHARED,
            created_at=utcnow_iso(),
        )
    )


async def on_reflect_decision(
    store,
    *,
    step: PlanStep,
    decision: str,
    reflection_count: int,
) -> None:
    """reflect：append REFLECTION；repair 时归档同 step 旧 reflection。"""
    if decision == "repair":
        await store.bulk_demote(
            lambda e: (
                e.entry_kind is EntryKind.REFLECTION
                and e.step_id == step.step_id
                and e.status is EntryStatus.ACTIVE
            ),
            "superseded",
        )
    text = f"{decision}:{step.step_id}"
    await store.append(
        ContextEntry(
            entry_id=f"refl:{step.step_id}:{reflection_count}",
            partition=ContextPartition.P2,
            entry_kind=EntryKind.REFLECTION,
            content=text,
            digest=programmatic_digest(text),
            tokens_est=estimate_tokens(text),
            step_id=step.step_id,
            created_at=utcnow_iso(),
            refs=EntryRefs(),
        )
    )


async def on_await_human_confirmed(
    store,
    *,
    artifact_id: str,
    artifact_kind: str,
    payload: Any,
) -> None:
    """人工确认：pin 对应 digest；大纲类另写 SHARED OUTLINE_DIGEST。"""
    eid = f"artifact:{artifact_id}"
    if any(e.entry_id == eid for e in store.entries()):
        entry = store.get(eid)
        if entry.status is EntryStatus.ACTIVE and not entry.pinned:
            await store.pin(eid, reason="human_confirmed")
    else:
        summary = _payload_summary(artifact_kind, payload, artifact_id)
        await store.append(
            ContextEntry(
                entry_id=eid,
                partition=ContextPartition.P2,
                entry_kind=EntryKind.ARTIFACT_DIGEST,
                content=summary,
                digest=programmatic_digest(summary),
                tokens_est=estimate_tokens(summary),
                pinned=True,
                refs=EntryRefs(payload_ref=artifact_id),
                phase=Phase.DESIGN,
                created_at=utcnow_iso(),
            )
        )

    kind = (artifact_kind or "").lower()
    if kind not in _OUTLINE_KINDS:
        return
    await store.bulk_demote(
        lambda e: (
            e.entry_kind is EntryKind.OUTLINE_DIGEST
            and e.status is EntryStatus.ACTIVE
        ),
        "superseded",
    )
    summary = _outline_summary(payload, artifact_id)
    await store.append(
        ContextEntry(
            entry_id=f"outline:{artifact_id}",
            partition=ContextPartition.P2,
            entry_kind=EntryKind.OUTLINE_DIGEST,
            content=summary,
            digest=programmatic_digest(summary),
            tokens_est=estimate_tokens(summary),
            pinned=True,
            refs=EntryRefs(payload_ref=artifact_id),
            phase=Phase.SHARED,
            created_at=utcnow_iso(),
        )
    )


def runtime_config_of(ctx: Any) -> dict:
    if ctx is None:
        return {}
    cfg = getattr(ctx, "runtime_config", None)
    if isinstance(cfg, dict):
        return cfg
    app = getattr(ctx, "app", None)
    if app is not None:
        rc = getattr(app, "runtime_config", None)
        if isinstance(rc, dict):
            return rc
    return {}


def step_window_of(ctx: Any) -> int:
    return _step_window(runtime_config_of(ctx))


def _payload_summary(kind: str, payload: Any, art_id: str) -> str:
    if isinstance(payload, dict):
        for key in ("title", "goal", "summary"):
            if payload.get(key):
                return str(payload[key])
        stories = payload.get("stories")
        if isinstance(stories, list) and stories:
            titles = [
                str(s.get("title") or s.get("id") or "")
                for s in stories[:5]
                if isinstance(s, dict)
            ]
            titles = [t for t in titles if t]
            if titles:
                return "；".join(titles)
    return f"{kind or 'artifact'} #{art_id}"


def _outline_summary(payload: Any, art_id: str) -> str:
    return "大纲：" + _payload_summary("outline", payload, art_id)
