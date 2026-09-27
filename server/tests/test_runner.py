"""WP-22 TaskRegistry + Runner + 状态迁移表测试（dd §6.3 §6.4）。

验收口径（WBS #22）：重复 run → 409；心跳 0 行更新自杀。另覆盖：
- 状态机全表合法/非法迁移；
- Registry：owner token 互斥、跨进程新鲜心跳判活、waiting_* 残留心跳不阻塞、
  错误 token 不能释放；
- Runner：completed/task_done、CP waiting_confirm/checkpoint_waiting、
  clarification waiting_input/clarification_needed、failed/task_error、
  cancel→aborted、failed 游标恢复。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fakes import FakeLLM, FakeReMeReader
from tester_agent.adapters.reme import ReMeReaderFactory
from tester_agent.errors import (
    AppError,
    NotFoundError,
    TaskBusyError,
    TaskCancelled,
    TaskStateConflict,
    ValidationError,
)
from tester_agent.graph.constants import (
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from tester_agent.graph.main_graph import build_graph
from tester_agent.graph.registry import CASE_DESIGNER, GraphRegistry
from tester_agent.graph.state import TaskState
from tester_agent.graph.wrap import wrap
from tester_agent.runtime.bus import EventBus
from tester_agent.runtime.context import AppContext
from tester_agent.runtime.runner import (
    TRANSITIONS,
    Runner,
    TaskRegistry,
    validate_transition,
)
from tester_agent.store.db import Database, run_migrations, utcnow_iso
from tester_agent.store.models import (
    ArtifactRow,
    ConfigDAO,
    EventDAO,
    TaskDAO,
    TaskRow,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

WS, CONV, TASK, RUN = "ws1", "conv1", "task1", "run-1"


# ---------- 夹具 ----------


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "app.db"
    run_migrations(db_path)
    handle = Database(db_path)
    yield handle
    handle.close()


@pytest.fixture()
async def saver(tmp_path):
    conn = await aiosqlite.connect(
        str(tmp_path / "checkpoints.db"), check_same_thread=False
    )
    sv = AsyncSqliteSaver(conn)
    await sv.setup()
    yield sv
    await conn.close()


async def _seed(db, *, status: str = "waiting_input",
                stage: str = STAGE_INTAKE, heartbeat: str | None = None) -> None:
    await WorkspaceDAO(db).create(
        WorkspaceRow.create(
            id=WS, name="ws",
            kb_config={"mode": "sdk", "target": "lark", "kb_id": "KB1"},
        )
    )
    await db.aexecute(
        "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (CONV, WS, "t", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
    )
    await TaskDAO(db).create(
        TaskRow.create(
            id=TASK, conversation_id=CONV, workspace_id=WS,
            status=status, current_stage=stage,
            langgraph_thread_id="th-1", graph_run_id=RUN,
        )
    )
    if heartbeat is not None:
        await db.aexecute(
            "UPDATE task SET runner_heartbeat = ? WHERE id = ?",
            (heartbeat, TASK),
        )


async def _build_app(tmp_path, db, graph, *, registry=None, bus=None,
                     heartbeat_interval: float = 0.05):
    store = FileStore(tmp_path / "wsfiles")
    reader = FakeReMeReader([])
    factory = ReMeReaderFactory()

    async def _builder(_kb_config):
        return reader

    factory.register("sdk", _builder)
    bus = bus or EventBus(EventDAO(db))
    registry = registry or TaskRegistry(TaskDAO(db))
    app_ctx = AppContext(
        db=db,
        file_store=store,
        llm=FakeLLM([]),
        reme_factory=factory,
        config=ConfigDAO(db),
        graphs=GraphRegistry.from_graph(CASE_DESIGNER, graph),
        bus=bus,
        registry=registry,
    )
    runner = Runner(app_ctx, heartbeat_interval=heartbeat_interval)
    return app_ctx, registry, runner


async def _event_rows(db):
    return await EventDAO(db).list_after(TASK, 0)


async def _await_task(registry, *, timeout: float = 5.0):
    task = registry.get_task(TASK)
    assert task is not None
    await asyncio.wait_for(task, timeout=timeout)


# ---------- 图构造辅助 ----------


def _marker_node(i: int):
    async def _node(ctx, state):
        return {"clauses": [*state.get("clauses", []), {"i": i}]}

    return _node


def _linear_graph(checkpointer, *node_fns):
    """线性 START -> n0 -> n1 ... -> END 图（节点经 wrap）。"""
    g = StateGraph(TaskState)
    prev = START
    for i, fn in enumerate(node_fns):
        name = f"n{i}"
        g.add_node(name, wrap(fn, name=name))
        g.add_edge(prev, name)
        prev = name
    g.add_edge(prev, END)
    return g.compile(checkpointer=checkpointer)


def _wait_cancel_node():
    """等到取消信号后在边界抛 TaskCancelled（模拟节点侧批次边界检查）。"""

    async def _node(ctx, state):
        while not await ctx.cancelled():
            await asyncio.sleep(0.01)
        raise TaskCancelled("cancel at batch boundary")

    return _node


# ---------- ① 状态机（dd §6.3） ----------


@pytest.mark.parametrize(
    "current,event,target",
    sorted((c, e, t) for (c, e), t in TRANSITIONS.items()),
)
def test_legal_transitions(current, event, target):
    assert validate_transition(current, event) == target


@pytest.mark.parametrize(
    "current,event",
    [
        ("completed", "run"),
        ("waiting_confirm", "run"),
        ("failed", "confirm"),
        ("aborted", "complete"),
        ("running", "answer"),
        ("unknown_status", "run"),
    ],
)
def test_illegal_transition_conflict(current, event):
    with pytest.raises(TaskStateConflict):
        validate_transition(current, event)


# ---------- ② TaskRegistry ----------


class TestTaskRegistry:
    async def test_acquire_returns_token_and_is_running(self, db):
        await _seed(db)
        registry = TaskRegistry(TaskDAO(db))
        token = await registry.acquire(TASK)
        assert isinstance(token, str) and token
        assert registry.is_running(TASK)
        await registry.release(TASK, token)
        assert not registry.is_running(TASK)

    async def test_duplicate_acquire_busy(self, db):
        await _seed(db)
        registry = TaskRegistry(TaskDAO(db))
        await registry.acquire(TASK)
        with pytest.raises(TaskBusyError):
            await registry.acquire(TASK)

    async def test_wrong_token_cannot_release(self, db):
        await _seed(db)
        registry = TaskRegistry(TaskDAO(db))
        token = await registry.acquire(TASK)
        await registry.release(TASK, "wrong-token")
        assert registry.is_running(TASK)
        await registry.release(TASK, token)
        assert not registry.is_running(TASK)

    async def test_cross_process_fresh_heartbeat_busy(self, db):
        await _seed(db, status="running", heartbeat=utcnow_iso())
        registry = TaskRegistry(TaskDAO(db))
        with pytest.raises(TaskBusyError):
            await registry.acquire(TASK)

    async def test_cross_process_stale_heartbeat_allowed(self, db):
        old = "2026-09-25T00:00:00.000Z"  # 2 天前，超过 120s 阈值
        await _seed(db, status="running", heartbeat=old)
        registry = TaskRegistry(TaskDAO(db))
        token = await registry.acquire(TASK)
        assert registry.is_running(TASK)
        await registry.release(TASK, token)

    async def test_waiting_status_fresh_heartbeat_allowed(self, db):
        """图中断即 Runner 释放，残留心跳不阻塞 waiting_* 续跑（dd §6.4①）。"""
        await _seed(db, status="waiting_confirm", heartbeat=utcnow_iso())
        registry = TaskRegistry(TaskDAO(db))
        token = await registry.acquire(TASK)
        assert registry.is_running(TASK)
        await registry.release(TASK, token)

    async def test_acquire_missing_task_not_found(self, db):
        registry = TaskRegistry(TaskDAO(db))
        with pytest.raises(NotFoundError):
            await registry.acquire("no-such-task")


# ---------- ③ Runner：正常结束 ----------


class TestRunnerCompletion:
    async def test_full_run_completed_and_task_done(self, tmp_path, db, saver):
        await _seed(db)
        graph = _linear_graph(
            saver, _marker_node(0), _marker_node(1), _marker_node(2)
        )
        _app, registry, runner = await _build_app(tmp_path, db, graph)

        handle = await runner.start(TASK)
        assert handle.task_id == TASK
        assert handle.graph_run_id == RUN
        assert handle.events_url == f"/api/v1/tasks/{TASK}/events"

        await _await_task(registry)
        task = await TaskDAO(db).get(TASK)
        assert task.status == "completed"
        assert not registry.is_running(TASK)

        rows = await _event_rows(db)
        assert rows[-1].type == "task_done"
        assert rows[-1].payload_dict() == {"status": "completed"}
        # wrap 事件：3 节点 start/end 成对
        starts = [r for r in rows if r.type == "node_start"]
        ends = [r for r in rows if r.type == "node_end"]
        assert len(starts) == len(ends) == 3

    async def test_duplicate_start_busy_409(self, tmp_path, db, saver):
        await _seed(db)
        gate = asyncio.Event()

        async def hold(ctx, state):
            await gate.wait()
            return {}

        graph = _linear_graph(saver, hold)
        _app, registry, runner = await _build_app(tmp_path, db, graph)

        await runner.start(TASK)
        with pytest.raises(TaskBusyError):
            await runner.start(TASK)

        gate.set()
        await _await_task(registry)

    async def test_illegal_start_event_conflict_and_releases(
        self, tmp_path, db, saver
    ):
        await _seed(db, status="completed", stage=STAGE_POINT_WRITE)
        graph = _linear_graph(saver, _marker_node(0))
        _app, registry, runner = await _build_app(tmp_path, db, graph)

        with pytest.raises(TaskStateConflict):
            await runner.start(TASK)
        assert not registry.is_running(TASK)


# ---------- ④ Runner：中断（CP / 澄清） ----------


class TestRunnerInterrupts:
    async def test_pauses_at_cp1_waiting_confirm(self, tmp_path, db, saver):
        await _seed(db)

        async def link_with_artifact(ctx, state):
            await ctx.daos.artifact.put(
                ArtifactRow.create(
                    id="art-link", task_id=TASK, stage=STAGE_LINK_IDENTIFY,
                    graph_run_id=RUN, stage_version=1, payload={"links": []},
                )
            )
            return {}

        graph = build_graph(
            saver,
            nodes={
                STAGE_INTAKE: _marker_node(0),
                STAGE_LINK_IDENTIFY: link_with_artifact,
                STAGE_POINT_WRITE: _marker_node(2),
            },
        )
        _app, registry, runner = await _build_app(tmp_path, db, graph)

        await runner.start(TASK)
        await _await_task(registry)

        task = await TaskDAO(db).get(TASK)
        assert task.status == "waiting_confirm"
        assert task.current_stage == STAGE_LINK_IDENTIFY

        rows = await _event_rows(db)
        cp = [r for r in rows if r.type == "checkpoint_waiting"]
        assert len(cp) == 1
        assert cp[-1].payload_dict() == {
            "stage": STAGE_LINK_IDENTIFY,
            "artifact_id": "art-link",
            "stage_version": 1,
        }

    async def test_clarification_waiting_input(self, tmp_path, db, saver):
        await _seed(db)
        questions = [
            {"id": "q1", "question": "角色是谁？", "options": ["管理员"]}
        ]

        async def intake_with_clarify(ctx, state):
            interrupt({"node": "intake", "questions": questions})
            return {}

        graph = _linear_graph(saver, intake_with_clarify)
        _app, registry, runner = await _build_app(tmp_path, db, graph)

        await runner.start(TASK)
        await _await_task(registry)

        task = await TaskDAO(db).get(TASK)
        assert task.status == "waiting_input"
        assert task.current_stage == STAGE_INTAKE

        rows = await _event_rows(db)
        clar = [r for r in rows if r.type == "clarification_needed"]
        assert len(clar) == 1
        assert clar[-1].payload_dict() == {"questions": questions}


# ---------- ⑤ Runner：失败 ----------


class TestRunnerFailure:
    async def test_node_app_error_failed_and_task_error(
        self, tmp_path, db, saver
    ):
        await _seed(db)

        async def boom(ctx, state):
            raise ValidationError(
                "输入不合法", details={"node": "intake"}
            )

        graph = _linear_graph(saver, boom)
        _app, registry, runner = await _build_app(tmp_path, db, graph)

        await runner.start(TASK)
        await _await_task(registry)

        task = await TaskDAO(db).get(TASK)
        assert task.status == "failed"
        info = task.error_obj()
        assert info.code == "VALIDATION_BODY"
        assert info.message == "输入不合法"
        assert info.retryable is False
        assert info.node == "intake"

        rows = await _event_rows(db)
        err = [r for r in rows if r.type == "task_error"]
        assert len(err) == 1
        payload = err[-1].payload_dict()
        assert payload == {
            "code": "VALIDATION_BODY",
            "message": "输入不合法",
            "retryable": False,
            "node": "intake",
        }


# ---------- ⑥ Runner：游标恢复 ----------


class TestRunnerResume:
    async def test_failed_run_resumes_from_checkpoint(
        self, tmp_path, db, saver
    ):
        await _seed(db)
        attempts = {"n": 0}

        async def flaky(ctx, state):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise AppError("瞬时故障")
            return {}

        graph = _linear_graph(saver, flaky)
        _app, registry, runner = await _build_app(tmp_path, db, graph)

        await runner.start(TASK)
        await _await_task(registry)
        assert (await TaskDAO(db).get(TASK)).status == "failed"
        assert attempts["n"] == 1

        # failed --run--> running：checkpoint 已存在 → input None 恢复
        await runner.start(TASK)
        await _await_task(registry)
        assert attempts["n"] == 2
        assert (await TaskDAO(db).get(TASK)).status == "completed"
        rows = await _event_rows(db)
        assert rows[-1].type == "task_done"


# ---------- ⑦ Runner：取消 / 心跳自杀 ----------


class TestRunnerCancel:
    async def test_cancel_request_aborts(self, tmp_path, db, saver):
        await _seed(db)
        graph = _linear_graph(saver, _wait_cancel_node())
        _app, registry, runner = await _build_app(tmp_path, db, graph)

        await runner.start(TASK)
        await asyncio.sleep(0.15)  # 等 Runner 进入节点
        await TaskDAO(db).request_cancel(TASK)

        await _await_task(registry)
        assert (await TaskDAO(db).get(TASK)).status == "aborted"
        assert not registry.is_running(TASK)

    async def test_heartbeat_zero_rows_self_kill(self, tmp_path, db, saver):
        await _seed(db)
        graph = _linear_graph(saver, _wait_cancel_node())
        _app, registry, runner = await _build_app(tmp_path, db, graph)

        await runner.start(TASK)
        await asyncio.sleep(0.15)
        # 模拟 Reaper/外部改判：run 被换走，心跳 UPDATE 匹配 0 行
        await db.aexecute(
            "UPDATE task SET graph_run_id = 'stolen-run' WHERE id = ?",
            (TASK,),
        )

        await _await_task(registry)
        assert (await TaskDAO(db).get(TASK)).status == "aborted"
