"""Capability tools wrapping stage node functions for the control loop."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from langchain_core.tools import StructuredTool

from ..domain import PlanStepKind
from ..graph.constants import (
    STAGE_CASE_GENERATE,
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from ..graph.wrap import NodeFn

# PlanStep.kind → stage_artifact.stage / get_active lookup key
KIND_TO_STAGE: dict[PlanStepKind, str] = {
    PlanStepKind.COVERAGE_DESIGN: STAGE_LINK_IDENTIFY,
    PlanStepKind.POINT_DESIGN: STAGE_POINT_WRITE,
    PlanStepKind.CASE_GENERATE: STAGE_CASE_GENERATE,
    PlanStepKind.REPAIR: STAGE_CASE_GENERATE,
}

STAGE_TO_KIND: dict[str, PlanStepKind] = {
    STAGE_INTAKE: PlanStepKind.INTAKE_PARSE,
    STAGE_LINK_IDENTIFY: PlanStepKind.COVERAGE_DESIGN,
    STAGE_POINT_WRITE: PlanStepKind.POINT_DESIGN,
    STAGE_CASE_GENERATE: PlanStepKind.CASE_GENERATE,
}


def caps_from_stage_nodes(nodes: Mapping[str, NodeFn]) -> dict[PlanStepKind, NodeFn]:
    """Map stage-name node dict → PlanStepKind caps for control graph tests."""
    out: dict[PlanStepKind, NodeFn] = {}
    for stage, fn in nodes.items():
        kind = STAGE_TO_KIND.get(stage)
        if kind is not None:
            out[kind] = fn
    return out


# Control-loop artifact.kind labels (spec §5.1)
KIND_TO_ARTIFACT_KIND: dict[PlanStepKind, str] = {
    PlanStepKind.INTAKE_PARSE: "clauses",
    PlanStepKind.COVERAGE_DESIGN: "coverage_design",
    PlanStepKind.POINT_DESIGN: "point_plan",
    PlanStepKind.CASE_GENERATE: "case_set",
    PlanStepKind.REPAIR: "case_set",
}

# State keys mirrored from confirmed artifact payloads
KIND_TO_STATE_KEY: dict[PlanStepKind, str] = {
    PlanStepKind.INTAKE_PARSE: "clauses",
    PlanStepKind.COVERAGE_DESIGN: "link_plan",
    PlanStepKind.POINT_DESIGN: "point_plan",
    PlanStepKind.CASE_GENERATE: "case_batch",
}


@dataclass
class CapabilityOutcome:
    artifact_id: str
    artifact_kind: str
    increment: dict[str, Any] = field(default_factory=dict)
    payload: Any = None
    version: int = 1


def _art_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def legacy_state_from_artifact(kind: PlanStepKind, payload: Any) -> dict[str, Any]:
    """Map confirmed artifact payload back onto legacy TaskState fields."""
    key = KIND_TO_STATE_KEY.get(kind)
    if key is None or payload is None:
        return {}
    return {key: payload}


def hydrate_state_from_artifacts(state: Mapping[str, Any]) -> dict[str, Any]:
    """Ensure link_plan / point_plan / clauses exist for downstream nodes."""
    out = dict(state)
    artifacts = out.get("artifacts") or {}
    plan_raw = out.get("agent_plan")
    if not plan_raw:
        return out
    from ..domain import AgentPlan

    plan = AgentPlan.model_validate(plan_raw)
    for step in plan.steps:
        if step.status != "done" or not step.output_ref:
            continue
        art = artifacts.get(step.output_ref) or {}
        payload = art.get("payload")
        if payload is None:
            continue
        for k, v in legacy_state_from_artifact(step.kind, payload).items():
            if out.get(k) is None:
                out[k] = v
    return out


def default_nodes() -> dict[PlanStepKind, NodeFn]:
    """Lazy import of production stage nodes (avoid circular imports at module load)."""
    from ..graph.nodes.case_generate import case_generate_node
    from ..graph.nodes.intake import intake_node
    from ..graph.nodes.link_identify import link_identify_node
    from ..graph.nodes.point_write import point_write_node

    caps = caps_from_stage_nodes(
        {
            STAGE_INTAKE: intake_node,
            STAGE_LINK_IDENTIFY: link_identify_node,
            STAGE_POINT_WRITE: point_write_node,
            STAGE_CASE_GENERATE: case_generate_node,
        }
    )
    # repair reuses case_generate path until dedicated repair exists
    caps[PlanStepKind.REPAIR] = case_generate_node
    return caps


def _payload_from_increment(kind: PlanStepKind, increment: dict[str, Any]) -> Any:
    if kind == PlanStepKind.INTAKE_PARSE:
        return increment.get("clauses")
    if kind == PlanStepKind.COVERAGE_DESIGN:
        return increment.get("link_plan")
    if kind == PlanStepKind.POINT_DESIGN:
        return increment.get("point_plan")
    if kind in (PlanStepKind.CASE_GENERATE, PlanStepKind.REPAIR):
        return {
            "case_count": increment.get("case_count"),
            "case_ids": increment.get("case_ids") or [],
        }
    return increment


async def _resolve_artifact_id(
    kind: PlanStepKind,
    ctx: Any,
    increment: dict[str, Any],
) -> tuple[str, int]:
    """Prefer DB active stage_artifact id; fall back to synthetic id."""
    stage = KIND_TO_STAGE.get(kind)
    art_kind = KIND_TO_ARTIFACT_KIND.get(kind, kind.value)
    version = 1
    versions = increment.get("current_stage_version") or {}
    if stage and isinstance(versions.get(stage), int):
        version = versions[stage]

    daos = getattr(ctx, "daos", None)
    artifact_dao = getattr(daos, "artifact", None) if daos is not None else None
    task = getattr(ctx, "task", None)
    if artifact_dao is not None and task is not None and stage:
        row = await artifact_dao.get_active(task.id, stage)
        if row is not None:
            return row.id, int(getattr(row, "stage_version", None) or version)

    return _art_id(art_kind), version


async def invoke_capability(
    kind: PlanStepKind,
    *,
    ctx: Any | None,
    state: Mapping[str, Any],
    nodes: Mapping[PlanStepKind, NodeFn] | None = None,
) -> CapabilityOutcome:
    """Run the stage node for ``kind`` (or stub when ``ctx`` is absent)."""
    art_kind = KIND_TO_ARTIFACT_KIND.get(kind, kind.value)
    if ctx is None:
        return CapabilityOutcome(
            artifact_id=_art_id(art_kind),
            artifact_kind=art_kind,
            increment={},
            payload=None,
            version=1,
        )

    node_map = dict(default_nodes() if nodes is None else nodes)
    node = node_map.get(kind)
    if node is None:
        return CapabilityOutcome(
            artifact_id=_art_id(art_kind),
            artifact_kind=art_kind,
            increment={},
            payload=None,
            version=1,
        )

    hydrated = hydrate_state_from_artifacts(state)
    # 防线 1：生产路径不经 wrap，在此压入 ExecScope（与 wrap 共用映射表）
    from ..graph.wrap import resolve_scope_kwargs
    from ..context.scopes import scope as exec_scope

    async with exec_scope(**resolve_scope_kwargs(hydrated, kind=kind)):
        increment = await node(ctx, hydrated)
    if not isinstance(increment, dict):
        increment = {}
    art_id, version = await _resolve_artifact_id(kind, ctx, increment)
    payload = _payload_from_increment(kind, increment)
    return CapabilityOutcome(
        artifact_id=art_id,
        artifact_kind=art_kind,
        increment=increment,
        payload=payload,
        version=version,
    )


def make_capability_tools(*, runner: Callable[..., Any] | None = None) -> list:
    """Return LangChain tools for main-agent capability set.

    ``runner`` optional hook for real node functions; default returns stub artifact ids.
    """

    def _stub(name: str):
        def _run(**kwargs: Any) -> str:
            if runner is not None:
                return str(runner(name, **kwargs))
            return _art_id(name)

        return _run

    return [
        StructuredTool.from_function(
            name="retrieve_kb",
            description="Retrieve knowledge-base passages for the current task.",
            func=_stub("retrieve_kb"),
        ),
        StructuredTool.from_function(
            name="run_intake_parse",
            description="Parse requirement into clauses artifact.",
            func=_stub("run_intake_parse"),
        ),
        StructuredTool.from_function(
            name="run_coverage_design",
            description="Produce coverage / link design artifact.",
            func=_stub("run_coverage_design"),
        ),
        StructuredTool.from_function(
            name="run_point_design",
            description="Produce test-point plan artifact.",
            func=_stub("run_point_design"),
        ),
        StructuredTool.from_function(
            name="generate_cases_batch",
            description="Generate cases for given point_ids.",
            func=_stub("generate_cases_batch"),
        ),
        StructuredTool.from_function(
            name="build_coverage_matrix",
            description="Build programmatic coverage matrix.",
            func=_stub("build_coverage_matrix"),
        ),
        StructuredTool.from_function(
            name="write_artifact",
            description="Write a structured artifact payload JSON.",
            func=lambda kind, payload_json: _art_id(f"write-{kind}"),
        ),
    ]
