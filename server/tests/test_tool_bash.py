import asyncio
from pathlib import Path

import pytest

from tester_agent.tools.bash_persistent import PersistentBashManager
from tester_agent.tools.shell_backend import ShellBackendError, detect_shell_backend


def test_detect_shell_backend_auto():
    kind, argv = detect_shell_backend("auto")
    assert kind in {"bash", "pwsh"}
    assert argv


def test_empty_command_errors(tmp_path: Path):
    mgr = PersistentBashManager(backend=detect_shell_backend("auto"))

    async def _run():
        with pytest.raises(ValueError):
            await mgr.run("o", "  ", cwd=tmp_path, timeout_ms=5_000, max_output_chars=1000)

    asyncio.run(_run())


def test_bash_persists_cwd(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    mgr = PersistentBashManager(backend=detect_shell_backend("auto"))

    async def _run():
        await mgr.run(
            "o1",
            "cd sub",
            cwd=tmp_path,
            timeout_ms=30_000,
            max_output_chars=16_000,
        )
        cmd = "pwd" if mgr.kind == "bash" else "(Get-Location).Path"
        out2 = await mgr.run(
            "o1",
            cmd,
            cwd=tmp_path,
            timeout_ms=30_000,
            max_output_chars=16_000,
        )
        assert "sub" in out2.replace("\\", "/")

    asyncio.run(_run())


def test_owners_isolated(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    mgr = PersistentBashManager(backend=detect_shell_backend("auto"))

    async def _run():
        await mgr.run("oa", "cd a", cwd=tmp_path, timeout_ms=30_000, max_output_chars=16_000)
        await mgr.run("ob", "cd b", cwd=tmp_path, timeout_ms=30_000, max_output_chars=16_000)
        cmd = "pwd" if mgr.kind == "bash" else "(Get-Location).Path"
        out_a = await mgr.run("oa", cmd, cwd=tmp_path, timeout_ms=30_000, max_output_chars=16_000)
        out_b = await mgr.run("ob", cmd, cwd=tmp_path, timeout_ms=30_000, max_output_chars=16_000)
        assert "a" in out_a.replace("\\", "/")
        assert "b" in out_b.replace("\\", "/")

    asyncio.run(_run())
