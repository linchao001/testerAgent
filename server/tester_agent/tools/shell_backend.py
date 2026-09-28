"""Detect bash or PowerShell for the persistent shell tool."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


class ShellBackendError(RuntimeError):
    """No usable shell backend found."""


def detect_shell_backend(preference: str = "auto") -> tuple[str, list[str]]:
    """Return (kind, argv) where kind is ``bash`` or ``pwsh``.

    preference: ``auto`` | ``bash`` | ``pwsh``
    """
    pref = (preference or "auto").strip().lower()
    if pref not in {"auto", "bash", "pwsh"}:
        raise ShellBackendError(f"unknown tool_shell_backend: {preference!r}")

    if pref in {"auto", "bash"}:
        bash = _find_bash()
        if bash is not None:
            return "bash", [bash, "--noprofile", "--norc"]
        if pref == "bash":
            raise ShellBackendError("bash not found on PATH / Git install")

    if pref in {"auto", "pwsh"}:
        pwsh = _find_pwsh()
        if pwsh is not None:
                return "pwsh", [pwsh, "-NoLogo", "-NoProfile"]
        if pref == "pwsh":
            raise ShellBackendError("pwsh/powershell not found")

    raise ShellBackendError("no bash or pwsh available")


def _find_bash() -> str | None:
    env = os.environ.get("BASH") or os.environ.get("TESTER_AGENT_BASH")
    if env and Path(env).is_file():
        return env
    which = shutil.which("bash")
    if which:
        return which
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def _find_pwsh() -> str | None:
    for name in ("pwsh", "powershell"):
        which = shutil.which(name)
        if which:
            return which
    return None
