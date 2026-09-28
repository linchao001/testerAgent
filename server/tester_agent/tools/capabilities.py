"""Capability tools wrapping legacy stage logic (stubs + dispatch helpers)."""

from __future__ import annotations

import uuid
from typing import Any, Callable

from langchain_core.tools import StructuredTool

from ..domain import PlanStepKind


def _art_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


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


#: PlanStep.kind → capability tool name for direct dispatch
KIND_TO_CAPABILITY: dict[PlanStepKind, str] = {
    PlanStepKind.INTAKE_PARSE: "run_intake_parse",
    PlanStepKind.COVERAGE_DESIGN: "run_coverage_design",
    PlanStepKind.POINT_DESIGN: "run_point_design",
    PlanStepKind.CASE_GENERATE: "generate_cases_batch",
    PlanStepKind.REPAIR: "write_artifact",
}


def dispatch_capability(kind: PlanStepKind, *, tools: list | None = None) -> str:
    """Run the capability tool for ``kind`` synchronously; return artifact id string."""
    tool_list = tools if tools is not None else make_capability_tools()
    by_name = {t.name: t for t in tool_list}
    name = KIND_TO_CAPABILITY.get(kind)
    if name is None:
        return _art_id(kind.value)
    tool = by_name.get(name)
    if tool is None:
        return _art_id(kind.value)
    if kind == PlanStepKind.CASE_GENERATE:
        return tool.invoke({"point_ids": []})
    if kind == PlanStepKind.REPAIR:
        return tool.invoke({"kind": "repair", "payload_json": "{}"})
    return tool.invoke({})
