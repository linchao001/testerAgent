"""Capability tool registry flags for Plan-Execute execute_step."""

from pathlib import Path

from tester_agent.tools.registry import ToolBuildContext, build_case_designer_tools


def test_capabilities_not_in_default_chat_tools(tmp_path: Path):
    tools = build_case_designer_tools(
        ToolBuildContext(owner_id="c:1", workspace_root=tmp_path, runtime_config={})
    )
    names = {t.name for t in tools}
    assert "bash" in names
    assert "run_coverage_design" not in names
    assert "generate_cases_batch" not in names
    assert "spawn_subtask" not in names


def test_capabilities_included_when_flagged(tmp_path: Path):
    tools = build_case_designer_tools(
        ToolBuildContext(owner_id="t:1", workspace_root=tmp_path, runtime_config={}),
        include_capabilities=True,
    )
    names = {t.name for t in tools}
    assert "generate_cases_batch" in names
    assert "run_coverage_design" in names
    assert "build_coverage_matrix" in names
