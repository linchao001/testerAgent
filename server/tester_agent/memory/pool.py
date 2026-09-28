# -*- coding: utf-8 -*-
"""Process-wide pool of per-workspace ReMeMemoryManager instances."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

from .manager import ReMeMemoryManager

logger = logging.getLogger(__name__)

ManagerFactory = Callable[..., ReMeMemoryManager]


class WorkspaceMemoryPool:
    """Lazy per-workspace embedded ReMe managers.

    Vault path: ``{data_root}/workspaces/{workspace_id}/reme/``.
    """

    def __init__(
        self,
        data_root: Path,
        *,
        manager_factory: ManagerFactory | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self._manager_factory = manager_factory or self._default_factory
        self._managers: dict[str, ReMeMemoryManager] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    def _default_factory(
        self,
        workspace_id: str,
        vault_dir: Path,
        kb_config: dict[str, Any],
        **kwargs: Any,
    ) -> ReMeMemoryManager:
        return ReMeMemoryManager(
            workspace_id=workspace_id,
            vault_dir=vault_dir,
            kb_config=kb_config,
            model_config=kwargs.get("model_config"),
        )

    def vault_dir_for(self, workspace_id: str) -> Path:
        return self.data_root / "workspaces" / workspace_id / "reme"

    async def get_or_start(
        self,
        workspace_id: str,
        kb_config: dict[str, Any],
        *,
        model_config: dict[str, Any] | None = None,
    ) -> ReMeMemoryManager:
        cached = self._managers.get(workspace_id)
        if cached is not None and cached.is_started:
            return cached
        lock = await self._lock_for(workspace_id)
        async with lock:
            cached = self._managers.get(workspace_id)
            if cached is not None and cached.is_started:
                return cached
            vault = self.vault_dir_for(workspace_id)
            mgr = self._manager_factory(
                workspace_id,
                vault,
                kb_config,
                model_config=model_config,
            )
            await mgr.start()
            self._managers[workspace_id] = mgr
            return mgr

    async def invalidate(self, workspace_id: str) -> None:
        lock = await self._lock_for(workspace_id)
        async with lock:
            mgr = self._managers.pop(workspace_id, None)
            if mgr is not None:
                await mgr.close()

    async def shutdown_all(self) -> None:
        async with self._global:
            ids = list(self._managers.keys())
        for ws_id in ids:
            await self.invalidate(ws_id)

    async def _lock_for(self, workspace_id: str) -> asyncio.Lock:
        async with self._global:
            lock = self._locks.get(workspace_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[workspace_id] = lock
            return lock
