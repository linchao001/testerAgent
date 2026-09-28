"""执行作用域（spec §15.6 防线 1）：phase/step/batch/item 标签的 contextvars 栈。

节点（wrap）/批次执行器（run_in_batches）在执行前 push 当前标签；store.append
未显式指定作用域字段时自动继承——条目产生即定性，asyncio 任务间不串标。
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic import BaseModel, ConfigDict

from .models import ContextEntry, ContextPartition, EntryStatus, Phase, ProfileName, ScopeLevel


class ExecScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: Phase = Phase.SHARED
    step_id: str | None = None
    step_seq: int = 0
    batch_id: str | None = None
    item_key: str | None = None
    turn_seq: int = 0


_current_scope: contextvars.ContextVar[ExecScope | None] = contextvars.ContextVar(
    "tester_agent_context_scope", default=None
)


def current_scope() -> ExecScope:
    return _current_scope.get() or ExecScope()


@asynccontextmanager
async def scope(**kwargs) -> AsyncIterator[ExecScope]:
    """压入新作用域；未显式提供（None）的字段从父作用域继承。"""
    parent = _current_scope.get() or ExecScope()
    merged = parent.model_copy(
        update={k: v for k, v in kwargs.items() if v is not None}
    )
    token = _current_scope.set(merged)
    try:
        yield merged
    finally:
        _current_scope.reset(token)


def visible_in_window(
    e: ContextEntry, *, profile: ProfileName, scope_: ExecScope
) -> bool:
    """组装 scope 过滤谓词（spec §15.1/§15.5）。

    - EVICTED 永不可见；P0 恒可见；
    - ITEM 仅同 item_key 调用可见；BATCH 仅同 batch_id 可见（跨批次/跨条目物理排除，
      无 tombstone、零写操作）；
    - TASK 级按 phase：SHARED 恒可见；DESIGN/WRITE 私有过程仅同相阶段可见。
    """
    if e.status is EntryStatus.EVICTED:
        return False
    if e.partition is ContextPartition.P0:
        return True

    if e.scope_level is ScopeLevel.ITEM:
        return scope_.item_key is not None and e.item_key == scope_.item_key
    if e.scope_level is ScopeLevel.BATCH:
        return scope_.batch_id is not None and e.batch_id == scope_.batch_id

    # TASK 级
    if e.phase is Phase.SHARED:
        return True
    return e.phase is scope_.phase and scope_.phase is not Phase.SHARED
