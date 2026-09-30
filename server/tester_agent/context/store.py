"""ContextStore：按 owner（task/conversation）驻留的三分区内存态（spec §3/§7）。

- 写入串行（per-owner asyncio.Lock），读为确定性快照（状态动作以替换
  ContextEntry 对象实现，不就地改字段）；
- append 幂等（同 entry_id 忽略，INSERT OR IGNORE 语义）；
- 每个动作经 JournalSink 落 journal（I4）；落库失败不阻断内存态，
  行进入 degraded_journal，待 flush_degraded 补写（spec §9）；
- 状态机：ACTIVE→{DEMOTED,EVICTED}、DEMOTED→{ACTIVE(refresh),EVICTED}，
  EVICTED 终态；pin 仅允许 ACTIVE。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from ..errors import ValidationError
from ._time import utcnow_iso
from .journal import JournalAction, JournalRecord, JournalSink
from .models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryStatus,
    OwnerType,
    Phase,
    ScopeLevel,
)
from .policy import make_tombstone
from .tokens import estimate_tokens

logger = logging.getLogger(__name__)

_GOAL_PREFIX = "goal:"
_BATCH_PREFIX = "batch:"
_DIGEST_LIMIT = 80


class ContextStore:
    def __init__(
        self,
        *,
        owner_type: OwnerType,
        owner_id: str,
        workspace_id: str,
        policy_version: str = "cp-v1",
        journal: JournalSink | None = None,
    ) -> None:
        self.owner_type: OwnerType = owner_type
        self.owner_id = owner_id
        self.workspace_id = workspace_id
        self.policy_version = policy_version
        self._journal = journal
        self._entries: dict[str, ContextEntry] = {}
        self._lock = asyncio.Lock()
        self.degraded_journal: list[JournalRecord] = []
        # 组装冻结（WP-33 freeze 指令置位；assembler 读取跳过裁剪）
        self.frozen: bool = False
        # 终态 owner（任务 completed/failed/aborted）写干预拒绝；API 置位
        self.closed: bool = False
        # 运行时预算覆盖（WP-33 budget 指令；assemble 优先取用）
        self.budget_overrides: dict[Any, Any] = {}
        # P0 引导段版本（bind_p0 注入；assembler 写入 AssemblyReport）
        self.p0_version: str = ""

    def bind_p0(self, p0_version: str) -> None:
        """登记当前 P0 引导段版本（bootstrap_p0 产出；不持有消息体）。"""
        self.p0_version = p0_version

    # ---------- 写 ----------

    async def append(
        self, entry: ContextEntry, *, scope: Any | None = None
    ) -> bool:
        """追加条目；同 entry_id 已存在则忽略返回 False（不覆盖、不写 journal）。

        ``scope`` 缺省时取 :func:`current_scope`（wrap/invoke 压栈的标签），
        条目产生即定性（spec §15.6 防线 1）。
        """
        from .scopes import current_scope

        async with self._lock:
            if entry.entry_id in self._entries:
                return False
            e = self._inherit(entry, scope if scope is not None else current_scope())
            if not e.created_at:
                e = e.model_copy(update={"created_at": utcnow_iso()})
            if e.tokens_est == 0 and e.content:
                e = e.model_copy(update={"tokens_est": estimate_tokens(e.content)})
            self._entries[e.entry_id] = e
            await self._emit([self._record(e, JournalAction.APPEND, "append")])
            return True

    async def demote(self, entry_id: str, reason: str) -> None:
        """ACTIVE→DEMOTED：正文替换为 tombstone，清除 pinned。"""
        async with self._lock:
            e = self._require(entry_id)
            if e.status is not EntryStatus.ACTIVE:
                raise ValidationError(
                    f"非法状态迁移 demote：{entry_id} 当前 {e.status.value}",
                    details={"entry_id": entry_id, "status": e.status.value},
                )
            updated = e.model_copy(
                update={
                    "status": EntryStatus.DEMOTED,
                    "content": make_tombstone(e),
                    "pinned": False,
                }
            )
            self._entries[entry_id] = updated
            await self._emit([self._record(updated, JournalAction.DEMOTE, reason)])

    async def evict(self, entry_id: str, reason: str) -> None:
        """ACTIVE/DEMOTED→EVICTED（终态）。"""
        async with self._lock:
            e = self._require(entry_id)
            if e.status is EntryStatus.EVICTED:
                raise ValidationError(
                    f"非法状态迁移 evict：{entry_id} 已终态",
                    details={"entry_id": entry_id, "status": e.status.value},
                )
            updated = e.model_copy(update={"status": EntryStatus.EVICTED, "pinned": False})
            self._entries[entry_id] = updated
            await self._emit([self._record(updated, JournalAction.EVICT, reason)])

    async def pin(self, entry_id: str, *, reason: str) -> None:
        async with self._lock:
            e = self._require(entry_id)
            if e.status is not EntryStatus.ACTIVE:
                raise ValidationError(
                    f"仅 active 条目可 pin：{entry_id} 当前 {e.status.value}",
                    details={"entry_id": entry_id, "status": e.status.value},
                )
            if e.pinned:
                return
            updated = e.model_copy(update={"pinned": True})
            self._entries[entry_id] = updated
            await self._emit([self._record(updated, JournalAction.PIN, reason)])

    async def unpin(self, entry_id: str, *, reason: str) -> None:
        async with self._lock:
            e = self._require(entry_id)
            if not e.pinned:
                return  # 幂等 no-op
            updated = e.model_copy(update={"pinned": False})
            self._entries[entry_id] = updated
            await self._emit([self._record(updated, JournalAction.UNPIN, reason)])

    async def reactivate(
        self, entry_id: str, *, content: str, reason: str
    ) -> None:
        """DEMOTED/EVICTED → ACTIVE：正文回填（干预 refresh / 审计源）。"""
        async with self._lock:
            e = self._require(entry_id)
            if e.status is EntryStatus.ACTIVE:
                return
            updated = e.model_copy(
                update={
                    "status": EntryStatus.ACTIVE,
                    "content": content,
                    "digest": (content or "")[:_DIGEST_LIMIT],
                    "tokens_est": estimate_tokens(content),
                    "pinned": False,
                }
            )
            self._entries[entry_id] = updated
            await self._emit(
                [self._record(updated, JournalAction.REFRESH, reason)]
            )

    async def note_policy(self, *, reason: str, digest: str = "") -> None:
        """无条目侧效应的策略变更 journal（budget / freeze）。"""
        async with self._lock:
            rec = JournalRecord(
                id=uuid.uuid4().hex,
                workspace_id=self.workspace_id,
                owner_type=self.owner_type,
                owner_id=self.owner_id,
                task_id=self.owner_id if self.owner_type == "task" else None,
                conversation_id=(
                    self.owner_id if self.owner_type == "conversation" else None
                ),
                partition=ContextPartition.P2.value,
                entry_id="*",
                entry_kind="policy",
                action=JournalAction.POLICY,
                reason=reason,
                policy_version=self.policy_version,
                tokens_est=0,
                digest=digest,
                refs={},
                created_at=utcnow_iso(),
            )
            await self._emit([rec])

    async def set_goal(self, text: str, *, reason: str) -> str:
        """写入新目标；既有 active GOAL 全部 demote(superseded)。返回新 entry_id。"""
        async with self._lock:
            goal_id = f"{_GOAL_PREFIX}{uuid.uuid4().hex}"
            now = utcnow_iso()
            new_goal = ContextEntry(
                entry_id=goal_id,
                partition=ContextPartition.P2,
                entry_kind=EntryKind.GOAL,
                content=text,
                digest=(text or "")[:_DIGEST_LIMIT],
                tokens_est=estimate_tokens(text),
                pinned=True,
                created_at=now,
            )
            records: list[JournalRecord] = []
            superseded_id: str | None = None
            for old in list(self._entries.values()):
                if old.entry_kind is EntryKind.GOAL and old.status is EntryStatus.ACTIVE:
                    demoted = old.model_copy(
                        update={
                            "status": EntryStatus.DEMOTED,
                            "content": make_tombstone(old),
                            "pinned": False,
                        }
                    )
                    self._entries[old.entry_id] = demoted
                    superseded_id = old.entry_id
                    records.append(self._record(demoted, JournalAction.DEMOTE, "superseded"))
            new_goal = new_goal.model_copy(update={"supersedes": superseded_id})
            self._entries[goal_id] = new_goal
            records.append(self._record(new_goal, JournalAction.APPEND, "append"))
            records.append(self._record(new_goal, JournalAction.GOAL, reason))
            await self._emit(records)
            return goal_id

    async def bulk_demote(
        self, predicate: Callable[[ContextEntry], bool], reason: str
    ) -> list[str]:
        """对所有 ACTIVE 且命中谓词的条目 demote；返回受影响 entry_id（确定性顺序）。"""
        async with self._lock:
            affected: list[str] = []
            records: list[JournalRecord] = []
            for e in list(self._sorted_entries()):
                if e.status is EntryStatus.ACTIVE and predicate(e):
                    updated = e.model_copy(
                        update={
                            "status": EntryStatus.DEMOTED,
                            "content": make_tombstone(e),
                            "pinned": False,
                        }
                    )
                    self._entries[e.entry_id] = updated
                    affected.append(e.entry_id)
                    records.append(self._record(updated, JournalAction.DEMOTE, reason))
            if records:
                await self._emit(records)
            return affected

    async def close_batch(self, batch_id: str) -> int:
        """批次终态：evict 该批次全部 BATCH/ITEM 条目（reason=batch_closed）。"""
        async with self._lock:
            records: list[JournalRecord] = []
            count = 0
            for e in list(self._sorted_entries()):
                if (
                    e.batch_id == batch_id
                    and e.scope_level in (ScopeLevel.BATCH, ScopeLevel.ITEM)
                    and e.status is not EntryStatus.EVICTED
                ):
                    updated = e.model_copy(
                        update={"status": EntryStatus.EVICTED, "pinned": False}
                    )
                    self._entries[e.entry_id] = updated
                    count += 1
                    records.append(
                        self._record(updated, JournalAction.EVICT, "batch_closed")
                    )
            marker = JournalRecord(
                id=uuid.uuid4().hex,
                workspace_id=self.workspace_id,
                owner_type=self.owner_type,
                owner_id=self.owner_id,
                task_id=self.owner_id if self.owner_type == "task" else None,
                conversation_id=(
                    self.owner_id if self.owner_type == "conversation" else None
                ),
                partition=ContextPartition.P2.value,
                entry_id=f"{_BATCH_PREFIX}{batch_id}",
                entry_kind="batch",
                action=JournalAction.BATCH_CLOSE,
                reason="batch_closed",
                policy_version=self.policy_version,
                tokens_est=count,
                digest=f"batch {batch_id} closed: {count} entries evicted",
                refs={},
                scope_level=ScopeLevel.BATCH.value,
                phase=Phase.WRITE.value,
                step_id=None,
                batch_id=batch_id,
                item_key=None,
                created_at=utcnow_iso(),
            )
            records.append(marker)
            await self._emit(records)
            return count

    async def flush_degraded(self) -> int:
        """补写滞留 journal；仍失败则原样保留并抛出。"""
        if not self.degraded_journal or self._journal is None:
            return 0
        pending = self.degraded_journal
        self.degraded_journal = []
        try:
            await self._journal.record(pending)
        except Exception:
            self.degraded_journal = pending
            raise
        return len(pending)

    # ---------- 读 ----------

    def get(self, entry_id: str) -> ContextEntry:
        return self._entries[entry_id]

    def entries(
        self,
        partition: ContextPartition | None = None,
        status: EntryStatus | None = None,
    ) -> list[ContextEntry]:
        out = self._sorted_entries()
        if partition is not None:
            out = [e for e in out if e.partition is partition]
        if status is not None:
            out = [e for e in out if e.status is status]
        return out

    def goal_text(self) -> str | None:
        for e in reversed(self._sorted_entries()):
            if e.entry_kind is EntryKind.GOAL and e.status is EntryStatus.ACTIVE:
                return e.content
        return None

    def snapshot_state(self) -> dict[str, str]:
        return {e.entry_id: e.status.value for e in self._sorted_entries()}

    # ---------- 内部 ----------

    def _require(self, entry_id: str) -> ContextEntry:
        try:
            return self._entries[entry_id]
        except KeyError:
            raise KeyError(entry_id) from None

    def _sorted_entries(self) -> list[ContextEntry]:
        return [self._entries[k] for k in sorted(
            self._entries, key=lambda eid: (self._entries[eid].created_at, eid)
        )]

    def _inherit(self, entry: ContextEntry, scope: Any | None) -> ContextEntry:
        """从 ExecScope 继承作用域字段（仅填充条目未显式给出的维度）。"""
        if scope is None:
            return entry
        updates: dict[str, Any] = {}
        phase = getattr(scope, "phase", Phase.SHARED)
        if phase and phase is not Phase.SHARED and entry.phase is Phase.SHARED:
            updates["phase"] = phase
        if entry.step_id is None and getattr(scope, "step_id", None) is not None:
            updates["step_id"] = scope.step_id
        if entry.step_seq == 0 and getattr(scope, "step_seq", 0):
            updates["step_seq"] = scope.step_seq
        if entry.batch_id is None and getattr(scope, "batch_id", None) is not None:
            updates["batch_id"] = scope.batch_id
        if entry.item_key is None and getattr(scope, "item_key", None) is not None:
            updates["item_key"] = scope.item_key
        if entry.turn_seq == 0 and getattr(scope, "turn_seq", 0):
            updates["turn_seq"] = scope.turn_seq
        return entry.model_copy(update=updates) if updates else entry

    def _record(
        self, e: ContextEntry, action: str, reason: str
    ) -> JournalRecord:
        return JournalRecord(
            id=uuid.uuid4().hex,
            workspace_id=self.workspace_id,
            owner_type=self.owner_type,
            owner_id=self.owner_id,
            task_id=self.owner_id if self.owner_type == "task" else None,
            conversation_id=(
                self.owner_id if self.owner_type == "conversation" else None
            ),
            partition=e.partition.value,
            entry_id=e.entry_id,
            entry_kind=e.entry_kind.value,
            action=action,
            reason=reason,
            policy_version=self.policy_version,
            tokens_est=e.tokens_est,
            digest=e.digest,
            refs=e.refs.model_dump(),
            scope_level=e.scope_level.value,
            phase=e.phase.value,
            step_id=e.step_id,
            batch_id=e.batch_id,
            item_key=e.item_key,
            created_at=utcnow_iso(),
        )

    async def _emit(self, records: list[JournalRecord]) -> None:
        if not records or self._journal is None:
            return
        try:
            await self._journal.record(records)
        except Exception:
            logger.warning(
                "context journal persist failed; %d records held degraded",
                len(records),
                exc_info=True,
            )
            self.degraded_journal.extend(records)
