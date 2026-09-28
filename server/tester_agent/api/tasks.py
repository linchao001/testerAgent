"""任务路由（WP-26 API-B；tech-design §5.2 §5.3 / dd §10.2 §10.3 §7.6）。

端点：

- ``POST /tasks``：创建任务——先写 requirement.md 文件后落 task 行
  （dd §10.3①"先文件"），初始 status=waiting_input（语义"已建未启动"，
  前端创建后立即调 /run；Reaper 不触碰该态）；
- ``GET /tasks/{id}``：任务详情 TaskOut（各阶段 active 产物摘要 / 批次
  进度 / error_info / waiting_* 超 7 天 stale 标记，dd §6.3 末行）；
- ``POST /tasks/{id}/run``：Runner.start 接管（已在运行→409），
  返回 events_url 与 resume_from 批次游标（dd §10.3②）；
- ``POST /tasks/{id}/cancel``：waiting_* 直接置 aborted（200，无在飞
  批次）；running 置 cancelling+cancel_requested 标志走协作取消（202，
  批次边界生效）；cancelling 重复取消幂等 202（dd §6.3 取消三行）；
- ``POST /tasks/{id}/confirm``：检查点放行/修订（dd §7.6）——
  action=confirm 标记 confirmed_by=user 后 ``ainvoke(None)`` 越过 gate；
  action=modify 旧版本 superseded、新版本 v+1(user_revised) 落库、写
  message(checkpoint_revision)、``aupdate_state`` 写新 plan 后 resume；
  迟到版本（expected_version 不匹配当前 active）→ 409 VERSION_CONFLICT；
- ``POST /tasks/{id}/answer``：澄清答复——写 message(clarification_qa)
  后经 ``Command(resume=answers)`` 恢复函数式 interrupt（dd §7.6 answer 行）；
- ``POST /tasks/{id}/rollback``：回退协议入口（dd §11.2 §7.6 rollback 行）
  ——调用 graph.rollback.rollback（WP-24 协议函数，BEGIN IMMEDIATE 落账
  + 派生 thread 入口 state 注入），随后 Runner.start(event=None) 接管
  从目标阶段起跑（迁移已在协议事务内裁决，无需再校验）。
- ``GET /artifacts/{id}``：阶段产物详情（含完整 payload；WP-F2 增量，
  dd §10.2 TaskOut.active_artifacts 仅为摘要，确认页需拉正文）。

与 dd 的偏离（交接单同步登记）：
1. confirm/answer 的图恢复经 Runner.start 后台执行（dd 伪码为 API 内联
   ``ainvoke``）——复用 Runner 的锁/心跳/终态收口/事件发射，避免 API 层
   重建执行体；
2. regenerate 入口（dd §7.6 regenerate 行）在本包落为 runtime/regenerate.py
   的可调用函数，HTTP 端点 POST /cases/regenerate 属 WP-27（tech-design
   §5.4），本包不挂路由；
3. ``GET /artifacts/{id}`` 为 WP-F2 新增（dd/tech-design 端点表未列）——
   TaskOut 摘要不含 payload，确认页无法渲染清单，故补只读详情端点。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..domain import (
    AgentPlan,
    HumanDecision,
    ImpactAnalysis,
    LinkPlan,
    MessageKind,
    MessageRole,
    PointPlan,
    RequirementRef,
    ReviewProposal,
)
from ..errors import (
    ArtifactRevisionError,
    NotFoundError,
    TaskStateConflict,
    ValidationError,
    VersionConflict,
)
from ..graph.constants import (
    STAGE_INTAKE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from ..graph.control.apply_decision import apply_human_decision
from ..graph.registry import CASE_DESIGNER
from ..graph.rollback import RollbackIn as _RollbackBody
from ..graph.rollback import rollback as rollback_protocol
from ..logging_config import get_logger
from ..runtime.bus import Emitter
from ..runtime.context import DAOs, TaskContext
from ..runtime.runner import validate_transition
from ..store.models import (
    ArtifactDAO,
    ArtifactRow,
    ConversationDAO,
    MessageDAO,
    MessageRow,
    SubtaskDAO,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
)
from ..store.workspace_files import FileStore
from .common import idem_check, idem_record

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["tasks"])

STALE_AFTER_DAYS = 7  # dd §6.3：waiting_* 超 7 天响应带 stale 标记

#: confirm(modify) 修订 payload 写回图 state 的键（dd §7.6 modify 行）
_STAGE_STATE_KEY = {
    STAGE_LINK_IDENTIFY: "link_plan",
    STAGE_POINT_WRITE: "point_plan",
}


# ---- 请求/响应模型（dd §10.2 task 段） ----


class CreateTaskIn(BaseModel):
    conversation_id: str = Field(min_length=1)
    requirement_md: str = Field(min_length=1)
    snapshot_level: Literal["off", "meta", "full"] | None = None


class ArtifactOut(BaseModel):
    """active 产物摘要（TaskOut.active_artifacts 值，dd §10.2"stage -> active 摘要"）。"""

    id: str
    stage: str
    stage_version: int
    origin: str
    status: str
    confirmed_by: str | None
    created_at: str


class ArtifactDetailOut(BaseModel):
    """阶段产物详情（WP-F2：含完整 payload，供确认页编辑）。"""

    id: str
    task_id: str
    stage: str
    stage_version: int
    origin: str
    status: str
    confirmed_by: str | None
    created_at: str
    payload: dict


class TaskOut(BaseModel):
    id: str
    workspace_id: str
    conversation_id: str
    status: str
    current_stage: str
    active_artifacts: dict[str, ArtifactOut]
    progress: dict[str, list[dict]]
    error_info: dict | None
    stale: bool = False
    created_at: str
    updated_at: str


class RunOut(BaseModel):
    task_id: str
    graph_run_id: str
    events_url: str
    resume_from: dict


class CancelOut(BaseModel):
    task_id: str
    status: str


class ConfirmIn(BaseModel):
    gate_kind: Literal["plan_confirm", "review_decision"]
    artifact_id: str = Field(min_length=1)
    action: Literal["confirm", "modify", "reject_rerun"] = "confirm"
    expected_version: int | None = None
    stage: str | None = None
    payload: dict | None = None


class ConfirmOut(BaseModel):
    task_id: str
    status: str
    artifact_id: str
    stage_version: int


class PlanOut(BaseModel):
    plan_id: str
    version: int
    goal: str
    steps: list[dict]
    status: str
    replan_count: int = 0


class SubtaskOut(BaseModel):
    id: str
    kind: str
    thread_id: str
    status: str
    result_artifact_id: str | None = None
    created_at: str
    updated_at: str


class AnswerIn(BaseModel):
    answers: list[dict]  # [{question_id, answer}]（dd §10.2）


class AnswerOut(BaseModel):
    task_id: str
    status: str


class RollbackBody(BaseModel):
    target_stage: Literal["link_identify", "point_write"]
    artifact_id: str = Field(min_length=1)
    expected_version: int
    revised_artifact: dict | None = None


class RollbackOut(BaseModel):
    graph_run_id: str
    impact: dict  # ImpactAnalysis（dd §10.2 RollbackOut.impact）


# ---- 响应组装 ----


def _artifact_out(a: ArtifactRow) -> ArtifactOut:
    return ArtifactOut(
        id=a.id,
        stage=a.stage,
        stage_version=a.stage_version,
        origin=a.origin,
        status=a.status,
        confirmed_by=a.confirmed_by,
        created_at=a.created_at,
    )


def _task_out(task: TaskRow, artifacts: list[ArtifactRow]) -> TaskOut:
    return TaskOut(
        id=task.id,
        workspace_id=task.workspace_id,
        conversation_id=task.conversation_id,
        status=task.status,
        current_stage=task.current_stage,
        active_artifacts={a.stage: _artifact_out(a) for a in artifacts},
        progress={a.stage: a.progress_list() for a in artifacts},
        error_info=task.error_obj().model_dump() if task.error_obj() else None,
        stale=_is_stale(task),
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _is_stale(task: TaskRow) -> bool:
    """waiting_* 超 7 天（dd §6.3 末行：原状态不变，响应带 stale 标记）。"""
    if task.status not in ("waiting_confirm", "waiting_input"):
        return False
    try:
        ts = datetime.fromisoformat(task.updated_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    age = datetime.now(timezone.utc) - ts
    return age.total_seconds() > STALE_AFTER_DAYS * 86400


def _db(request: Request):
    return request.app.state.db


def _runner(request: Request):
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        # lifespan 容忍 LLM 未配置不构造 Runner（main.py §6.5 第 2 步）；
        # 此时任务操作一律不可用，按状态冲突口径拒绝而非 500。
        raise TaskStateConflict(
            "任务执行组件未就绪（模型未配置），请先在设置中配置模型并重启",
            details={"reason": "runner_not_ready"},
        )
    return runner


def _resume_from(task: TaskRow, artifacts: list[ArtifactRow]) -> dict:
    """dd §10.3② ``resume_from:{node, batch_id, done, total}``：当前阶段
    active 产物 progress 中首个未完成批；无产物/批次时零游标。"""
    stage = task.current_stage
    for a in artifacts:
        if a.stage != stage:
            continue
        records = a.progress_list()
        if not records:
            break
        done = sum(1 for r in records if r.get("status") == "done")
        pending = next((r for r in records if r.get("status") != "done"), None)
        return {
            "node": stage,
            "batch_id": pending.get("batch_id") if pending else None,
            "done": done,
            "total": len(records),
        }
    return {"node": stage, "batch_id": None, "done": 0, "total": 0}


# ---- 端点 ----


@router.post("/tasks", status_code=201)
async def create_task(body: CreateTaskIn, request: Request) -> JSONResponse:
    db = _db(request)
    conv = await ConversationDAO(db).get(body.conversation_id)  # 404 不暴露存在性
    task_id = uuid.uuid4().hex

    # dd §10.3①：先写 requirement.md 文件，后落 task 行（先文件后 DB）
    store: FileStore = request.app.state.file_store
    ref = await store.save_requirement(
        conv.workspace_id, task_id, body.requirement_md
    )

    task = TaskRow.create(
        id=task_id,
        conversation_id=conv.id,
        workspace_id=conv.workspace_id,
        status="waiting_input",
        current_stage=STAGE_INTAKE,
        langgraph_thread_id=f"th-{task_id}",
        graph_run_id=uuid.uuid4().hex,
        requirement=RequirementRef(
            path=ref.path, content_hash=ref.content_hash, clause_count=0
        ),
        snapshot_level=body.snapshot_level or "meta",
    )
    await TaskDAO(db).create(task)
    logger.info("task created", extra={"task_id": task_id})
    return JSONResponse(
        status_code=201,
        headers={"Location": f"/api/v1/tasks/{task_id}"},
        content=_task_out(task, []).model_dump(),
    )


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request) -> TaskOut:
    task = await TaskDAO(_db(request)).get(task_id)  # 404 不暴露存在性
    artifacts = await ArtifactDAO(_db(request)).list_active_chain(task_id)
    return _task_out(task, artifacts)


@router.post("/tasks/{task_id}/run")
async def run_task(
    task_id: str, request: Request,
    idempotency_key: str | None = Header(None),
) -> RunOut:
    # WP-29 幂等键去重（tech-design §5.0：tasks/run 必带 Idempotency-Key）
    cached = idem_check(request, "tasks.run", idempotency_key, "")
    if cached is not None:
        return RunOut(**cached.body)
    runner = _runner(request)
    handle = await runner.start(task_id, event="run")  # 404/409 在此裁决
    task = await TaskDAO(_db(request)).get(task_id)
    artifacts = await ArtifactDAO(_db(request)).list_active_chain(task_id)
    out = RunOut(
        task_id=handle.task_id,
        graph_run_id=handle.graph_run_id,
        events_url=handle.events_url,
        resume_from=_resume_from(task, artifacts),
    )
    idem_record(request, "tasks.run", idempotency_key, "", 200, out.model_dump())
    return out


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request) -> JSONResponse:
    db = _db(request)
    task_dao = TaskDAO(db)
    task = await task_dao.get(task_id)  # 404

    if task.status in ("waiting_confirm", "waiting_input"):
        # dd §6.3：waiting_* cancel 直接生效（无在飞批次）
        validate_transition(task.status, "cancel")
        await task_dao.update_status(task_id, status="aborted")
        return JSONResponse(
            status_code=200,
            content=CancelOut(task_id=task_id, status="aborted").model_dump(),
        )
    if task.status == "cancelling":
        # 重复取消幂等：标志已在，批次边界自行收口
        return JSONResponse(
            status_code=202,
            content=CancelOut(task_id=task_id, status="cancelling").model_dump(),
        )
    if task.status == "running":
        # runner 已接管：仅置标志，批次边界转 aborted（协作取消，dd §6.4③）
        validate_transition(task.status, "cancel")
        await task_dao.request_cancel(task_id)
        await task_dao.update_status(task_id, status="cancelling")
        return JSONResponse(
            status_code=202,
            content=CancelOut(task_id=task_id, status="cancelling").model_dump(),
        )
    # 终态（completed/failed/aborted）：非法迁移 → 409
    validate_transition(task.status, "cancel")
    raise TaskStateConflict(  # pragma: no cover —— validate 未覆盖态的保险
        f"当前状态不允许取消：{task.status}", details={"current": task.status}
    )


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, request: Request) -> ArtifactDetailOut:
    """阶段产物详情（含 payload）。缺失 → 404 NOT_FOUND。"""
    row = await ArtifactDAO(_db(request)).get(artifact_id)
    return ArtifactDetailOut(
        id=row.id,
        task_id=row.task_id,
        stage=row.stage,
        stage_version=row.stage_version,
        origin=row.origin,
        status=row.status,
        confirmed_by=row.confirmed_by,
        created_at=row.created_at,
        payload=row.payload_dict(),
    )


@router.get("/tasks/{task_id}/plan")
async def get_task_plan(task_id: str, request: Request) -> PlanOut:
    db = _db(request)
    task = await TaskDAO(db).get(task_id)
    if not task.current_plan_artifact_id:
        raise NotFoundError(f"任务尚无 AgentPlan：{task_id}")
    art = await ArtifactDAO(db).get(task.current_plan_artifact_id)
    plan = AgentPlan.model_validate(art.payload_dict())
    return PlanOut(
        plan_id=plan.plan_id,
        version=plan.version,
        goal=plan.goal,
        steps=[s.model_dump() for s in plan.steps],
        status=plan.status,
        replan_count=plan.replan_count,
    )


@router.get("/tasks/{task_id}/subtasks")
async def list_task_subtasks(task_id: str, request: Request) -> list[SubtaskOut]:
    db = _db(request)
    await TaskDAO(db).get(task_id)
    rows = await SubtaskDAO(db).list_by_task(task_id)
    return [
        SubtaskOut(
            id=r.id,
            kind=r.kind,
            thread_id=r.thread_id,
            status=r.status,
            result_artifact_id=r.result_artifact_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.get("/tasks/{task_id}/review-proposals/{artifact_id}")
async def get_review_proposal(
    task_id: str, artifact_id: str, request: Request
) -> dict:
    db = _db(request)
    await TaskDAO(db).get(task_id)
    art = await ArtifactDAO(db).get(artifact_id)
    if art.task_id != task_id:
        raise NotFoundError(f"评审提案不存在：{artifact_id}")
    kind = art.kind or art.stage
    if kind not in ("review_proposal", "review_coverage", "review_quality", "review_adoption"):
        # Still allow if payload looks like ReviewProposal
        payload = art.payload_dict()
        if "scope" not in payload or "items" not in payload:
            raise NotFoundError(f"评审提案不存在：{artifact_id}")
    return ReviewProposal.model_validate(art.payload_dict()).model_dump()


@router.post("/tasks/{task_id}/confirm")
async def confirm_task(task_id: str, body: ConfirmIn, request: Request) -> ConfirmOut:
    db = _db(request)
    runner = _runner(request)
    task_dao = TaskDAO(db)
    task = await task_dao.get(task_id)  # 404

    if task.status != "waiting_confirm":
        validate_transition(task.status, "confirm")

    artifact_dao = ArtifactDAO(db)
    artifact = await artifact_dao.get(body.artifact_id)
    if artifact.task_id != task_id:
        raise NotFoundError(f"阶段产物不存在：{body.artifact_id}")

    legacy_stages = {STAGE_LINK_IDENTIFY, STAGE_POINT_WRITE}
    use_legacy = body.stage in legacy_stages and body.expected_version is not None

    if use_legacy:
        if artifact.stage != body.stage:
            raise ValidationError(
                "产物阶段与确认阶段不一致",
                details={"artifact_stage": artifact.stage, "stage": body.stage},
            )
        if artifact.stage_version != body.expected_version:
            raise VersionConflict(
                f"产物版本冲突：期望 v{body.expected_version}，"
                f"实际 v{artifact.stage_version}",
                details={
                    "expected": body.expected_version,
                    "actual": artifact.stage_version,
                },
            )
        active = await artifact_dao.get_active(task_id, body.stage)
        if active is None or active.id != artifact.id:
            raise ValidationError(
                "产物不是该阶段当前 active 版本",
                details={"artifact_id": body.artifact_id, "stage": body.stage},
            )

        if body.action == "confirm":
            await artifact_dao.mark_confirmed(artifact.id, by="user")
            await runner.start(task_id, event="confirm")
            return ConfirmOut(
                task_id=task_id,
                status="running",
                artifact_id=artifact.id,
                stage_version=artifact.stage_version,
            )

        if body.payload is None:
            raise ValidationError("modify 动作必须携带修订 payload")
        _validate_revision_payload(body.stage, body.payload)

        async with db.immediate_tx():
            await artifact_dao.supersede(artifact.id)
            v = await artifact_dao.next_version(task_id, body.stage)
            new_id = uuid.uuid4().hex
            await artifact_dao.put(
                ArtifactRow.create(
                    id=new_id,
                    task_id=task_id,
                    stage=body.stage,
                    graph_run_id=artifact.graph_run_id,
                    stage_version=v,
                    origin="user_revised",
                    status="active",
                    payload=body.payload,
                    confirmed_by="user",
                )
            )
            await MessageDAO(db).put(
                MessageRow.create(
                    id=uuid.uuid4().hex,
                    conversation_id=task.conversation_id,
                    role=MessageRole.USER,
                    kind=MessageKind.CHECKPOINT_REVISION,
                    content=f"修订 {body.stage} 产物 v{artifact.stage_version} → v{v}",
                    task_id=task_id,
                    ref_artifact_id=new_id,
                    payload={
                        "stage": body.stage,
                        "old_version": artifact.stage_version,
                        "new_version": v,
                    },
                )
            )

        graph = request.app.state.graphs.get(CASE_DESIGNER)
        await graph.aupdate_state(
            {"configurable": {"thread_id": task.langgraph_thread_id}},
            {_STAGE_STATE_KEY[body.stage]: body.payload},
        )
        await runner.start(task_id, event="confirm")
        return ConfirmOut(
            task_id=task_id,
            status="running",
            artifact_id=new_id,
            stage_version=v,
        )

    # ---- Plan-Execute path (gate_kind) ----
    decision = HumanDecision(
        gate_kind=body.gate_kind,
        action=body.action,  # type: ignore[arg-type]
        artifact_id=body.artifact_id,
        payload=body.payload,
    )
    art_payload = artifact.payload_dict()
    artifacts_map = {
        artifact.id: {
            "kind": artifact.kind or artifact.stage,
            "version": artifact.stage_version,
            "confirmed_by": artifact.confirmed_by,
            "payload": art_payload,
        }
    }
    plan: AgentPlan | None = None
    if task.current_plan_artifact_id:
        plan_art = await artifact_dao.get(task.current_plan_artifact_id)
        plan = AgentPlan.model_validate(plan_art.payload_dict())
        artifacts_map[plan_art.id] = {
            "kind": "agent_plan",
            "version": plan_art.stage_version,
            "confirmed_by": plan_art.confirmed_by,
            "payload": plan_art.payload_dict(),
        }

    case_updates: list[tuple[str, str]] = []
    if plan is not None:
        apply_human_decision(
            decision,
            plan=plan,
            artifacts=artifacts_map,
            case_review_updates=case_updates,
        )
        # persist plan + confirm marker
        async with db.immediate_tx():
            await artifact_dao.mark_confirmed(artifact.id, by="user")
            if task.current_plan_artifact_id:
                await artifact_dao.supersede(task.current_plan_artifact_id)
                v = await artifact_dao.next_version(task_id, "agent_plan")
                new_plan_id = uuid.uuid4().hex
                await artifact_dao.put(
                    ArtifactRow.create(
                        id=new_plan_id,
                        task_id=task_id,
                        stage="agent_plan",
                        kind="agent_plan",
                        graph_run_id=task.graph_run_id,
                        stage_version=v,
                        origin="user_revised",
                        status="active",
                        payload=plan.model_dump(),
                        confirmed_by="user",
                    )
                )
                await task_dao.set_current_plan_artifact(task_id, new_plan_id)
            for case_id, status in case_updates:
                await TestcaseDAO(db).update_review(case_id, status)
    else:
        await artifact_dao.mark_confirmed(artifact.id, by="user")

    graph = request.app.state.graphs.get(CASE_DESIGNER)
    resume_payload = {
        "action": body.action,
        "payload": body.payload,
        "gate_kind": body.gate_kind,
    }
    try:
        await graph.aupdate_state(
            {"configurable": {"thread_id": task.langgraph_thread_id}},
            {
                "artifacts": {
                    k: {**v, "confirmed_by": "user"}
                    for k, v in artifacts_map.items()
                },
                **({"agent_plan": plan.model_dump()} if plan is not None else {}),
            },
        )
    except Exception:  # noqa: BLE001 — control graph may not have checkpoint yet
        logger.warning(
            "confirm aupdate_state skipped",
            extra={"task_id": task_id},
            exc_info=True,
        )

    await runner.start(task_id, event="confirm", resume=resume_payload)
    return ConfirmOut(
        task_id=task_id,
        status="running",
        artifact_id=artifact.id,
        stage_version=artifact.stage_version,
    )


def _validate_revision_payload(stage: str, payload: dict) -> None:
    """修订契约校验（dd §2.3/§2.4）：payload 须可解析为该阶段计划模型。"""
    model = LinkPlan if stage == STAGE_LINK_IDENTIFY else PointPlan
    try:
        model.model_validate(payload)
    except Exception as e:
        raise ArtifactRevisionError(
            f"修订 payload 不符合 {model.__name__} 契约",
            details={"stage": stage, "error": str(e)},
        ) from e


@router.post("/tasks/{task_id}/answer")
async def answer_task(task_id: str, body: AnswerIn, request: Request) -> AnswerOut:
    db = _db(request)
    runner = _runner(request)
    task = await TaskDAO(db).get(task_id)  # 404

    if task.status != "waiting_input":
        validate_transition(task.status, "answer")  # 非 waiting_input → 409

    # resume 值契约：intake 节点按 [{"id","answer"}, ...] 消费（graph/nodes/intake.py）
    resume_value: list[dict] = []
    for i, item in enumerate(body.answers):
        if not isinstance(item, dict) or not str(item.get("question_id") or "").strip():
            raise ValidationError(
                f"answers[{i}] 缺少 question_id",
                details={"index": i},
            )
        if "answer" not in item:
            raise ValidationError(
                f"answers[{i}] 缺少 answer",
                details={"index": i, "question_id": item.get("question_id")},
            )
        resume_value.append(
            {"id": item["question_id"], "answer": item["answer"]}
        )

    # 写 message(clarification_qa)（dd §7.6 answer 行）
    await MessageDAO(db).put(
        MessageRow.create(
            id=uuid.uuid4().hex,
            conversation_id=task.conversation_id,
            role=MessageRole.USER,
            kind=MessageKind.CLARIFICATION_QA,
            content="\n".join(
                f"{a['id']}: {a['answer']}" for a in resume_value
            ),
            task_id=task_id,
            payload={"answers": resume_value},
        )
    )
    await runner.start(task_id, event="answer", resume=resume_value)
    return AnswerOut(task_id=task_id, status="running")


@router.post("/tasks/{task_id}/rollback")
async def rollback_task(
    task_id: str, body: RollbackBody, request: Request
) -> RollbackOut:
    db = _db(request)
    runner = _runner(request)
    task = await TaskDAO(db).get(task_id)  # 404

    # 预检迁移（waiting_confirm/waiting_input/completed 才可回退）；协议事务内再裁决
    validate_transition(task.status, "rollback")

    artifact = await ArtifactDAO(db).get(body.artifact_id)
    if artifact.task_id != task_id:
        raise NotFoundError(f"阶段产物不存在：{body.artifact_id}")
    if artifact.stage != body.target_stage:
        raise ValidationError(
            "产物阶段与回退目标阶段不一致",
            details={"artifact_stage": artifact.stage, "target_stage": body.target_stage},
        )

    ctx = _rollback_ctx(request, task)
    impact: ImpactAnalysis = await rollback_protocol(
        ctx,
        task_id,
        _RollbackBody(
            artifact_id=body.artifact_id,
            expected_version=body.expected_version,
            revised_artifact=body.revised_artifact,
        ),
    )

    # 协议事务已切 running/新 run/新 thread 并注入入口 state（dd §11.2）；
    # Runner 接管执行（event=None 跳过迁移校验——已在协议内裁决）。
    await runner.start(task_id, event=None)

    fresh = await TaskDAO(db).get(task_id)
    return RollbackOut(
        graph_run_id=fresh.graph_run_id, impact=impact.model_dump()
    )


def _rollback_ctx(request: Request, task: TaskRow) -> TaskContext:
    """回退协议所需的最小 TaskContext。

    rollback() 只消费 db/daos/graphs/emit，不触检索面（reader/mirror/files），
    故 reader 置 None——避免为回退建立 ReMe 连接（WP-09 前工厂无 builder）。
    与 Runner.build_task_context 的偏离：不做 reader/mirror 构造（交接单登记）。
    """
    app_ctx = getattr(request.app.state, "app_ctx", None)
    if app_ctx is None:
        raise TaskStateConflict(
            "任务执行组件未就绪（模型未配置），回退不可用",
            details={"reason": "app_ctx_not_ready"},
        )
    db = app_ctx.db
    bus = app_ctx.bus
    return TaskContext(
        app=app_ctx,
        task=task,
        run_id=task.graph_run_id,
        files=app_ctx.file_store,
        reader=None,  # type: ignore[arg-type] —— 回退协议不触检索面（见 docstring）
        snapshot_level=task.snapshot_level,
        mirror=None,  # type: ignore[arg-type]
        agent_config={},
        daos=DAOs(
            task=TaskDAO(db),
            message=MessageDAO(db),
            artifact=ArtifactDAO(db),
            testcase=TestcaseDAO(db),  # rollback 协议置 obsolete 受影响用例
        ),
        emit=Emitter(bus, task.id) if bus is not None else None,
        cancel_event=None,
    )
