"""context.registry：owner 级 ContextStore 注册表（spec §7）。"""

from __future__ import annotations

import pytest

from tester_agent.context.registry import evict_owner, get_owner, start_owner
from tester_agent.context.store import ContextStore


def _start(owner_type="task", owner_id="t1", **kw):
    return start_owner(
        owner_type=owner_type,
        owner_id=owner_id,
        workspace_id="ws1",
        **kw,
    )


def test_start_returns_store_and_get_same():
    store = _start()
    assert isinstance(store, ContextStore)
    assert get_owner("task", "t1") is store


def test_duplicate_start_is_idempotent():
    s1 = _start()
    s2 = _start(policy_version="cp-v9")  # 重复 start 参数被忽略
    assert s1 is s2
    assert s2.policy_version == "cp-v1"


def test_owner_keys_are_isolated_by_type_and_id():
    st = _start(owner_type="task", owner_id="x")
    sc = _start(owner_type="conversation", owner_id="x")
    assert st is not sc
    assert get_owner("task", "x") is st
    assert get_owner("conversation", "x") is sc
    assert get_owner("task", "missing") is None


def test_evict_owner_removes_and_allows_restart():
    s1 = _start()
    assert evict_owner("task", "t1") is True
    assert get_owner("task", "t1") is None
    s2 = _start()
    assert s2 is not s1
    # 未注册 owner evict 返回 False
    assert evict_owner("task", "ghost") is False


def test_workspace_and_policy_version_threaded_to_store():
    store = start_owner(
        owner_type="task", owner_id="t9", workspace_id="ws-99",
        policy_version="cp-v42",
    )
    assert store.workspace_id == "ws-99"
    assert store.policy_version == "cp-v42"
