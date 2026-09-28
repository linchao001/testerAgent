from pathlib import Path

import pytest

from tester_agent.tools.sandbox import SandboxError, WorkspaceSandbox


def test_resolve_relative_under_root(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    (tmp_path / "a").mkdir()
    p = sb.resolve("a/b.txt")
    assert p == (tmp_path / "a" / "b.txt").resolve()


def test_reject_escape(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    with pytest.raises(SandboxError):
        sb.resolve("../outside.txt")


def test_snapshots_not_writable(tmp_path: Path):
    sb = WorkspaceSandbox(tmp_path)
    target = tmp_path / "t1" / "snapshots" / "x.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    with pytest.raises(SandboxError):
        sb.assert_writable(sb.resolve("t1/snapshots/x.jsonl"))
