"""控制环 build_graph / wrap 测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from tester_agent.domain import PlanStepKind
from tester_agent.errors import AppError, TaskCancelled, ValidationError
from tester_agent.graph.control.graph import build_control_graph
from tester_agent.graph.main_graph import STAGE_NODES, build_graph
from tester_agent.graph.state import TaskState
from tester_agent.graph.wrap import wrap
from tester_agent.runtime.context import AppContext, TaskContext


def _make_ctx(*, cancelled: bool = False, events: list | None = None) -> TaskContext:
    app = AppContext(db=None, file_store=None, llm=None, reme_factory=None, config=None)
    event = asyncio.Event()
    if cancelled:
        event.set()

    async def emit(type_: str, payload: dict) -> None:
        events.append((type_, payload))

    return TaskContext(
        app=app,
        task=SimpleNamespace(id="t1", graph_run_id="r1"),
        run_id="r1",
        files=None,
        reader=None,
        snapshot_level="off",
        mirror=None,
        emit=emit if events is not None else None,
        cancel_event=event,
    )


def _cfg(thread_id: str, ctx: TaskContext) -> dict:
    return {"configurable": {"thread_id": thread_id, "ctx": ctx}}


@pytest.fixture
async def saver(tmp_path):
    conn = await aiosqlite.connect(
        str(tmp_path / "checkpoints.db"), check_same_thread=False
    )
    sv = AsyncSqliteSaver(conn)
    await sv.setup()
    yield sv
    await conn.close()


def test_control_graph_compiles():
    graph = build_graph(None)
    nodes = set(graph.get_graph().nodes)
    assert {"plan", "dispatch", "execute_step", "await_human", "reflect"} <= nodes


def test_build_graph_alias_is_control_graph():
    assert build_graph(None).get_graph().nodes.keys() == build_control_graph(
        None
    ).get_graph().nodes.keys()


def test_stage_nodes_constant_lists_capabilities():
    assert STAGE_NODES == (
        "intake",
        "link_identify",
        "point_write",
        "case_generate",
        "coverage_check",
    )


@pytest.mark.asyncio
async def test_gates_off_stub_run_completes(saver):
    graph = build_graph(saver)
    cfg = {"configurable": {"thread_id": "t-complete"}, "recursion_limit": 64}
    out = await graph.ainvoke(
        {
            "task_id": "t1",
            "graph_run_id": "r1",
            "workspace_id": "w1",
            "human_gates": {"link": False, "point": False, "review": False},
        },
        cfg,
    )
    assert out["agent_plan"]["status"] == "completed"


@pytest.mark.asyncio
async def test_default_gates_interrupt_at_coverage_design(saver):
    graph = build_graph(saver)
    cfg = {"configurable": {"thread_id": "t-gate"}, "recursion_limit": 80}
    result = await graph.ainvoke(
        {
            "task_id": "t1",
            "graph_run_id": "r1",
            "workspace_id": "w1",
            "human_gates": {"link": True, "point": True, "review": True},
        },
        cfg,
    )
    interrupted = isinstance(result, dict) and bool(result.get("__interrupt__"))
    snap = await graph.aget_state(cfg)
    assert interrupted or bool(snap.tasks)
    plan = snap.values["agent_plan"]
    by_id = {s["step_id"]: s for s in plan["steps"]}
    assert by_id["s1"]["status"] == "done"
    assert by_id["s2"]["kind"] == PlanStepKind.COVERAGE_DESIGN.value


# ---------- wrap 单元（独立小图，非生产拓扑） ----------


async def test_wrap_emits_node_events(saver):
    events: list = []
    ctx = _make_ctx(events=events)

    async def intake(ctx, state):
        return {"clauses": [{"clause_id": "c1"}]}

    g = StateGraph(TaskState)
    g.add_node("intake", wrap(intake, name="intake"))
    g.add_edge(START, "intake")
    g.add_edge("intake", END)
    graph = g.compile(checkpointer=saver)
    await graph.ainvoke({}, _cfg("w1", ctx))
    assert [t for t, _ in events] == ["node_start", "node_end"]
    assert events[0][1]["node"] == "intake"


async def test_wrap_cancel_before_node(saver):
    ctx = _make_ctx(cancelled=True)

    async def intake(ctx, state):
        return {}

    g = StateGraph(TaskState)
    g.add_node("intake", wrap(intake, name="intake"))
    g.add_edge(START, "intake")
    g.add_edge("intake", END)
    graph = g.compile(checkpointer=saver)
    with pytest.raises(TaskCancelled):
        await graph.ainvoke({}, _cfg("w2", ctx))


async def test_wrap_maps_unexpected_to_app_error(saver):
    ctx = _make_ctx()

    async def boom(ctx, state):
        raise RuntimeError("x")

    g = StateGraph(TaskState)
    g.add_node("intake", wrap(boom, name="intake"))
    g.add_edge(START, "intake")
    g.add_edge("intake", END)
    graph = g.compile(checkpointer=saver)
    with pytest.raises(AppError):
        await graph.ainvoke({}, _cfg("w3", ctx))


async def test_wrap_preserves_app_error(saver):
    ctx = _make_ctx()

    async def boom(ctx, state):
        raise ValidationError("bad", details={"node": "intake"})

    g = StateGraph(TaskState)
    g.add_node("intake", wrap(boom, name="intake"))
    g.add_edge(START, "intake")
    g.add_edge("intake", END)
    graph = g.compile(checkpointer=saver)
    with pytest.raises(ValidationError):
        await graph.ainvoke({}, _cfg("w4", ctx))


async def test_wrap_interrupt_resume(saver):
    ctx = _make_ctx()

    async def clarify(ctx, state):
        resume = interrupt({"node": "intake", "questions": [{"id": "q1"}]})
        return {"clarification_questions": resume}

    g = StateGraph(TaskState)
    g.add_node("intake", wrap(clarify, name="intake"))
    g.add_edge(START, "intake")
    g.add_edge("intake", END)
    graph = g.compile(checkpointer=saver)
    cfg = _cfg("w5", ctx)
    await graph.ainvoke({}, cfg)
    state = await graph.aget_state(cfg)
    assert state.next == ("intake",)
    await graph.ainvoke(Command(resume=[{"id": "q1", "answer": "a"}]), cfg)
    state = await graph.aget_state(cfg)
    assert state.next == ()
