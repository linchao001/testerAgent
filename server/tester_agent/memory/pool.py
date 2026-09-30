# -*- coding: utf-8 -*-
"""Process-wide pool of per-workspace ReMeMemoryManager instances."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from .manager import ReMeMemoryManager

logger = logging.getLogger(__name__)

ManagerFactory = Callable[..., ReMeMemoryManager]


class WorkspaceMemoryPool:
    """Per-workspace embedded ReMe managers (eager warm-start + lazy fallback).

    Vault path: ``{workspace_root}/reme/`` (default
    ``{data_root}/workspaces/{workspace_id}/``, or a registered custom root).

    App lifespan should call :meth:`start_all` for every active workspace
    (QwenPaw-style); :meth:`get_or_start` remains the cache/hit path for
    newly created workspaces and recovery after invalidate.
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
        self._ws_roots: dict[str, Path] = {}

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

    def register_workspace_root(self, workspace_id: str, root: Path | str) -> None:
        self._ws_roots[workspace_id] = Path(root).expanduser().resolve()

    def unregister_workspace_root(self, workspace_id: str) -> None:
        self._ws_roots.pop(workspace_id, None)

    def vault_dir_for(self, workspace_id: str) -> Path:
        pinned = self._ws_roots.get(workspace_id)
        if pinned is not None:
            return pinned / "reme"
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

    async def start_all(
        self,
        workspaces: Iterable[tuple[str, dict[str, Any]]],
        *,
        model_config: dict[str, Any] | None = None,
    ) -> dict[str, bool]:
        """Warm-start embedded ReMe for each workspace (all non-deleted).

        Per-workspace failures are logged and recorded as ``False``; they do
        not raise or block the caller (aligns with optional memory in QwenPaw).
        """
        results: dict[str, bool] = {}
        for workspace_id, kb_config in workspaces:
            try:
                mgr = await self.get_or_start(
                    workspace_id,
                    kb_config,
                    model_config=model_config,
                )
                ok = bool(mgr.is_started)
                results[workspace_id] = ok
                if not ok:
                    logger.warning(
                        "ReMe warm-start incomplete for ws=%s", workspace_id
                    )
            except Exception:
                logger.exception(
                    "ReMe warm-start failed for ws=%s", workspace_id
                )
                results[workspace_id] = False
        return results

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
