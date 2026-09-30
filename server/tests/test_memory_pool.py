"""M1: WorkspaceMemoryPool / ReMeMemoryManager / reme_config (mock ReMe)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tester_agent.memory.reme_config import build_reme_app_config
from tester_agent.memory.manager import ReMeMemoryManager
from tester_agent.memory.pool import WorkspaceMemoryPool


class FakeReMeApp:
    """Injectable stand-in for ``reme.ReMe`` (start/close/run_job)."""

    def __init__(self, **_kwargs):
        self.is_started = False
        self.closed = False
        self.jobs: list[tuple[str, dict]] = []
        self.kwargs = _kwargs

    async def start(self) -> None:
        self.is_started = True

    async def close(self) -> None:
        self.is_started = False
        self.closed = True

    async def run_job(self, name: str, **kwargs):
        self.jobs.append((name, kwargs))

        class _Resp:
            success = True
            answer = f"ok:{name}"
            metadata = {"results": []}

        return _Resp()

    async def update_component(self, *_a, **_k) -> None:
        return None


def test_build_reme_app_config_maps_kb_id(tmp_path: Path):
    cfg = build_reme_app_config(
        workspace_dir=str(tmp_path),
        kb_config={
            "kb_id": "zhb_kb",
            "knowledge_dir": "knowledge",
            "options": {"daily_dir": "daily", "digest_dir": "digest"},
        },
    )
    assert cfg["workspace_dir"] == str(tmp_path)
    assert cfg["knowledge_base_id"] == "zhb_kb"
    assert cfg["daily_dir"] == "daily"
    assert cfg["digest_dir"] == "digest"
    assert "search" in cfg["jobs"]
    assert "knowledge_search" in cfg["jobs"]
    assert "auto_memory" in cfg["jobs"]


def test_build_reme_app_config_without_kb_omits_knowledge_jobs(tmp_path: Path):
    cfg = build_reme_app_config(
        workspace_dir=str(tmp_path),
        kb_config={"kb_id": "", "options": {}},
    )
    assert "knowledge_base_id" not in cfg
    assert "knowledge_search" not in cfg["jobs"]
    assert "search" in cfg["jobs"]


@pytest.mark.asyncio
async def test_manager_start_run_close(tmp_path: Path):
    fake = FakeReMeApp()
    mgr = ReMeMemoryManager(
        workspace_id="ws1",
        vault_dir=tmp_path / "reme",
        kb_config={"kb_id": "kb-1", "options": {}},
        reme_ctor=lambda **kw: fake,
    )
    await mgr.start()
    assert fake.is_started
    assert mgr.is_started
    resp = await mgr.run_job("search", query="q")
    assert resp is not None
    assert fake.jobs[0][0] == "search"
    await mgr.close()
    assert fake.closed
    assert not mgr.is_started


@pytest.mark.asyncio
async def test_pool_get_or_start_caches_and_invalidate(tmp_path: Path):
    created: list[FakeReMeApp] = []

    def factory(workspace_id, vault_dir, kb_config, **kwargs):
        app = FakeReMeApp()
        created.append(app)
        return ReMeMemoryManager(
            workspace_id=workspace_id,
            vault_dir=vault_dir,
            kb_config=kb_config,
            reme_ctor=lambda **kw: app,
            **{k: v for k, v in kwargs.items() if k in ("model_config",)},
        )

    pool = WorkspaceMemoryPool(tmp_path, manager_factory=factory)
    kb = {"kb_id": "kb-1", "options": {}}
    a = await pool.get_or_start("ws-a", kb)
    b = await pool.get_or_start("ws-a", kb)
    assert a is b
    assert len(created) == 1
    assert (tmp_path / "workspaces" / "ws-a" / "reme").is_dir() or (
        tmp_path / "ws-a" / "reme"
    ).exists() or a.vault_dir.exists()

    await pool.invalidate("ws-a")
    c = await pool.get_or_start("ws-a", kb)
    assert c is not a
    assert len(created) == 2
    assert created[0].closed

    await pool.shutdown_all()
    assert created[1].closed


@pytest.mark.asyncio
async def test_pool_start_all_warms_every_workspace(tmp_path: Path):
    created: list[FakeReMeApp] = []

    def factory(workspace_id, vault_dir, kb_config, **kwargs):
        app = FakeReMeApp()
        created.append(app)
        return ReMeMemoryManager(
            workspace_id=workspace_id,
            vault_dir=vault_dir,
            kb_config=kb_config,
            reme_ctor=lambda **kw: app,
            **{k: v for k, v in kwargs.items() if k in ("model_config",)},
        )

    pool = WorkspaceMemoryPool(tmp_path, manager_factory=factory)
    results = await pool.start_all(
        [
            ("ws-a", {"kb_id": "kb-a", "options": {}}),
            ("ws-b", {"kb_id": "", "options": {}}),
        ],
        model_config={"api_key": "x"},
    )
    assert results == {"ws-a": True, "ws-b": True}
    assert len(created) == 2
    assert all(app.is_started for app in created)

    # Idempotent: second warm-start hits cache
    again = await pool.start_all(
        [("ws-a", {"kb_id": "kb-a", "options": {}})],
    )
    assert again == {"ws-a": True}
    assert len(created) == 2

    await pool.shutdown_all()


@pytest.mark.asyncio
async def test_pool_start_all_records_failure_without_raising(tmp_path: Path):
    class BoomReMe(FakeReMeApp):
        async def start(self) -> None:
            raise RuntimeError("boom")

    def factory(workspace_id, vault_dir, kb_config, **kwargs):
        app = BoomReMe()
        return ReMeMemoryManager(
            workspace_id=workspace_id,
            vault_dir=vault_dir,
            kb_config=kb_config,
            reme_ctor=lambda **kw: app,
        )

    pool = WorkspaceMemoryPool(tmp_path, manager_factory=factory)
    results = await pool.start_all([("ws-x", {"kb_id": "k", "options": {}})])
    assert results == {"ws-x": False}
