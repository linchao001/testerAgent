"""任务注册表 + Runner + 状态迁移表（dd §6.3 §6.4）。

- :func:`validate_transition`：任务状态机唯一裁决点（API 层 WP-25~27 与
  Runner 共用），非法迁移抛 :class:`TaskStateConflict`（dd §6.3）；
- :class:`TaskRegistry`：内存 owner token 锁（防同进程并发）+ DB 心跳判活
  （防跨重启/跨进程误判），二者分工（dd §6.4①）；
- :class:`Runner`：``start`` 获取锁后后台 asyncio.Task 内构造 TaskContext
  （配置快照冻结 R21）、心跳循环、启动/恢复图，并按图结果收口终态
  （completed / waiting_confirm / waiting_input / failed / aborted），
  发 task_done / task_error / checkpoint_waiting / clarification_needed。

与 dd §6.4 的架构差异（沿用 WP-15/21 决策，交接单登记）：
1. 节点事件（node_start/node_end 等）由节点经 ``ctx.emit`` 直接发射，
   不再额外挂 LangGraph EventBridge callback；Emitter 已在 _build_ctx 接线；
2. langgraph 1.2 的 ``ainvoke`` 遇中断**不抛异常**而返回含
   ``__interrupt__`` 的 dict，Runner 经 ``aget_state`` 判定中断类型。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from langgraph.types import Command

from ..adapters.reme import IndexMirror
from ..errors import (
    AppError,
    TaskBusyError,
    TaskCancelled,
    TaskStateConflict,
    new_trace_id,
    trace_id_var,
)
from ..domain import PlanStepKind
from ..graph.constants import (
    HEARTBEAT_INTERVAL_SEC,
    HEARTBEAT_STALE_SEC,
)
from ..graph.registry import CASE_DESIGNER
from ..tools.capabilities import KIND_TO_STAGE
from ..logging_config import get_logger
from ..runtime.bus import Emitter
from ..store.models import (
    ArtifactDAO,
    EventDAO,
    MessageDAO,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
    TraceDAO,
    WorkspaceDAO,
    _UNSET,
)

if TYPE_CHECKING:
    from ..store.db import Database
    from ..store.workspace_files import FileStore
    from .context import AppContext, TaskContext

logger = get_logger(__name__)


# ---------- §6.3 任务状态机 ----------

#: 合法迁移表 ``(当前状态, 事件) -> 目标状态``（dd §6.3 全表）
TRANSITIONS: dict[tuple[str, str], str] = {
    ("waiting_input", "run"): "running",
    ("running", "checkpoint"): "waiting_confirm",
    ("running", "clarify"): "waiting_input",
    ("running", "complete"): "completed",
    ("running", "fail"): "failed",
    ("running", "abort"): "aborted",
    ("waiting_confirm", "confirm"): "running",
    ("waiting_confirm", "rollback"): "running",
    ("waiting_input", "answer"): "running",
    ("waiting_confirm", "cancel"): "aborted",
    ("waiting_input", "cancel"): "aborted",
    ("running", "cancel"): "cancelling",
    ("cancelling", "abort"): "aborted",
    ("failed", "run"): "running",
    ("aborted", "run"): "running",
    ("completed", "rollback"): "running",
    ("completed", "regenerate"): "running",
}


def validate_transition(current: str, event: str) -> str:
    """校验状态迁移，返回目标状态；非法迁移 → ``TASK_STATE_CONFLICT``（dd §6.3）。"""
    target = TRANSITIONS.get((str(current), str(event)))
    if target is None:
        raise TaskStateConflict(
            f"非法任务状态迁移：{current} --{event}--> ?",
            details={"current": current, "event": event},
        )
    return target


# ---------- §6.4 TaskRegistry ----------


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _heartbeat_fresh(heartbeat: str | None, stale_sec: int) -> bool:
    """心跳是否新鲜（距今不足 ``stale_sec``）；空值/非法 → 不新鲜。"""
    if not heartbeat:
        return False
    ts = _parse_iso(heartbeat)
    if ts is None:
        return False
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age < stale_sec


class TaskRegistry:
    """任务运行锁注册表（dd §6.4）。

    内存 owner token 防同进程并发；acquire 时另查 DB：仅当任务处于
    running/cancelling 且心跳新鲜才以 409 拒绝（跨进程/未释放判据）。
    waiting_* 的残留心跳不阻塞续跑——图中断即 Runner 释放、心跳停更。
    """

    def __init__(
        self,
        task_dao: TaskDAO,
        *,
        stale_sec: int = HEARTBEAT_STALE_SEC,
    ) -> None:
        self._task_dao = task_dao
        self._stale_sec = stale_sec
        self._owners: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def acquire(self, task_id: str) -> str:
        """获取任务执行权，返回 owner_token；占用中 → :class:`TaskBusyError`（409）。

        任务不存在由 TaskDAO.get 抛 NotFound（404）。
        """
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            if task_id in self._owners:
                raise TaskBusyError(
                    f"任务正在执行：{task_id}",
                    details={"task_id": task_id, "reason": "local_owner"},
                )
            row = await self._task_dao.get(task_id)
            if row.status in ("running", "cancelling") and _heartbeat_fresh(
                row.runner_heartbeat, self._stale_sec
            ):
                raise TaskBusyError(
                    f"任务在其他执行方处运行：{task_id}",
                    details={"task_id": task_id, "reason": "fresh_heartbeat"},
                )
            token = uuid.uuid4().hex
            self._owners[task_id] = token
            return token

    async def release(self, task_id: str, token: str) -> None:
        """释放执行权（仅 token 匹配者可释放，防迟到任务误删新 owner）。"""
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            if self._owners.get(task_id) == token:
                del self._owners[task_id]
                self._tasks.pop(task_id, None)

    def attach(self, task_id: str, task: asyncio.Task) -> None:
        self._tasks[task_id] = task

    def is_running(self, task_id: str) -> bool:
        return task_id in self._owners

    def get_task(self, task_id: str) -> asyncio.Task | None:
        return self._tasks.get(task_id)

    def all_tasks(self) -> list[asyncio.Task]:
        """当前全部在飞 run 任务（优雅关闭统一取消，dd §6.5 关闭序列）。"""
        return list(self._tasks.values())


# ---------- §6.4 Runner ----------


async def build_task_context(app: "AppContext", task_id: str) -> "TaskContext":
    """构造单次图运行的依赖包 :class:`TaskContext`（dd §6.1，配置快照 R21）。

    Runner 主循环（``_run``）与 WP-26 API 层入口（回退 ctx / regenerate
    入口）共用，保证读到的 task 行、DAO 组、Emitter、cancel_event 构造
    口径一致。cancel_event 为新事件——由调用方决定是否桥接 DB 取消标志
    （Runner 心跳循环负责；API 层的 regenerate 入口经心跳语义外直接检查）。
    """
    from .context import DAOs, TaskContext

    db = app.db
    task_dao = TaskDAO(db)
    task: TaskRow = await task_dao.get(task_id)

    ws = await WorkspaceDAO(db).get(task.workspace_id)
    reader = await app.reme_factory.for_workspace(
        task.workspace_id, ws.kb_config_obj()
    )

    from ..store.models import ContextJournalDAO

    journal_dao = ContextJournalDAO(db)
    daos = DAOs(
        task=task_dao,
        message=MessageDAO(db),
        artifact=ArtifactDAO(db),
        testcase=TestcaseDAO(db),
        trace=TraceDAO(db),
        event=EventDAO(db),
        journal=journal_dao,
    )
    bus = app.bus
    context_store = None
    runtime_cfg: dict = {}
    try:
        from ..store.models import ConfigDAO

        runtime_cfg = (await ConfigDAO(db).get()).runtime_dict()
    except Exception:  # noqa: BLE001
        runtime_cfg = {}
    if bool(runtime_cfg.get("context.enabled", True)):
        try:
            context_store = await app.context_registry.restore_owner(
                owner_type="task",
                owner_id=task_id,
                workspace_id=task.workspace_id,
                daos=daos,
                journal_sink=journal_dao,
                policy_version=str(
                    runtime_cfg.get("context.policy_version", "cp-v1")
                ),
                step_window=int(runtime_cfg.get("context.step_window", 1)),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "context store restore failed; continuing without context_store",
                extra={"task_id": task_id},
            )
    return TaskContext(
        app=app,
        task=task,
        run_id=task.graph_run_id,
        files=app.file_store,
        reader=reader,
        snapshot_level=task.snapshot_level,
        mirror=IndexMirror(reader),
        agent_config={},
        daos=daos,
        emit=Emitter(bus, task_id) if bus is not None else None,
        cancel_event=asyncio.Event(),
        context_store=context_store,
    )


@dataclass(frozen=True)
class RunHandle:
    """``Runner.start`` 立即返回的句柄（后台执行，不阻塞到图结束）。"""

    task_id: str
    graph_run_id: str
    events_url: str


class Runner:
    """图运行器（dd §6.4）。

    :param app: 进程组合根（需已接 db/file_store/reme_factory/graphs/bus/registry）。
    """

    def __init__(
        self,
        app: "AppContext",
        *,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SEC,
    ) -> None:
        self._app = app
        self._heartbeat_interval = heartbeat_interval

    # ---- 入口 ----

    async def start(
        self,
        task_id: str,
        *,
        event: str | None = "run",
        resume: Any = None,
    ) -> RunHandle:
        """启动/续跑任务（后台 asyncio.Task），立即返回 :class:`RunHandle`。

        acquire（409/404）与状态迁移校验（409）在返回句柄前同步完成，
        调用方（API WP-26）可直接把异常翻译成错误信封。

        :param event: 状态机事件（``run``/``confirm``/``answer``）；
            ``None`` 表示调用方（WP-26 回退端点）已在协议事务内完成迁移
            裁决并落账（task 已切 running/新 run），此处只负责接管执行，
            跳过迁移校验（dd §11.2 回退时序）。
        :param resume: 非空时作为 ``Command(resume=resume)`` 恢复图运行
            （函数式 ``interrupt()`` 澄清答复，dd §7.6 answer 行）；
            缺省走启动/静态 gate 恢复（``ainvoke(None)``）。
        """
        registry = self._app.registry
        token = await registry.acquire(task_id)
        try:
            task = await TaskDAO(self._app.db).get(task_id)
            if event is not None:
                target = validate_transition(task.status, event)
                if target != "running":
                    await registry.release(task_id, token)
                    raise TaskStateConflict(
                        f"该事件不会启动任务：{task.status} --{event}--> {target}",
                        details={"current": task.status, "event": event, "target": target},
                    )
        except TaskStateConflict:
            await registry.release(task_id, token)
            raise
        run_task = asyncio.create_task(
            self._run(task_id, token, resume),
            name=f"task-{task_id}",
        )
        registry.attach(task_id, run_task)
        return RunHandle(
            task_id=task_id,
            graph_run_id=task.graph_run_id,
            events_url=f"/api/v1/tasks/{task_id}/events",
        )

    # ---- 主循环 ----

    async def _run(self, task_id: str, token: str, resume: Any = None) -> None:
        trace_id_var.set(new_trace_id())
        ctx: "TaskContext | None" = None
        hb: asyncio.Task | None = None
        try:
            ctx = await self._build_ctx(task_id)
            await ctx.daos.task.update_status(
                task_id, status="running", heartbeat=True
            )
            hb = asyncio.create_task(self._heartbeat_loop(ctx))

            graph = self._app.graphs.get(CASE_DESIGNER)
            invoke_cfg = {
                "configurable": {
                    "thread_id": ctx.task.langgraph_thread_id,
                    "ctx": ctx,
                }
            }
            if resume is not None:
                # 函数式 interrupt 恢复（澄清答复，dd §7.6 answer 行）
                graph_input: Any = Command(resume=resume)
            else:
                graph_input = await self._initial_or_resume_input(
                    graph, ctx, invoke_cfg
                )
            result = await graph.ainvoke(graph_input, invoke_cfg)

            await self._settle_after_invoke(ctx, graph, invoke_cfg, result)
        except TaskCancelled:
            await self._mark_aborted(task_id)
        except asyncio.CancelledError:
            # 进程优雅关闭（WP-23）：不做终态落账，Reaper 重启时改判。
            raise
        except AppError as e:
            await self._mark_failed(task_id, e)
        except Exception as e:  # noqa: BLE001 —— Runner 是 run 级唯一兜底边界
            logger.exception(
                "runner unhandled error",
                extra={"task_id": task_id},
            )
            await self._mark_failed(task_id, AppError(str(e)))
        finally:
            if hb is not None:
                hb.cancel()
            await self._app.registry.release(task_id, token)

    # ---- TaskContext 构造（配置快照冻结 R21） ----

    async def _build_ctx(self, task_id: str) -> "TaskContext":
        return await build_task_context(self._app, task_id)

    async def _initial_or_resume_input(
        self,
        graph: Any,
        ctx: "TaskContext",
        invoke_cfg: dict,
    ) -> Any:
        """首次启动 → 初始 state；线程已有 checkpoint（failed/aborted 重跑）→ None 恢复。"""
        snapshot = await graph.aget_state(invoke_cfg)
        if snapshot.values:
            return None
        gates = {"link": True, "point": True, "review": True}
        cfg_dao = getattr(ctx.app, "config", None)
        if cfg_dao is not None:
            try:
                crow = await cfg_dao.get()
                rt = crow.runtime_dict() if crow is not None else {}
            except Exception:  # noqa: BLE001 — 配置缺失时用默认门禁
                rt = {}
            if isinstance(rt, dict):
                gates = {
                    "link": bool(rt.get("human_gate_link", True)),
                    "point": bool(rt.get("human_gate_point", True)),
                    "review": bool(rt.get("human_gate_review", True)),
                }
        return {
            "task_id": ctx.task.id,
            "graph_run_id": ctx.run_id,
            "workspace_id": ctx.task.workspace_id,
            "human_gates": gates,
        }

    # ---- 终态收口 ----

    async def _settle_after_invoke(
        self,
        ctx: "TaskContext",
        graph: Any,
        invoke_cfg: dict,
        result: Any,
    ) -> None:
        """按 ainvoke 后的图状态置 waiting_confirm / waiting_input / completed。"""
        state = await graph.aget_state(invoke_cfg)
        nxt = tuple(state.next or ())
        interrupted = isinstance(result, dict) and bool(result.get("__interrupt__"))

        if not interrupted and not nxt:
            await ctx.daos.task.update_status(ctx.task.id, status="completed")
            await ctx.emit("task_done", {"status": "completed"})
            return

        # 函数式 interrupt()：人机门禁或澄清
        node = nxt[0] if nxt else None
        questions: list = []
        gate_payload: dict | None = None
        for ptask in state.tasks:
            for intr in getattr(ptask, "interrupts", []):
                val = intr.value
                if isinstance(val, dict):
                    node = val.get("node", node)
                    if val.get("gate_kind"):
                        gate_payload = val
                    qs = val.get("questions")
                    if isinstance(qs, list):
                        questions.extend(qs)

        if gate_payload is not None:
            kind_raw = str(gate_payload.get("kind") or "")
            stage = kind_raw or str(
                gate_payload.get("step_id") or node or "await_human"
            )
            try:
                stage = KIND_TO_STAGE.get(PlanStepKind(kind_raw), stage)
            except ValueError:
                pass
            art_id = gate_payload.get("artifact_id") or ""
            stage_version = 1
            if art_id:
                try:
                    art_row = await ctx.daos.artifact.get(art_id)
                    stage_version = int(art_row.stage_version or 1)
                except Exception:  # noqa: BLE001
                    pass
            elif stage:
                active = await ctx.daos.artifact.get_active(ctx.task.id, stage)
                if active is not None:
                    art_id = active.id
                    stage_version = int(active.stage_version or 1)
            await ctx.daos.task.update_status(
                ctx.task.id, status="waiting_confirm", current_stage=stage
            )
            payload = {
                "gate_kind": gate_payload.get("gate_kind"),
                "artifact_id": art_id,
                "step_id": gate_payload.get("step_id"),
                "stage": stage,
                "stage_version": stage_version,
            }
            await ctx.emit("human_gate_waiting", payload)
            await ctx.emit("checkpoint_waiting", payload)
            return

        await ctx.daos.task.update_status(
            ctx.task.id, status="waiting_input", current_stage=node
        )
        await ctx.emit("clarification_needed", {"questions": questions})

    async def _mark_aborted(self, task_id: str) -> None:
        await TaskDAO(self._app.db).update_status(task_id, status="aborted")
        logger.info("task aborted", extra={"task_id": task_id})

    async def _mark_failed(self, task_id: str, err: AppError) -> None:
        from ..domain import ErrorInfo

        task_dao = TaskDAO(self._app.db)
        node = err.details.get("node") if isinstance(err.details, dict) else None
        info = ErrorInfo(
            code=err.code,
            message=err.message or str(err),
            retryable=err.retryable,
            node=node,
            trace_id=trace_id_var.get(),
        )
        await task_dao.update_status(
            task_id,
            status="failed",
            # current_stage 为 NOT NULL：无节点归属时保持原值（_UNSET=不改列）
            current_stage=node if node is not None else _UNSET,
            error_info=info.model_dump(),
        )
        bus = self._app.bus
        if bus is not None:
            await bus.emit(
                task_id,
                "task_error",
                {
                    "code": err.code,
                    "message": info.message,
                    "retryable": err.retryable,
                    "node": node,
                },
            )

    # ---- 心跳循环 ----

    async def _heartbeat_loop(self, ctx: "TaskContext") -> None:
        """每 heartbeat_interval 续期；0 行更新或检测到取消标志 → 置 cancel_event
        （节点/批次边界检查后抛 TaskCancelled 自杀，dd §6.4②③）。"""
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                rows = await ctx.daos.task.heartbeat(
                    ctx.task.id, ctx.run_id
                )
                if rows == 0:
                    logger.warning(
                        "heartbeat updated 0 rows → self-kill",
                        extra={"task_id": ctx.task.id},
                    )
                    ctx.cancel_event.set()
                    return
                if await ctx.daos.task.is_cancel_requested(ctx.task.id):
                    ctx.cancel_event.set()
        except asyncio.CancelledError:
            return
