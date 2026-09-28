"""WP-15 build_graph / wrap / gate 测试（dd §7.1 §7.2 §17.2）。

验收口径：图编译通过；断点位置断言（静态 interrupt_before + 运行期
aget_state.next）；wrap 的 ctx 注入/事件/计时/取消/异常映射；wrap 不破坏
interrupt()+Command(resume=) 函数式中断（与 SP-2 结论接线）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command, interrupt

from tester_agent.errors import AppError, TaskCancelled, ValidationError
from tester_agent.graph.constants import (
    GATE_CP1,
    GATE_CP2,
    STAGE_CASE_GENERATE,
    STAGE_COVERAGE_CHECK,
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from tester_agent.graph.main_graph import (
    STAGE_NODES,
    build_graph,
    build_legacy_stage_graph,
)
from tester_agent.graph.wrap import NodeNotImplemented
from tester_agent.runtime.context import AppContext, TaskContext

# asyncio_mode=auto（pyproject.toml）：async 测试自动标记，同步拓扑测试不挂标记


# ---------- 夹具与测试替身 ----------


def _make_ctx(*, cancelled: bool = False, events: list | None = None) -> TaskContext:
    """wrap 只消费 cancel_event/emit，其余依赖置 None（单测隔离）。"""
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


def _marker_node(stage: str, *, fail: Exception | None = None, bad_return: bool = False):
    """在 state.clauses 追加阶段标记的假节点（用冻结字段承载测试标记）。"""

    async def _node(ctx, state):
        if fail is not None:
            raise fail
        if bad_return:
            return ["not", "a", "dict"]
        return {"clauses": [*state.get("clauses", []), {"clause_id": stage}]}

    _node.__name__ = f"{stage}_node"
    return _node


def _all_fake_nodes(**kw) -> dict:
    return {stage: _marker_node(stage, **kw) for stage in STAGE_NODES}


@pytest.fixture
async def saver(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "checkpoints.db"), check_same_thread=False)
    sv = AsyncSqliteSaver(conn)
    await sv.setup()
    yield sv
    await conn.close()


# ---------- ① 拓扑与编译（验收：图编译通过；断点位置断言） ----------


def test_graph_compiles_without_checkpointer():
    graph = build_legacy_stage_graph(None)
    drawn = graph.get_graph()
    assert set(drawn.nodes) == {
        "__start__", "__end__", *STAGE_NODES, GATE_CP1, GATE_CP2,
    }
    edges = {(e.source, e.target) for e in drawn.edges}
    assert edges == {
        ("__start__", STAGE_INTAKE),
        (STAGE_INTAKE, STAGE_LINK_IDENTIFY),
        (STAGE_LINK_IDENTIFY, GATE_CP1),
        (GATE_CP1, STAGE_POINT_WRITE),
        (STAGE_POINT_WRITE, GATE_CP2),
        (GATE_CP2, STAGE_CASE_GENERATE),
        (STAGE_CASE_GENERATE, STAGE_COVERAGE_CHECK),
        (STAGE_COVERAGE_CHECK, "__end__"),
    }


def test_interrupt_before_static_breakpoints():
    graph = build_legacy_stage_graph(None)
    assert tuple(graph.interrupt_before_nodes) == (GATE_CP1, GATE_CP2)


def test_build_rejects_unknown_node_name():
    with pytest.raises(TypeError, match="未知阶段节点名"):
        build_graph(None, nodes={"not_a_stage": _marker_node("x")})


# ---------- ② 运行期断点 + 节点事件 ----------


async def test_full_run_pauses_at_both_gates_and_emits_node_events(saver):
    events: list = []
    ctx = _make_ctx(events=events)
    graph = build_graph(saver, nodes=_all_fake_nodes())
    cfg = _cfg("t1", ctx)

    # 首轮：intake + link_identify 后停在 cp1_gate
    await graph.ainvoke({"clauses": []}, cfg)
    state = await graph.aget_state(cfg)
    assert state.next == (GATE_CP1,)
    assert [c["clause_id"] for c in state.values["clauses"]] == [
        STAGE_INTAKE, STAGE_LINK_IDENTIFY,
    ]
    done_nodes = [p["node"] for t, p in events if t == "node_end"]
    assert done_nodes == [STAGE_INTAKE, STAGE_LINK_IDENTIFY]
    assert GATE_CP1 not in done_nodes  # gate 不发事件

    # 放行 CP1：point_write 后停在 cp2_gate
    await graph.ainvoke(None, cfg)
    state = await graph.aget_state(cfg)
    assert state.next == (GATE_CP2,)
    assert [c["clause_id"] for c in state.values["clauses"]] == [
        STAGE_INTAKE, STAGE_LINK_IDENTIFY, STAGE_POINT_WRITE,
    ]

    # 放行 CP2：case_generate + coverage_check 到 END
    await graph.ainvoke(None, cfg)
    state = await graph.aget_state(cfg)
    assert state.next == ()
    assert [c["clause_id"] for c in state.values["clauses"]] == list(STAGE_NODES)

    # 事件契约（dd §10.4）：每节点 start/end 成对，latency_ms 非负整数，无 gate 事件
    starts = [p for t, p in events if t == "node_start"]
    ends = [p for t, p in events if t == "node_end"]
    assert [p["node"] for p in starts] == list(STAGE_NODES)
    assert [p["node"] for p in ends] == list(STAGE_NODES)
    assert all(set(p) == {"node", "stage_version", "batch_id"} for p in starts)
    assert all(p["batch_id"] is None for p in starts)
    assert all(set(p) == {"node", "latency_ms"} for p in ends)
    assert all(isinstance(p["latency_ms"], int) and p["latency_ms"] >= 0 for p in ends)
    assert all(GATE_CP1 not in p["node"] and GATE_CP2 not in p["node"] for p in starts)


async def test_node_start_reads_stage_version_from_state(saver):
    events: list = []
    ctx = _make_ctx(events=events)
    graph = build_graph(saver, nodes=_all_fake_nodes())
    await graph.ainvoke(
        {"clauses": [], "current_stage_version": {STAGE_INTAKE: 3, STAGE_LINK_IDENTIFY: 1}},
        _cfg("t2", ctx),
    )
    starts = {p["node"]: p["stage_version"] for t, p in events if t == "node_start"}
    assert starts[STAGE_INTAKE] == 3
    assert starts[STAGE_LINK_IDENTIFY] == 1


async def test_ctx_object_injected_into_node(saver):
    seen: list = []
    ctx = _make_ctx()

    async def probe(ctx_arg, state):
        seen.append(ctx_arg)
        return {"clauses": [*state.get("clauses", []), {"clause_id": "probe"}]}

    graph = build_graph(
        saver,
        nodes={
            STAGE_INTAKE: probe,
            **{s: _marker_node(s) for s in STAGE_NODES if s != STAGE_INTAKE},
        },
    )
    await graph.ainvoke({"clauses": []}, _cfg("t3", ctx))
    await graph.ainvoke(None, _cfg("t3", ctx))
    await graph.ainvoke(None, _cfg("t3", ctx))
    assert seen and all(item is ctx for item in seen)


async def test_gate_passes_through_without_state_mutation(saver):
    ctx = _make_ctx()
    graph = build_graph(saver, nodes=_all_fake_nodes())
    cfg = _cfg("t4", ctx)
    await graph.ainvoke({"clauses": []}, cfg)
    await graph.ainvoke(None, cfg)
    state = await graph.aget_state(cfg)
    # 已过 cp1：gate 未注入任何键，state 仅含图入口键与节点写的 clauses
    assert set(state.values) == {"clauses"}


# ---------- ③ wrap：取消 / 异常映射 ----------


async def test_cancel_before_node_raises_task_cancelled(saver):
    ctx = _make_ctx(cancelled=True)
    graph = build_graph(saver, nodes=_all_fake_nodes())
    with pytest.raises(TaskCancelled):
        await graph.ainvoke({"clauses": []}, _cfg("t5", ctx))


async def test_task_cancelled_inside_node_passes_through(saver):
    ctx = _make_ctx()
    graph = build_graph(
        saver,
        nodes={
            STAGE_INTAKE: _marker_node(STAGE_INTAKE, fail=TaskCancelled("batch edge")),
            **{s: _marker_node(s) for s in STAGE_NODES if s != STAGE_INTAKE},
        },
    )
    with pytest.raises(TaskCancelled):
        await graph.ainvoke({"clauses": []}, _cfg("t6", ctx))


async def test_app_error_inside_node_passes_through(saver):
    ctx = _make_ctx()
    err = ValidationError("bad artifact", details={"x": 1})
    graph = build_graph(
        saver,
        nodes={
            STAGE_INTAKE: _marker_node(STAGE_INTAKE, fail=err),
            **{s: _marker_node(s) for s in STAGE_NODES if s != STAGE_INTAKE},
        },
    )
    with pytest.raises(ValidationError) as ei:
        await graph.ainvoke({"clauses": []}, _cfg("t7", ctx))
    assert ei.value is err
    assert ei.value.code == "VALIDATION_BODY"


async def test_unknown_error_wrapped_to_internal(saver):
    ctx = _make_ctx()
    graph = build_graph(
        saver,
        nodes={
            STAGE_INTAKE: _marker_node(STAGE_INTAKE, fail=RuntimeError("boom")),
            **{s: _marker_node(s) for s in STAGE_NODES if s != STAGE_INTAKE},
        },
    )
    with pytest.raises(AppError) as ei:
        await graph.ainvoke({"clauses": []}, _cfg("t8", ctx))
    assert ei.value.code == "INTERNAL"
    assert ei.value.http_status == 500
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert ei.value.details["node"] == STAGE_INTAKE


async def test_non_dict_result_wrapped_to_internal(saver):
    ctx = _make_ctx()
    graph = build_graph(
        saver,
        nodes={
            STAGE_INTAKE: _marker_node(STAGE_INTAKE, bad_return=True),
            **{s: _marker_node(s) for s in STAGE_NODES if s != STAGE_INTAKE},
        },
    )
    with pytest.raises(AppError) as ei:
        await graph.ainvoke({"clauses": []}, _cfg("t9", ctx))
    assert ei.value.code == "INTERNAL"


async def test_missing_ctx_in_config_raises_app_error(saver):
    graph = build_graph(saver, nodes=_all_fake_nodes())
    with pytest.raises(AppError) as ei:
        await graph.ainvoke(
            {"clauses": []}, {"configurable": {"thread_id": "t10"}}
        )
    assert ei.value.code == "INTERNAL"


# ---------- ④ wrap 不破坏 interrupt()/Command(resume=)（SP-2 接线） ----------


async def test_functional_interrupt_inside_wrapped_node(saver):
    ctx = _make_ctx()

    async def intake_with_clarify(ctx_arg, state):
        answer = interrupt({"q": "missing role?"})
        return {
            "clauses": [
                *state.get("clauses", []),
                {"clause_id": f"{STAGE_INTAKE}:{answer}"},
            ],
            "clarification_questions": [],
        }

    graph = build_graph(
        saver,
        nodes={
            STAGE_INTAKE: intake_with_clarify,
            **{s: _marker_node(s) for s in STAGE_NODES if s != STAGE_INTAKE},
        },
    )
    cfg = _cfg("t11", ctx)

    result = await graph.ainvoke({"clauses": []}, cfg)
    assert "__interrupt__" in result
    state = await graph.aget_state(cfg)
    assert state.next == (STAGE_INTAKE,)
    assert state.tasks[0].interrupts[0].value == {"q": "missing role?"}

    await graph.ainvoke(Command(resume="管理员"), cfg)
    state = await graph.aget_state(cfg)
    # 恢复后继续 link_identify，停在 cp1
    assert state.next == (GATE_CP1,)
    assert [c["clause_id"] for c in state.values["clauses"]] == [
        f"{STAGE_INTAKE}:管理员", STAGE_LINK_IDENTIFY,
    ]


# ---------- ⑤ 占位节点 ----------


async def test_stub_node_fails_internal_when_reached(saver):
    ctx = _make_ctx()
    graph = build_legacy_stage_graph(saver)  # 全占位
    with pytest.raises(AppError) as ei:
        await graph.ainvoke({"clauses": []}, _cfg("t12", ctx))
    assert ei.value.code == "INTERNAL"
    assert isinstance(ei.value.__cause__, NodeNotImplemented)


# ---------- ⑥ emit 缺省（WP-21 前）静默，不影响图运行 ----------


async def test_none_emitter_is_silent(saver):
    ctx = _make_ctx(events=None)
    assert ctx.emit is None
    graph = build_graph(saver, nodes=_all_fake_nodes())
    await graph.ainvoke({"clauses": []}, _cfg("t13", ctx))
    await graph.ainvoke(None, _cfg("t13", ctx))
    await graph.ainvoke(None, _cfg("t13", ctx))
    state = await graph.aget_state(_cfg("t13", ctx))
    assert state.next == ()
    assert [c["clause_id"] for c in state.values["clauses"]] == list(STAGE_NODES)
