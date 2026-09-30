"""工作区数据根目录解析（root_dir 空=默认 / 非空=绝对路径）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.errors import ValidationError
from tester_agent.store.paths import normalize_root_dir, resolve_workspace_root


def test_resolve_default_under_data_workspaces(tmp_path: Path):
    data = tmp_path / "data"
    root = resolve_workspace_root(data, "ws-abc", "")
    assert root == (data / "workspaces" / "ws-abc").resolve()


def test_resolve_custom_absolute(tmp_path: Path):
    data = tmp_path / "data"
    custom = (tmp_path / "my-ws").resolve()
    root = resolve_workspace_root(data, "ws-abc", str(custom))
    assert root == custom


def test_normalize_empty_stays_empty():
    assert normalize_root_dir("") == ""
    assert normalize_root_dir("  ") == ""


def test_normalize_rejects_relative(tmp_path: Path):
    with pytest.raises(ValidationError):
        normalize_root_dir("relative/path")


def test_normalize_absolute_expands_and_resolves(tmp_path: Path):
    target = tmp_path / "custom"
    target.mkdir()
    out = normalize_root_dir(str(target))
    assert Path(out).is_absolute()
    assert Path(out) == target.resolve()
