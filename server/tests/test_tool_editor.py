import asyncio
from pathlib import Path

import pytest

from tester_agent.tools.sandbox import SandboxError, WorkspaceSandbox
from tester_agent.tools.str_replace_editor import (
    TRUNCATED_MESSAGE,
    EditorError,
    run_str_replace_editor,
)


def test_view_file_with_line_numbers(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    (tmp_path / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    out = asyncio.run(run_str_replace_editor(sb, command="view", path="f.txt"))
    assert "     1  one" in out
    assert "     2  two" in out


def test_view_directory_two_levels(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "note.txt").write_text("x", encoding="utf-8")
    out = asyncio.run(run_str_replace_editor(sb, command="view", path="."))
    normalized = out.replace("\\", "/")
    assert "a/b" in normalized
    assert "note.txt" in out


def test_create_and_str_replace(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    asyncio.run(
        run_str_replace_editor(sb, command="create", path="n.txt", file_text="hello\n")
    )
    asyncio.run(
        run_str_replace_editor(
            sb, command="str_replace", path="n.txt", old_str="hello", new_str="world"
        )
    )
    assert (tmp_path / "n.txt").read_text(encoding="utf-8") == "world\n"


def test_str_replace_requires_unique_match(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    (tmp_path / "f.txt").write_text("aa\nbb\naa\n", encoding="utf-8")
    with pytest.raises(EditorError) as ei:
        asyncio.run(
            run_str_replace_editor(
                sb, command="str_replace", path="f.txt", old_str="aa", new_str="cc"
            )
        )
    assert "Multiple occurrences" in str(ei.value)


def test_insert(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    (tmp_path / "f.txt").write_text("a\nc\n", encoding="utf-8")
    asyncio.run(
        run_str_replace_editor(
            sb, command="insert", path="f.txt", insert_line=1, new_str="b"
        )
    )
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "a\nb\nc\n"


def test_snapshots_write_denied(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    (tmp_path / "t" / "snapshots").mkdir(parents=True)
    with pytest.raises(SandboxError):
        asyncio.run(
            run_str_replace_editor(
                sb, command="create", path="t/snapshots/x.txt", file_text="nope"
            )
        )


def test_view_truncation(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    (tmp_path / "big.txt").write_text("x" * 100, encoding="utf-8")
    out = asyncio.run(
        run_str_replace_editor(sb, command="view", path="big.txt", max_output_chars=40)
    )
    assert "<response clipped>" in out
    assert TRUNCATED_MESSAGE in out or "NOTE" in out
