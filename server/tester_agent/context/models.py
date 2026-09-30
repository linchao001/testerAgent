"""上下文管理层域模型（WP-30，设计 spec §3/§15）。

- 三分区：P0 静态 / P1 知识 / P2 任务（淘汰策略分派依据）；
- 作用域二维：scope_level（task/batch/item）× phase（shared/design/write），
  实现批次条目隔离与测试设计/用例编写阶段隔离（spec §15）；
- 所有模型 ``extra="forbid"``，与 domain.py 同款 StrEnum 风格。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ContextPartition(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class EntryStatus(StrEnum):
    ACTIVE = "active"
    DEMOTED = "demoted"
    EVICTED = "evicted"


class EntryKind(StrEnum):
    METHODOLOGY = "methodology"       # P0
    KB_BLOCK = "kb_block"             # P1
    PLAN = "plan"                     # P2
    DECISION = "decision"             # P2 人工决策
    ARTIFACT_DIGEST = "artifact"      # P2 产物摘要
    TOOL_RESULT = "tool_result"       # P2
    CHAT_TURN = "chat_turn"           # P2
    REFLECTION = "reflection"         # P2
    GOAL = "goal"                     # P2 当前目标
    OUTLINE_DIGEST = "outline"        # P2 shared：确认大纲摘要


class ScopeLevel(StrEnum):
    TASK = "task"
    BATCH = "batch"
    ITEM = "item"


class Phase(StrEnum):
    SHARED = "shared"
    DESIGN = "design"
    WRITE = "write"


class ProfileName(StrEnum):
    CHAT = "chat"
    PLAN = "plan"
    EXECUTE = "execute"
    REFLECT = "reflect"
    REVIEW = "review"
    CASE_ITEM = "case_item"


OwnerType = Literal["task", "conversation"]


class EntryRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload_ref: str | None = None
    trace_id: str | None = None
    snapshot_id: str | None = None
    message_id: str | None = None


class ContextEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str
    partition: ContextPartition
    entry_kind: EntryKind
    status: EntryStatus = EntryStatus.ACTIVE
    content: str
    digest: str
    tokens_est: int = 0
    pinned: bool = False
    refs: EntryRefs = EntryRefs()
    # 作用域二维
    scope_level: ScopeLevel = ScopeLevel.TASK
    phase: Phase = Phase.SHARED
    step_id: str | None = None
    step_seq: int = 0
    batch_id: str | None = None
    item_key: str | None = None
    turn_seq: int = 0
    # CHAT_TURN 专属：user / assistant（组装时转 HumanMessage/AIMessage）
    role: str | None = None
    # 生命周期
    created_at: str
    supersedes: str | None = None
    source: str = "runtime"  # runtime | rebuild | manual


class EvictionRecord(BaseModel):
    """组装期新出窗记录（AssemblyReport.evicted_in_assembly 元素）。"""

    model_config = ConfigDict(extra="forbid")

    entry_id: str
    reason: str


class AssemblyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: ProfileName
    policy_version: str
    p0_version: str
    per_partition_tokens: dict[str, int]
    included: list[str]
    demoted_shown: list[str]
    evicted_in_assembly: list[EvictionRecord]
    budget_overrides: list[str]
    frozen: bool = False


class ContextView(BaseModel):
    """GET 上下文视图响应（P1 不含 passage 全文）。"""

    model_config = ConfigDict(extra="forbid")

    owner: dict
    goal: str | None
    entries: list[ContextEntry]
    totals: dict
    # journal 落库失败滞留时非 None：``{journal_pending: N}``；不阻断读路径
    degraded: dict | None = None
