"""Workspace path sandbox for builtin agent tools."""

from __future__ import annotations

from pathlib import Path


class SandboxError(ValueError):
    """Path escapes workspace root or hits a write-protected area."""


class WorkspaceSandbox:
    """Resolve and gate paths under a single workspace root."""

    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root.expanduser().resolve()

    def resolve(self, path: str) -> Path:
        raw = (path or "").strip()
        if not raw:
            raise SandboxError("path must be a non-empty string")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SandboxError(f"path escapes workspace root: {path}") from exc
        return resolved

    def assert_writable(self, path: Path) -> None:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SandboxError(f"path escapes workspace root: {path}") from exc
        if "snapshots" in resolved.parts:
            raise SandboxError(f"writes under snapshots/ are forbidden: {path}")

    def relpath(self, path: Path) -> str:
        resolved = path.resolve()
        return resolved.relative_to(self.root).as_posix()
