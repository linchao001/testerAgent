"""重生成入口（WP-26；dd §7.6 regenerate 行 / tech-design §4.2④ R8）。

``regenerate_cases``：评审期局部重生成的运行时入口——

1. 状态迁移裁决（completed --regenerate--> running，dd §6.3）；
2. 获取任务锁（Registry.acquire，409 防并发）并置 running；
3. 直接调 case_generate 的单批 worker :func:`generate_case_batch`
   （同一套检索/LLM/先文件后 DB 提交/trace 路径，不 invoke 图），
   ``batch_id=regen-{ts}``、``stage_version=当前 v``（增补批次，不产生
   游离的第二条产物链路）；
4. 新行 lineage 挂旧 case（``regenerated_from_case_id``）；
   ``keep_original=False`` 时旧用例置 obsolete（旧行保留或废弃由用户选择）；
5. 回 completed 并发 task_done；置 running 之后失败按 Runner 同口径收口
   failed（前置校验失败——缺产物/跨任务用例等——任务保持原状态直接抛出）。

HTTP 端点 ``POST /cases/regenerate``（tech-design §5.4）属 WP-27 API-C，
本包只交付入口函数。指令以 message(kind=regen_instruction) 落库留痕。

与 dd 的偏离（交接单登记）：新行与旧 case 的对应按 point 分组、组内按
(创建时间, id) 顺序 zip 匹配——dd §7.6 只要求"新行 lineage 挂旧 case"，
未指定 1:1 映射粒度；LLM 产出数量与旧例不一致时，多余新行保持
``root_case_id=自身``（无来源挂点）、多余旧例不受影响。
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

from ..domain import Lineage, LinkPlan, MessageKind, MessageRole, PointPlan
from ..errors import AppError, NotFoundError, TaskStateConflict, ValidationError
from ..graph.constants import (
    STAGE_CASE_GENERATE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from ..graph.nodes.case_generate import generate_case_batch
from ..logging_config import get_logger
from ..runtime.runner import build_task_context, validate_transition
from ..store.db import utcnow_iso
from ..store.models import (
    ArtifactDAO,
    CaseRow,
    MessageDAO,
    MessageRow,
    TaskDAO,
    TestcaseDAO,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime.context import AppContext

logger = get_logger(__name__)


async def regenerate_cases(
    app: "AppContext",
    task_id: str,
    *,
    case_ids: list[str],
    instruction: str,
    keep_original: bool = True,
) -> list[CaseRow]:
    """局部重生成入口（dd §7.6 regenerate 行）。

    :param case_ids: 待重生成的旧用例 id（须为本任务 active 用例）
    :param instruction: 用户重生成指令（落 regen_instruction 消息留痕）
    :param keep_original: True=旧用例行保留；False=旧用例置 obsolete
    :returns: 本次新生成的用例行（lineage 已挂旧 case）
    """
    if not case_ids:
        raise ValidationError("case_ids 不能为空", details={"field": "case_ids"})
    if not instruction.strip():
        raise ValidationError(
            "instruction 不能为空", details={"field": "instruction"}
        )
    if app.registry is None:
        raise TaskStateConflict(
            "任务执行组件未就绪", details={"reason": "registry_not_ready"}
        )

    task_dao = TaskDAO(app.db)
    task = await task_dao.get(task_id)  # 404

    # ① 状态迁移裁决（先于拿锁，避免非法迁移误触失败收口）
    validate_transition(task.status, "regenerate")

    token = await app.registry.acquire(task_id)  # 409 防并发（busy）
    started = False  # 置 running 之后失败才收口 failed；前置校验失败保持原状态
    try:
        # ① 前置校验（不改动任务状态）：上下文/产物/旧用例可重生成性
        ctx = await build_task_context(app, task_id)
        artifact_dao = ctx.daos.artifact
        assert artifact_dao is not None  # build_task_context 全量注入
        testcase_dao = TestcaseDAO(app.db)

        case_artifact = await artifact_dao.get_active(task_id, STAGE_CASE_GENERATE)
        if case_artifact is None:
            raise AppError(
                "缺少 case_generate active 产物，无法重生成",
                details={"node": STAGE_CASE_GENERATE},
            )
        version = case_artifact.stage_version

        # ② 旧用例 → 测试点（须为本任务用例；跨任务 id 视为不存在）
        point_plan = await _load_point_plan(artifact_dao, task_id)
        points_by_id = {p.point_id: p for p in point_plan.points}
        old_rows: list[CaseRow] = []
        for cid in dict.fromkeys(case_ids):  # 去重保序
            row = await testcase_dao.get(cid)
            if row.task_id != task_id:
                raise NotFoundError(f"测试用例不存在：{cid}")
            old_rows.append(row)
        points = []
        seen: set[str] = set()
        for row in old_rows:
            p = points_by_id.get(row.point_id)
            if p is None:
                raise AppError(
                    f"用例 {row.id} 的测试点 {row.point_id} 不在当前 PointPlan 中",
                    details={"case_id": row.id, "point_id": row.point_id},
                )
            if row.point_id not in seen:
                seen.add(row.point_id)
                points.append(p)

        # ③ 置 running（此后失败按 Runner 同口径收口 failed）
        await task_dao.update_status(task_id, status="running")
        started = True

        # ④ 指令留痕（regen_instruction）+ 检索 scope 映射
        await MessageDAO(app.db).put(
            MessageRow.create(
                id=uuid.uuid4().hex,
                conversation_id=task.conversation_id,
                role=MessageRole.USER,
                kind=MessageKind.REGEN_INSTRUCTION,
                content=instruction,
                task_id=task_id,
                payload={
                    "case_ids": list(dict.fromkeys(case_ids)),
                    "keep_original": keep_original,
                },
            )
        )
        story_to_link = await _load_story_to_link(artifact_dao, task_id)
        batch_id = f"regen-{utcnow_iso()}"

        # ⑤ 直接调 case_generate worker（不 invoke 图，dd §7.6）
        rows = await generate_case_batch(
            ctx, points, version=version, batch_id=batch_id,
            story_to_link=story_to_link,
        )

        # ⑥ 新行 lineage 挂旧 case；keep_original=False 旧行废弃
        await _attach_lineage(testcase_dao, old_rows, rows)
        if not keep_original:
            await testcase_dao.mark_obsolete_by_ids(
                task_id, [r.id for r in old_rows]
            )

        # ⑦ 回 completed 并发 task_done（dd §7.6：不经过 Runner 终态收口）
        await task_dao.update_status(task_id, status="completed")
        if ctx.emit is not None:
            await ctx.emit("task_done", {"status": "completed"})
        logger.info(
            "regenerate done",
            extra={"task_id": task_id, "batch_id": batch_id,
                   "old": len(old_rows), "new": len(rows)},
        )
        return rows
    except Exception as e:
        # 失败收口与 Runner 同口径：running 之后失败才置 failed
        # （前置校验失败——缺产物/跨任务用例等——任务保持原状态直接抛出）
        if started:
            await _mark_failed(app, task_id, e)
        raise
    finally:
        await app.registry.release(task_id, token)


async def _load_point_plan(artifact_dao: ArtifactDAO, task_id: str) -> PointPlan:
    art = await artifact_dao.get_active(task_id, STAGE_POINT_WRITE)
    if art is None:
        raise AppError(
            "缺少 point_write active 产物，无法定位重生成测试点",
            details={"node": STAGE_POINT_WRITE},
        )
    try:
        return PointPlan.model_validate(art.payload_dict())
    except Exception as e:
        raise AppError(
            "point_plan 解析失败", details={"node": STAGE_POINT_WRITE}
        ) from e


async def _load_story_to_link(
    artifact_dao: ArtifactDAO, task_id: str
) -> dict[str, str]:
    art = await artifact_dao.get_active(task_id, STAGE_LINK_IDENTIFY)
    if art is None:
        return {}
    try:
        plan = LinkPlan.model_validate(art.payload_dict())
    except Exception:
        return {}
    return {s.story_id: s.link_id for s in plan.stories}


async def _attach_lineage(
    testcase_dao: TestcaseDAO,
    old_rows: list[CaseRow],
    new_rows: list[CaseRow],
) -> None:
    """按 point 分组、组内 (created_at, id) 排序 zip，把新行挂到旧 case。

    DB 行经 set_lineage 更新；返回给调用方的内存 CaseRow 同步改写 lineage，
    保证"返回的用例行 lineage 已挂旧 case"（docstring 契约）。
    """
    old_by_point: dict[str, list[CaseRow]] = {}
    for r in old_rows:
        old_by_point.setdefault(r.point_id, []).append(r)
    new_by_point: dict[str, list[CaseRow]] = {}
    for r in new_rows:
        new_by_point.setdefault(r.point_id, []).append(r)

    for point_id, olds in old_by_point.items():
        olds_sorted = sorted(olds, key=lambda r: (r.created_at, r.id))
        for old, new in zip(olds_sorted, new_by_point.get(point_id, [])):
            root = old.lineage_obj().root_case_id or old.id
            lineage = Lineage(
                root_case_id=root,
                regenerated_from_case_id=old.id,
            )
            await testcase_dao.set_lineage(new.id, lineage.model_dump())
            new.lineage = json.dumps(lineage.model_dump(), ensure_ascii=False)


async def _mark_failed(app: "AppContext", task_id: str, e: Exception) -> None:
    from ..domain import ErrorInfo
    from ..errors import AppError as _AE

    code = e.code if isinstance(e, _AE) else "INTERNAL"
    retryable = bool(getattr(e, "retryable", False))
    try:
        await TaskDAO(app.db).update_status(
            task_id,
            status="failed",
            error_info=ErrorInfo(
                code=code, message=str(e), retryable=retryable, node="regenerate",
            ).model_dump(),
        )
    except Exception:  # noqa: BLE001 —— 收口失败不再外抛（原异常优先）
        logger.warning("regenerate failed-path also failed",
                       extra={"task_id": task_id}, exc_info=True)
