import asyncio
from pathlib import Path

from tester_agent.tools.registry import ToolBuildContext, build_case_designer_tools


def test_registry_tool_names(tmp_path: Path):
    tools = build_case_designer_tools(
        ToolBuildContext(
            owner_id="conv:1",
            workspace_root=tmp_path,
            runtime_config={"tool_bash_timeout_ms": 5000, "tool_max_output_chars": 8000},
        )
    )
    assert {t.name for t in tools} == {"bash", "str_replace_editor"}


def test_editor_tool_ainvoke(tmp_path: Path):
    (tmp_path / "f.txt").write_text("hi\n", encoding="utf-8")
    tools = build_case_designer_tools(
        ToolBuildContext(owner_id="c1", workspace_root=tmp_path, runtime_config={})
    )
    editor = next(t for t in tools if t.name == "str_replace_editor")
    out = asyncio.run(editor.ainvoke({"command": "view", "path": "f.txt"}))
    assert "hi" in out
