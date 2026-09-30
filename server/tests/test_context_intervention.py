"""WP-33：运行时干预（selector / 执行器 / commands API / 工具与斜杠）。

Task 17–19 合集：plan Wave D / design §14。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.adapters.reme import ReMeReaderFactory
from tester_agent.api.context import router as context_router
from tester_agent.api.error_handling import install_error_handling
from tester_agent.api.events import (
    CONTEXT_COMMAND_EXECUTED,
    CONTEXT_POLICY_CHANGED,
    make_events_router,
)
from tester_agent.context.intervention import (
    ContextAction,
    ContextCommand,
    ConfirmRequiredError,
    execute,
    parse_selector,
)
from tester_agent.context.journal import JournalRecord
from tester_agent.context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryStatus,
    ProfileName,
)
from tester_agent.context.registry import ContextRegistry
from tester_agent.context.store import ContextStore
from tester_agent.errors import TaskStateConflict, ValidationError
from tester_agent.graph.main_graph import build_graph
from tester_agent.graph.registry import CASE_DESIGNER, GraphRegistry
from tester_agent.runtime.bus import EventBus
from tester_agent.runtime.context import AppContext
from tester_agent.runtime.idempotency import IdempotencyStore
from tester_agent.runtime.runner import TaskRegistry
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ConfigDAO,
    ContextJournalDAO,
    TaskDAO,
    TaskRow,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore
from tester_agent.tools.registry import ToolBuildContext, build_case_designer_tools

WS, CONV, TASK = "ws-iv", "conv-iv", "task-iv"
TS0 = "2026-09-28T00:00:00.000Z"


def _run(coro):
    return asyncio.run(coro)


def _err(r):
    return r.json()["error"]


class FakeSink:
    def __init__(self):
        self.rows: list[JournalRecord] = []

    async def record(self, records: list[JournalRecord]) -> None:
        self.rows.extend(records)


def _store(**kw) -> tuple[ContextStore, FakeSink]:
    sink = FakeSink()
    store = ContextStore(
        owner_type="task",
        owner_id="t1",
        workspace_id="ws1",
        journal=sink,
        **kw,
    )
    return store, sink


def _entry(eid: str, **kw) -> ContextEntry:
    base = dict(
        entry_id=eid,
        partition=ContextPartition.P2,
        entry_kind=EntryKind.DECISION,
        content="决策正文",
        digest="决策",
        created_at=TS0,
    )
    base.update(kw)
    return ContextEntry(**base)


# ---------- Task 17：selector + 执行器 ----------


@pytest.mark.parametrize(
    "raw,kind,value",
    [
        ("id:plan:v1", "id", "plan:v1"),
        ("kind:decision", "kind", "decision"),
        ("step:s3", "step", "s3"),
        ("recent:3", "recent", "3"),
        ("item:b1:p2", "item", "b1:p2"),
        ("batch:b9", "batch", "b9"),
        ("all", "all", None),
    ],
)
def test_parse_selector_six_forms(raw, kind, value):
    assert parse_selector(raw) == (kind, value)


def test_parse_selector_invalid_raises_422():
    with pytest.raises(ValidationError) as ei:
        parse_selector("nope")
    assert ei.value.http_status == 422


async def test_pin_unpin_forget_refresh_set_goal_budget_freeze():
    store, sink = _store()
    await store.append(_entry("d1"))
    await store.append(
        _entry("d2", entry_kind=EntryKind.PLAN, content="计划", digest="计划")
    )
    await store.append(
        _entry(
            "chat1",
            entry_kind=EntryKind.CHAT_TURN,
            content="旧轮",
            digest="旧轮",
            turn_seq=1,
            created_at="2026-09-28T00:00:01.000Z",
        )
    )

    r = await execute(
        store, ContextCommand(action=ContextAction.PIN, selector="id:d1"), operator="api"
    )
    assert store.get("d1").pinned is True
    assert any(x.startswith("manual:") for x in [row.reason for row in sink.rows])
    assert "d1" in r.affected

    await execute(
        store, ContextCommand(action=ContextAction.UNPIN, selector="id:d1"), operator="api"
    )
    assert store.get("d1").pinned is False

    await execute(
        store,
        ContextCommand(action=ContextAction.FORGET, selector="id:d2"),
        operator="user",
    )
    assert store.get("d2").status is EntryStatus.DEMOTED

    await store.demote("chat1", "prep")
    r2 = await execute(
        store,
        ContextCommand(action=ContextAction.REFRESH, selector="id:chat1"),
        operator="api",
        source_resolver=lambda e: "恢复正文",
    )
    assert store.get("chat1").status is EntryStatus.ACTIVE
    assert store.get("chat1").content == "恢复正文"
    assert r2.affected_meta.get("chat1") != "source_missing"

    await execute(
        store,
        ContextCommand(action=ContextAction.SET_GOAL, arg="只覆盖支付"),
        operator="api",
    )
    assert store.goal_text() == "只覆盖支付"

    await execute(
        store,
        ContextCommand(
            action=ContextAction.BUDGET,
            arg=json.dumps({"profile": "chat", "p0": 1000, "p1": 2000, "p2": 3000}),
        ),
        operator="api",
        model_window=128_000,
    )
    assert store.budget_overrides[ProfileName.CHAT].p2 == 3000

    await execute(
        store, ContextCommand(action=ContextAction.FREEZE), operator="api"
    )
    assert store.frozen is True
    await execute(
        store, ContextCommand(action=ContextAction.UNFREEZE), operator="api"
    )
    assert store.frozen is False


async def test_p0_write_rejected_422():
    store, _ = _store()
    await store.append(
        _entry(
            "p0:meth",
            partition=ContextPartition.P0,
            entry_kind=EntryKind.METHODOLOGY,
            content="方法论",
            digest="方法论",
        )
    )
    with pytest.raises(ValidationError) as ei:
        await execute(
            store,
            ContextCommand(action=ContextAction.PIN, selector="id:p0:meth"),
            operator="api",
        )
    assert ei.value.http_status == 422


async def test_pinned_forget_needs_confirm():
    store, _ = _store()
    await store.append(_entry("d1"))
    await store.pin("d1", reason="human")
    with pytest.raises(ConfirmRequiredError) as ei:
        await execute(
            store,
            ContextCommand(action=ContextAction.FORGET, selector="id:d1"),
            operator="api",
        )
    assert ei.value.code == "CONFIRM_REQUIRED"
    assert "d1" in str(ei.value.details.get("candidates", []))

    await execute(
        store,
        ContextCommand(
            action=ContextAction.FORGET, selector="id:d1", confirm=True
        ),
        operator="api",
    )
    assert store.get("d1").status is EntryStatus.DEMOTED


async def test_refresh_source_missing_no_raise():
    store, _ = _store()
    await store.append(_entry("x1"))
    await store.demote("x1", "cut")
    r = await execute(
        store,
        ContextCommand(action=ContextAction.REFRESH, selector="id:x1"),
        operator="api",
        source_resolver=lambda e: None,
    )
    assert r.ok is True
    assert r.affected_meta["x1"] == "source_missing"
    assert store.get("x1").status is EntryStatus.DEMOTED


async def test_closed_store_write_409():
    store, _ = _store()
    store.closed = True
    await store.append(_entry("d1"))  # append still works under lock; closed checked in execute
    with pytest.raises(TaskStateConflict):
        await execute(
            store,
            ContextCommand(action=ContextAction.PIN, selector="id:d1"),
            operator="api",
        )
    # show 只读允许
    r = await execute(
        store,
        ContextCommand(action=ContextAction.SHOW, selector="id:d1"),
        operator="api",
    )
    assert r.ok is True


async def test_manual_journal_reason_prefix():
    store, sink = _store()
    await store.append(_entry("d1"))
    await execute(
        store,
        ContextCommand(
            action=ContextAction.PIN, selector="id:d1", reason="ops"
        ),
        operator="api",
    )
    assert any(row.reason == "manual:ops" for row in sink.rows)


async def test_selector_kind_and_item_batch():
    store, _ = _store()
    await store.append(_entry("a", entry_kind=EntryKind.DECISION))
    await store.append(
        _entry(
            "b",
            entry_kind=EntryKind.TOOL_RESULT,
            batch_id="bx",
            item_key="bx:p1",
            content="工具",
            digest="工具",
        )
    )
    r = await execute(
        store,
        ContextCommand(action=ContextAction.PIN, selector="kind:decision"),
        operator="api",
    )
    assert r.affected == ["a"]
    r2 = await execute(
        store,
        ContextCommand(action=ContextAction.FORGET, selector="item:bx:p1"),
        operator="api",
    )
    assert "b" in r2.affected
    r3 = await execute(
        store,
        ContextCommand(action=ContextAction.FORGET, selector="batch:bx", confirm=True),
        operator="api",
    )
    # b already demoted; batch forget may evict remaining batch entries
    assert r3.ok is True


# ---------- Task 18：commands API ----------


async def _seed(db, *, status="running", task_id=TASK, ws=WS, conv=CONV):
    row = await db.aquery_one("SELECT id FROM workspace WHERE id = ?", (ws,))
    if row is None:
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(
                id=ws,
                name="ws",
                kb_config={
                    "kb_id": "kb-1",
                    "knowledge_bases_dir": "",
                    "knowledge_dir": "knowledge",
                    "create_knowledge_base": False,
                    "options": {},
                },
            )
        )
    await db.aexecute(
        "INSERT OR IGNORE INTO conversation "
        "(id, workspace_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (conv, ws, "c", TS0, TS0),
    )
    await TaskDAO(db).create(
        TaskRow.create(
            id=task_id,
            conversation_id=conv,
            workspace_id=ws,
            status=status,
            current_stage="link_identify",
            langgraph_thread_id=f"th-{task_id}",
            graph_run_id=f"run-{task_id}",
            snapshot_level="meta",
        )
    )


def _make_iv_app(tmp_path: Path, *, status="running"):
    from contextlib import asynccontextmanager

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from fakes import FakeLLM
    from tester_agent.store.models import EventDAO

    tag = uuid.uuid4().hex[:8]
    db_path = tmp_path / f"iv-{tag}.db"
    ckpt_path = tmp_path / f"ivckpt-{tag}.db"
    run_migrations(db_path)
    file_store = FileStore(tmp_path / f"wsfiles-{tag}")
    factory = ReMeReaderFactory()
    registry = ContextRegistry()

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        conn = await aiosqlite.connect(str(ckpt_path), check_same_thread=False)
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        g = build_graph(saver)
        db = Database(db_path)
        graphs = GraphRegistry.from_graph(CASE_DESIGNER, g)
        bus = EventBus(EventDAO(db))
        a.state.db = db
        a.state.db_path = db_path
        a.state.bus = bus
        a.state.idem_store = IdempotencyStore()
        a.state.registry = TaskRegistry(TaskDAO(db))
        a.state.app_ctx = AppContext(
            db=db,
            file_store=file_store,
            llm=FakeLLM([]),
            reme_factory=factory,
            config=ConfigDAO(db),
            graphs=graphs,
            bus=bus,
            registry=a.state.registry,
            context_registry=registry,
        )
        await _seed(db, status=status)
        try:
            yield
        finally:
            await graphs.aclose()
            await conn.close()
            db.close()

    app = FastAPI(lifespan=lifespan)
    install_error_handling(app)
    app.include_router(context_router)
    app.include_router(make_events_router())
    return app, registry


@pytest.fixture()
def iv_env(tmp_path):
    app, registry = _make_iv_app(tmp_path)
    with TestClient(app) as client:
        db = Database(app.state.db_path)
        store = registry.start_owner(
            owner_type="task",
            owner_id=TASK,
            workspace_id=WS,
            journal=ContextJournalDAO(db),
        )
        _run(store.append(_entry("dec1")))
        try:
            yield SimpleNamespace(
                client=client,
                db=db,
                registry=registry,
                store=store,
                bus=app.state.bus,
            )
        finally:
            db.close()


def test_commands_idempotent_replay(iv_env):
    body = {"action": "pin", "selector": "id:dec1"}
    headers = {"Idempotency-Key": "k-pin-1"}
    r1 = iv_env.client.post(
        f"/api/v1/tasks/{TASK}/context/commands", json=body, headers=headers
    )
    assert r1.status_code == 200, r1.text
    assert iv_env.store.get("dec1").pinned is True
    r2 = iv_env.client.post(
        f"/api/v1/tasks/{TASK}/context/commands", json=body, headers=headers
    )
    assert r2.status_code == 200
    assert r2.json() == r1.json()


def test_commands_cross_workspace_404(iv_env):
    iv_env.store.workspace_id = "other-ws"
    r = iv_env.client.post(
        f"/api/v1/tasks/{TASK}/context/commands",
        json={"action": "pin", "selector": "id:dec1"},
    )
    assert r.status_code == 404


def test_commands_completed_task_write_409(tmp_path):
    app, registry = _make_iv_app(tmp_path, status="completed")
    with TestClient(app) as client:
        db = Database(app.state.db_path)
        store = registry.start_owner(
            owner_type="task",
            owner_id=TASK,
            workspace_id=WS,
            journal=ContextJournalDAO(db),
        )
        _run(store.append(_entry("dec1")))
        r = client.post(
            f"/api/v1/tasks/{TASK}/context/commands",
            json={"action": "pin", "selector": "id:dec1"},
        )
        assert r.status_code == 409, r.text
        r2 = client.post(
            f"/api/v1/tasks/{TASK}/context/commands",
            json={"action": "show", "selector": "id:dec1"},
        )
        assert r2.status_code == 200, r2.text
        db.close()


def test_commands_sse_events(iv_env):
    from tester_agent.store.models import EventDAO

    r = iv_env.client.post(
        f"/api/v1/tasks/{TASK}/context/commands",
        json={
            "action": "budget",
            "arg": json.dumps(
                {"profile": "chat", "p0": 1000, "p1": 2000, "p2": 3000}
            ),
        },
    )
    assert r.status_code == 200, r.text
    rows = _run(EventDAO(iv_env.db).list_after(TASK, 0))
    types = [row.type for row in rows]
    assert CONTEXT_COMMAND_EXECUTED in types
    assert CONTEXT_POLICY_CHANGED in types


# ---------- Task 19：工具组 + 斜杠 ----------


class ScriptedModel(BaseChatModel):
    def __init__(self, script: list[dict]) -> None:
        super().__init__()
        self._script = script
        self._received: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, **kwargs):
        raise NotImplementedError

    async def _agenerate(self, messages, stop=None, **kwargs) -> ChatResult:
        self._received.append(list(messages))
        step = len(self._received) - 1
        spec = self._script[step]
        if "tool_calls" in spec:
            msg = AIMessage(content="", tool_calls=spec["tool_calls"])
        else:
            msg = AIMessage(content=spec["content"])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def test_context_tools_registered_when_enabled(tmp_path):
    store, sink = _store()
    _run(store.append(_entry("d1")))
    tools = build_case_designer_tools(
        ToolBuildContext(
            owner_id="c1",
            workspace_root=tmp_path,
            runtime_config={"context.intervention.enabled": True},
            context_store=store,
        )
    )
    names = {t.name for t in tools}
    assert "context_forget" in names
    assert "context_pin" in names

    tools_off = build_case_designer_tools(
        ToolBuildContext(
            owner_id="c1",
            workspace_root=tmp_path,
            runtime_config={"context.intervention.enabled": False},
            context_store=store,
        )
    )
    assert "context_forget" not in {t.name for t in tools_off}


async def test_context_forget_tool_exact_and_ambiguous(tmp_path):
    from tester_agent.tools.context_tools import make_context_tools
    from tester_agent.graph.tool_agent import run_tool_agent

    store, sink = _store()
    await store.append(_entry("d1"))
    await store.append(
        _entry("d2", entry_kind=EntryKind.DECISION, content="二", digest="二")
    )
    tools = make_context_tools(store)
    model = ScriptedModel(
        [
            {
                "tool_calls": [
                    {
                        "name": "context_forget",
                        "id": "c1",
                        "args": {"selector": "id:d1"},
                    }
                ]
            },
            {"content": "done"},
        ]
    )
    await run_tool_agent(
        history=[HumanMessage(content="forget")],
        system_prompt="sys",
        tools=tools,
        model=model,
        max_steps=4,
    )
    assert store.get("d1").status is EntryStatus.DEMOTED
    assert any(r.reason.startswith("manual:") for r in sink.rows)

    # 多命中不执行
    store2, _ = _store()
    await store2.append(_entry("a1", entry_kind=EntryKind.DECISION))
    await store2.append(
        _entry("a2", entry_kind=EntryKind.DECISION, content="x", digest="x")
    )
    tools2 = make_context_tools(store2)
    forget = next(t for t in tools2 if t.name == "context_forget")
    out = await forget.ainvoke({"selector": "kind:decision"})
    assert "候选" in out or "candidate" in out.lower() or "id:" in out
    assert store2.get("a1").status is EntryStatus.ACTIVE


def test_slash_context_bypasses_llm(tmp_path):
    from tester_agent.runtime.chat_agent import try_slash_context_command

    store, sink = _store()
    _run(store.append(_entry("d1", entry_kind=EntryKind.DECISION)))
    handled, text = _run(
        try_slash_context_command(
            store, "/context pin kind:decision"
        )
    )
    assert handled is True
    assert store.get("d1").pinned is True
    assert "pin" in text.lower() or "d1" in text

    handled2, _ = _run(
        try_slash_context_command(store, "普通消息不要拦截")
    )
    assert handled2 is False
