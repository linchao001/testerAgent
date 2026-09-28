"""Owner-scoped persistent shell (bash or pwsh) with marker-based capture."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .sandbox import WorkspaceSandbox
from .shell_backend import ShellBackendError, detect_shell_backend
from .str_replace_editor import maybe_truncate


@dataclass
class _Session:
    proc: asyncio.subprocess.Process
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    kind: str = "bash"


class PersistentBashManager:
    """One long-lived shell per owner_id; commands serialized per owner."""

    def __init__(self, backend: tuple[str, list[str]] | None = None) -> None:
        if backend is None:
            backend = detect_shell_backend("auto")
        self.kind, self.argv = backend
        self._sessions: dict[str, _Session] = {}
        self._global = asyncio.Lock()

    async def run(
        self,
        owner_id: str,
        command: str,
        *,
        cwd: Path,
        timeout_ms: int,
        max_output_chars: int,
        cancel: asyncio.Event | None = None,
    ) -> str:
        if not command or not command.strip():
            raise ValueError("command must be a non-empty string")
        session = await self._ensure(owner_id, cwd)
        async with session.lock:
            try:
                return await self._exec(
                    session,
                    command,
                    timeout_ms=timeout_ms,
                    max_output_chars=max_output_chars,
                    cancel=cancel,
                )
            except Exception:
                await self.reset(owner_id, "command failed")
                raise

    async def reset(self, owner_id: str, reason: str) -> None:
        async with self._global:
            session = self._sessions.pop(owner_id, None)
        if session is None:
            return
        await self._kill(session)

    async def _ensure(self, owner_id: str, cwd: Path) -> _Session:
        async with self._global:
            existing = self._sessions.get(owner_id)
            if existing is not None and existing.proc.returncode is None:
                return existing
            if existing is not None:
                await self._kill(existing)
            proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd.resolve()),
            )
            session = _Session(proc=proc, kind=self.kind)
            self._sessions[owner_id] = session
            return session

    async def _kill(self, session: _Session) -> None:
        if session.proc.returncode is not None:
            return
        try:
            session.proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(session.proc.wait(), timeout=2)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    async def _exec(
        self,
        session: _Session,
        command: str,
        *,
        timeout_ms: int,
        max_output_chars: int,
        cancel: asyncio.Event | None,
    ) -> str:
        assert session.proc.stdin and session.proc.stdout
        nonce = uuid.uuid4().hex
        start = f"__TA_BASH_START_{nonce}__"
        end = f"__TA_BASH_END_{nonce}:"
        wrapped = self._wrap(command, start, end, session.kind)
        session.proc.stdin.write((wrapped + "\n").encode("utf-8"))
        await session.proc.stdin.drain()

        timeout_s = max(timeout_ms, 1) / 1000.0
        buf = bytearray()
        deadline = asyncio.get_running_loop().time() + timeout_s

        while True:
            if cancel is not None and cancel.is_set():
                raise TimeoutError("persistent bash command aborted")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("persistent bash command timed out")
            try:
                chunk = await asyncio.wait_for(session.proc.stdout.read(4096), timeout=min(0.25, remaining))
            except asyncio.TimeoutError:
                text = buf.decode("utf-8", errors="replace")
                if end in text:
                    break
                continue
            if not chunk:
                if session.proc.returncode is not None:
                    raise RuntimeError("persistent bash shell exited")
                await asyncio.sleep(0.01)
                continue
            buf.extend(chunk)
            text = buf.decode("utf-8", errors="replace")
            if end in text and self._status_ready(text, end):
                break

        text = buf.decode("utf-8", errors="replace")
        body, code = self._extract(text, start, end)
        out = maybe_truncate(body, max_output_chars)
        if code != 0:
            return f"{out}\n\n[exit_code={code}]"
        return out

    @staticmethod
    def _status_ready(text: str, end: str) -> bool:
        idx = text.rfind(end)
        if idx < 0:
            return False
        rest = text[idx + len(end) :]
        return bool(rest) and rest[0].isdigit()

    @staticmethod
    def _extract(text: str, start: str, end: str) -> tuple[str, int]:
        end_idx = text.rfind(end)
        if end_idx < 0:
            return text, 1
        after = text[end_idx + len(end) :]
        # status then newline
        status_line = after.splitlines()[0] if after else "1"
        try:
            code = int(status_line.strip())
        except ValueError:
            code = 1
        start_idx = text.rfind(start, 0, end_idx)
        if start_idx < 0:
            body = text[:end_idx]
        else:
            body = text[start_idx + len(start) : end_idx]
        body = body.lstrip("\r\n")
        if body.endswith("\n"):
            body = body[:-1]
        return body, code

    @staticmethod
    def _wrap(command: str, start: str, end: str, kind: str) -> str:
        if kind == "bash":
            # single physical line; quote via $'...'
            def q(s: str) -> str:
                esc = (
                    s.replace("\\", "\\\\")
                    .replace("'", "\\'")
                    .replace("\r", "\\r")
                    .replace("\n", "\\n")
                )
                return f"$'{esc}'"

            return (
                f"printf '%s\\n' {q(start)}; eval -- {q(command)}; "
                f"__ta_status=$?; printf '%s%s\\n' {q(end)} \"$__ta_status\""
            )
        # PowerShell
        def pq(s: str) -> str:
            return "'" + s.replace("'", "''") + "'"

        return (
            f"Write-Output {pq(start)}; "
            f"$__ta_err = $null; try {{ Invoke-Expression -Command {pq(command)} }} "
            f"catch {{ $__ta_err = $_; Write-Output $__ta_err }}; "
            f"$__ta_status = if ($__ta_err) {{ 1 }} elseif ($LASTEXITCODE -ne $null) {{ $LASTEXITCODE }} else {{ 0 }}; "
            f"Write-Output ({pq(end)} + $__ta_status)"
        )


class BashArgs(BaseModel):
    command: str = Field(description="The shell command to run. Relative path is preferred in the command.")


def make_bash_tool(
    manager: PersistentBashManager,
    *,
    owner_id: str,
    sandbox: WorkspaceSandbox,
    timeout_ms: int,
    max_output_chars: int,
    cancel: asyncio.Event | None = None,
) -> StructuredTool:
    backend_note = (
        f" (backend={manager.kind})"
        if manager.kind != "bash"
        else ""
    )
    description = (
        "Run commands in a persistent shell. State, including the current directory and "
        f"exported environment variables, persists across calls for this agent{backend_note}."
    )

    async def _run(command: str) -> str:
        try:
            return await manager.run(
                owner_id,
                command,
                cwd=sandbox.root,
                timeout_ms=timeout_ms,
                max_output_chars=max_output_chars,
                cancel=cancel,
            )
        except (TimeoutError, RuntimeError, ShellBackendError, ValueError) as exc:
            return f"Error: {exc}"

    return StructuredTool.from_function(
        coroutine=_run,
        name="bash",
        description=description,
        args_schema=BashArgs,
    )
