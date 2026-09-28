"""WP-31 Task 8：AppContext/TaskContext 的 context 接线位（spec §6.1 组合根）。

- AppContext.context_registry 默认工厂（进程内独立实例，可显式注入）；
- TaskContext.context_store 默认 None（旧路径无影响），可注入 fake store；
- DAOs.journal 字段预留（WP-32 Task 12 落具体 ContextJournalDAO）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.context import registry as reg_module
from tester_agent.context.registry import ContextRegistry
from tester_agent.context.store import ContextStore
from tester_agent.runtime.context import AppContext, DAOs, TaskContext


def _app(**kw) -> AppContext:
    base = dict(
        db=object(),
        file_store=object(),
        llm=object(),
        reme_factory=object(),
        config=object(),
    )
    base.update(kw)
    return AppContext(**base)


def test_app_context_default_context_registry_isolated():
    app = _app()
    assert isinstance(app.context_registry, ContextRegistry)
    other = _app()
    assert other.context_registry is not app.context_registry

    store = app.context_registry.start_owner(
        owner_type="conversation", owner_id="c1", workspace_id="ws1"
    )
    assert isinstance(store, ContextStore)
    assert app.context_registry.get_owner("conversation", "c1") is store
    # 另一实例不可见
    assert other.context_registry.get_owner("conversation", "c1") is None


def test_app_context_context_registry_explicit_injection():
    registry = ContextRegistry()
    app = _app(context_registry=registry)
    assert app.context_registry is registry


def test_app_context_registry_separate_from_module_default():
    """组合根实例与进程默认注册表（模块级函数）互不相通。"""
    app = _app()
    app.context_registry.start_owner(
        owner_type="task", owner_id="leak-check", workspace_id="ws1"
    )
    assert reg_module.get_owner("task", "leak-check") is None
    # 反向：模块默认实例的 owner 不出现在 app registry
    reg_module.start_owner(
        owner_type="task", owner_id="default-only", workspace_id="ws1"
    )
    assert app.context_registry.get_owner("task", "default-only") is None
    reg_module.evict_owner("task", "default-only")


def _task_context(**kw) -> TaskContext:
    base = dict(
        app=object(),
        task=object(),
        run_id="run-1",
        files=object(),
        reader=object(),
        snapshot_level="off",
        mirror=object(),
    )
    base.update(kw)
    return TaskContext(**base)


def test_task_context_context_store_default_none():
    ctx = _task_context()
    assert ctx.context_store is None


async def test_task_context_context_store_injected():
    store = ContextStore(
        owner_type="task", owner_id="t1", workspace_id="ws1", journal=None
    )
    ctx = _task_context(context_store=store)
    assert ctx.context_store is store


def test_daos_journal_field_default_none_and_settable():
    daos = DAOs(task=object(), message=object())
    assert daos.journal is None
    fake_journal = object()
    daos2 = DAOs(task=object(), message=object(), journal=fake_journal)
    assert daos2.journal is fake_journal
