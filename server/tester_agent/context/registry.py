"""owner 级 ContextStore 注册表（spec §7）。

键：(owner_type, owner_id)；任务/会话全程持有同一内存 store。重复 start
幂等（忽略后续参数），evict 后允许重新 start（rebuild/测试隔离场景）。

WP-31：``ContextRegistry`` 实例类（AppContext.context_registry 持有独立
实例，组合根可显式注入做隔离测试）；模块级函数操作进程默认实例，既有
调用路径（含 WP-30 测试）行为不变。单 worker 模型下二者均合法。
"""

from __future__ import annotations

from typing import Any, Literal

from .journal import JournalSink
from .rebuild import rebuild_store
from .store import ContextStore

OwnerType = Literal["task", "conversation"]
_OwnerKey = tuple[str, str]


class ContextRegistry:
    """owner → ContextStore 的进程内注册表。"""

    def __init__(self) -> None:
        self._stores: dict[_OwnerKey, ContextStore] = {}

    def start_owner(
        self,
        *,
        owner_type: OwnerType,
        owner_id: str,
        workspace_id: str,
        policy_version: str = "cp-v1",
        journal: JournalSink | None = None,
    ) -> ContextStore:
        """注册并返回 owner 的 ContextStore；重复调用幂等返回既有实例。"""
        key = (owner_type, owner_id)
        existing = self._stores.get(key)
        if existing is not None:
            return existing
        store = ContextStore(
            owner_type=owner_type,
            owner_id=owner_id,
            workspace_id=workspace_id,
            policy_version=policy_version,
            journal=journal,
        )
        self._stores[key] = store
        return store

    async def restore_owner(
        self,
        *,
        owner_type: OwnerType,
        owner_id: str,
        workspace_id: str,
        daos: Any,
        journal_sink: JournalSink | None = None,
        policy_version: str = "cp-v1",
        step_window: int = 1,
    ) -> ContextStore:
        """无内存 store 时经 rebuild 恢复并注册；已存在则幂等返回。"""
        key = (owner_type, owner_id)
        existing = self._stores.get(key)
        if existing is not None:
            return existing
        store = await rebuild_store(
            owner_type=owner_type,
            owner_id=owner_id,
            workspace_id=workspace_id,
            daos=daos,
            journal_sink=journal_sink,
            policy_version=policy_version,
            step_window=step_window,
        )
        self._stores[key] = store
        return store

    def get_owner(self, owner_type: OwnerType, owner_id: str) -> ContextStore | None:
        return self._stores.get((owner_type, owner_id))

    def evict_owner(self, owner_type: OwnerType, owner_id: str) -> bool:
        """从注册表移除；返回是否曾存在。不主动清空 store 内存数据。"""
        return self._stores.pop((owner_type, owner_id), None) is not None


# ---------- 进程默认实例（模块级函数；WP-30 既有接口） ----------

_REGISTRY = ContextRegistry()


def start_owner(
    *,
    owner_type: OwnerType,
    owner_id: str,
    workspace_id: str,
    policy_version: str = "cp-v1",
    journal: JournalSink | None = None,
) -> ContextStore:
    """注册并返回 owner 的 ContextStore；重复调用幂等返回既有实例。"""
    return _REGISTRY.start_owner(
        owner_type=owner_type,
        owner_id=owner_id,
        workspace_id=workspace_id,
        policy_version=policy_version,
        journal=journal,
    )


async def restore_owner(
    *,
    owner_type: OwnerType,
    owner_id: str,
    workspace_id: str,
    daos: Any,
    journal_sink: JournalSink | None = None,
    policy_version: str = "cp-v1",
    step_window: int = 1,
) -> ContextStore:
    """无内存 store 时经 rebuild 恢复并注册；已存在则幂等返回。"""
    return await _REGISTRY.restore_owner(
        owner_type=owner_type,
        owner_id=owner_id,
        workspace_id=workspace_id,
        daos=daos,
        journal_sink=journal_sink,
        policy_version=policy_version,
        step_window=step_window,
    )


def get_owner(owner_type: OwnerType, owner_id: str) -> ContextStore | None:
    return _REGISTRY.get_owner(owner_type, owner_id)


def evict_owner(owner_type: OwnerType, owner_id: str) -> bool:
    """从注册表移除；返回是否曾存在。不主动清空 store 内存数据。"""
    return _REGISTRY.evict_owner(owner_type, owner_id)
