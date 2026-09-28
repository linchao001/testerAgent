# -*- coding: utf-8 -*-
"""Per-workspace embedded ReMe memory manager."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from .reme_config import build_reme_app_config

logger = logging.getLogger(__name__)

ReMeCtor = Callable[..., Any]


class ReMeMemoryManager:
    """Lifecycle wrapper around an embedded ``reme.ReMe`` instance.

    ``reme_ctor`` is injectable for tests; production uses ``from reme import ReMe``.
    """

    def __init__(
        self,
        workspace_id: str,
        vault_dir: Path,
        kb_config: dict[str, Any],
        *,
        model_config: dict[str, Any] | None = None,
        reme_ctor: ReMeCtor | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.vault_dir = Path(vault_dir)
        self.kb_config = dict(kb_config or {})
        self.model_config = dict(model_config or {})
        self._reme_ctor = reme_ctor
        self._reme: Any | None = None
        self._lifecycle_writer_lock = asyncio.Lock()
        self._lifecycle_condition = asyncio.Condition()
        self._active_reme_jobs = 0
        self._lifecycle_operation: str | None = None

    @property
    def is_started(self) -> bool:
        return self._reme is not None and bool(
            getattr(self._reme, "is_started", False)
        )

    def kb_enabled(self) -> bool:
        return bool((self.kb_config.get("kb_id") or "").strip())

    def memory_search_enabled(self) -> bool:
        options = self.kb_config.get("options") or {}
        return bool(options.get("memory_search_enabled", True))

    def auto_memory_interval(self) -> int:
        options = self.kb_config.get("options") or {}
        try:
            return int(options.get("auto_memory_interval") or 0)
        except (TypeError, ValueError):
            return 0

    def _initialize_reme(self) -> None:
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        cfg = build_reme_app_config(
            workspace_dir=str(self.vault_dir),
            kb_config=self.kb_config,
            model_config=self.model_config or None,
        )
        ctor = self._reme_ctor
        if ctor is None:
            try:
                from reme import ReMe as ReMeApp  # type: ignore
            except Exception as exc:
                logger.warning(
                    "ReMe import failed; memory disabled for ws=%s: %s",
                    self.workspace_id,
                    exc,
                )
                self._reme = None
                return
            ctor = ReMeApp
        try:
            self._reme = ctor(**cfg)
        except Exception as exc:
            logger.warning(
                "ReMe construct failed for ws=%s: %s", self.workspace_id, exc
            )
            self._reme = None

    async def start(self) -> None:
        if self._reme is None:
            await asyncio.to_thread(self._initialize_reme)
        if self._reme is None:
            return
        try:
            await self._reme.start()
            logger.info("ReMe started for workspace %s", self.workspace_id)
        except Exception:
            logger.exception("ReMe start failed for ws=%s", self.workspace_id)
            self._reme = None

    async def close(self) -> bool:
        async with self._exclusive_reme_lifecycle("close"):
            return await self._close_unlocked()

    async def _close_unlocked(self) -> bool:
        if self._reme is not None:
            try:
                await self._reme.close()
            except Exception:
                logger.exception("ReMe close failed for ws=%s", self.workspace_id)
                self._reme = None
                return False
            self._reme = None
        return True

    @asynccontextmanager
    async def _reme_job_lease(self):
        async with self._lifecycle_condition:
            await self._lifecycle_condition.wait_for(
                lambda: self._lifecycle_operation is None
            )
            self._active_reme_jobs += 1
        try:
            yield
        finally:
            async with self._lifecycle_condition:
                self._active_reme_jobs -= 1
                if self._active_reme_jobs == 0:
                    self._lifecycle_condition.notify_all()

    @asynccontextmanager
    async def _exclusive_reme_lifecycle(self, operation: str):
        async with self._lifecycle_writer_lock:
            async with self._lifecycle_condition:
                self._lifecycle_operation = operation
            try:
                async with self._lifecycle_condition:
                    await self._lifecycle_condition.wait_for(
                        lambda: self._active_reme_jobs == 0
                    )
                yield
            finally:
                async with self._lifecycle_condition:
                    self._lifecycle_operation = None
                    self._lifecycle_condition.notify_all()

    async def run_job(
        self,
        name: str,
        *,
        raise_on_error: bool = False,
        **kwargs: Any,
    ) -> Any | None:
        async with self._reme_job_lease():
            if self._reme is None or not getattr(self._reme, "is_started", False):
                logger.debug("ReMe job skipped; not started: %s", name)
                return None
            try:
                return await self._reme.run_job(name, **kwargs)
            except Exception:
                logger.exception("ReMe job failed: %s", name)
                if raise_on_error:
                    raise
                return None
