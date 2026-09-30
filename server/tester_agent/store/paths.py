"""Workspace filesystem root resolution.

Default layout (empty ``root_dir``)::

    {data_dir}/workspaces/{workspace_id}/

Custom ``root_dir`` (absolute after normalize)::

    {root_dir}/   # contains {task_id}/… and reme/
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ValidationError


def normalize_root_dir(raw: str | None) -> str:
    """Normalize user input: blank → ``""``; else absolute resolved path string.

    Relative paths are rejected (ValidationError).
    """
    text = (raw or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValidationError(
            "root_dir 必须是绝对路径",
            details={"root_dir": text},
        )
    return str(path.resolve())


def resolve_workspace_root(
    data_dir: Path | str,
    workspace_id: str,
    root_dir: str | None = "",
) -> Path:
    """Resolve the on-disk root for a workspace.

    Empty ``root_dir`` → ``{data_dir}/workspaces/{workspace_id}``.
    Non-empty → that absolute path (already normalized or raw absolute).
    """
    custom = (root_dir or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return (Path(data_dir) / "workspaces" / workspace_id).resolve()
