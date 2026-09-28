"""journal 动作记录与落库协议（WP-30 先落记录模型与 Sink；DAO 在 WP-32 Task 13）。

journal 只记"发生过什么"（动作元数据 + digest），不存正文（spec §7/§15.6）。
淘汰必归因（I4）：store 的每个状态动作都经 JournalSink 落一行。
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from .models import OwnerType


class JournalAction:
    APPEND = "append"
    DEMOTE = "demote"
    EVICT = "evict"
    PIN = "pin"
    UNPIN = "unpin"
    REBUILD = "rebuild"
    GOAL = "goal"
    BATCH_CLOSE = "batch_close"


class JournalRecord(BaseModel):
    """单次动作记录（对应 005 迁移 context_journal 一行）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    owner_type: OwnerType
    owner_id: str
    task_id: str | None
    conversation_id: str | None
    partition: str
    entry_id: str
    entry_kind: str
    action: str
    reason: str
    policy_version: str
    tokens_est: int = 0
    digest: str = ""
    refs: dict = {}
    scope_level: str = "task"
    phase: str = "shared"
    step_id: str | None = None
    batch_id: str | None = None
    item_key: str | None = None
    created_at: str


class JournalSink(Protocol):
    """journal 落库协议（WP-32 由 ContextJournalDAO 适配；测试用 fake）。"""

    async def record(self, records: list[JournalRecord]) -> None: ...
