"""用例路由（WP-27 API-C；tech-design §5.4 / dd §10.2 §10.3④⑤ §9.4 §7.6）。

端点：

- ``GET /tasks/{id}/cases``：用例列表（分页；默认仅 active；``?status=&review=
  &version=&limit=&cursor=``，游标为 testcase.created_at 倒序，dd §3.3⑦）；
- ``GET /cases/{id}``：用例详情（MD 正文 + trace_refs + lineage + content_hash，
  dd §10.2 CaseDetailOut）；
- ``PUT /cases/{id}``：编辑 MD 正文（``If-Match: {content_hash}`` 乐观锁——
  不匹配 409 VERSION_CONFLICT 且 details 带当前 hash；服务端重算 front-matter
  元数据（用户误改以服务端为准，仅正文 hash 参与 If-Match）→ 原子覆盖 →
  review_status→edited_adopted + review_record(edit, detail=diff 统计)）；
- ``POST /cases/review``：批量评审——逐条按 tech-design §3.2④ 转换表校验
  （pending→adopted/edited_adopted/rejected；三者互转允许；同状态重复
  评审与非 active 用例评审均属非法），任一非法整体 422
  VALIDATION_REVIEW_TRANSITION 不落账（全部通过才在单事务内逐条
  update_review + review_record）；
- ``POST /cases/regenerate``：局部重生成（走 runtime/regenerate.py 入口，
  WP-26 已交付；Idempotency-Key 头按 tech-design §5.0 接受，去重存储属
  WP-29，本包登记为遗留）；
- ``POST /tasks/{id}/export``：导出（默认仅 active 且 adopted/edited_adopted；
  导出前逐文件 hash 校验，不一致列 skipped；≤200 条且 ≤20MB 同步返回 zip
  下载地址，否则异步任务 + GET 句柄轮询）；
- ``GET /tasks/{id}/export/{job_id}``：导出结果轮询/下载（ready 时
  ``?download=1`` 直接返回 zip 文件流）。

跨工作区/跨任务不暴露存在性（404 同形态，dd §10.1）。
"""

from __future__ import annotations

import difflib
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..domain import CaseFileContent, ReviewStatus
from ..errors import (
    FileConflict,
    ReviewTransitionError,
    TaskStateConflict,
    ValidationError,
    VersionConflict,
)
from ..logging_config import get_logger
from ..runtime.export import ExportService
from ..store.models import (
    CaseRow,
    ReviewDAO,
    ReviewRow,
    TaskDAO,
    TestcaseDAO,
)
from ..store.workspace_files import parse_case_markdown
from .common import DEFAULT_LIMIT, checked_cursor, idem_check, idem_record, page_response

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["cases"])

# ---- 评审状态机（tech-design §3.2④ / dd §10.3⑤） ----
# pending → adopted / edited_adopted / rejected；三者互转（用户改判）；
# 每次转换写 review_record。edited_adopted 只能经编辑（PUT）进入——
# 评审动作 "edited_adopted" 视为合法转换（用户先编辑后评审的合并动作）。
_ALLOWED_REVIEW_ACTIONS: dict[str, set[str]] = {
    ReviewStatus.PENDING.value: {
        ReviewStatus.ADOPTED.value,
        ReviewStatus.EDITED_ADOPTED.value,
        ReviewStatus.REJECTED.value,
    },
    ReviewStatus.ADOPTED.value: {
        ReviewStatus.EDITED_ADOPTED.value,
        ReviewStatus.REJECTED.value,
    },
    ReviewStatus.EDITED_ADOPTED.value: {
        ReviewStatus.ADOPTED.value,
        ReviewStatus.REJECTED.value,
    },
    ReviewStatus.REJECTED.value: {
        ReviewStatus.ADOPTED.value,
        ReviewStatus.EDITED_ADOPTED.value,
    },
}


# ---- 请求/响应模型（dd §10.2 case 段） ----


class CaseSummaryOut(BaseModel):
    id: str
    point_id: str
    stage_version: int
    title: str
    status: str
    review_status: str
    batch_id: str
    error_info: dict | None
    created_at: str
    updated_at: str


class CaseDetailOut(BaseModel):
    id: str
    point_id: str
    stage_version: int
    lineage: dict
    review_status: str
    markdown: str
    content_hash: str
    trace_refs: dict
    error_info: dict | None


class CaseUpdateIn(BaseModel):
    markdown: str = Field(min_length=1)


class ReviewItem(BaseModel):
    case_id: str = Field(min_length=1)
    action: Literal["adopt", "reject", "edited_adopted"]


class ReviewIn(BaseModel):
    items: list[ReviewItem] = Field(min_length=1)


class ReviewResultItem(BaseModel):
    case_id: str
    ok: bool
    review_status: str | None = None
    error: str | None = None


class ReviewOut(BaseModel):
    results: list[ReviewResultItem]


class RegenerateIn(BaseModel):
    case_ids: list[str] = Field(min_length=1)
    instruction: str = Field(min_length=1)
    keep_original: bool = True


class RegenerateOut(BaseModel):
    task_id: str
    status: str
    new_case_ids: list[str]


class ExportIn(BaseModel):
    case_ids: list[str] | None = None


class ExportOutModel(BaseModel):
    status: str
    download_url: str | None
    skipped: list[dict]
    job_id: str | None


# ---- 响应组装 ----


def _error_info(row: CaseRow) -> dict | None:
    if not row.error_info:
        return None
    import json as _json

    try:
        return _json.loads(row.error_info)
    except Exception:  # noqa: BLE001
        return {"raw": row.error_info}


def _summary_out(row: CaseRow) -> CaseSummaryOut:
    return CaseSummaryOut(
        id=row.id,
        point_id=row.point_id,
        stage_version=row.stage_version,
        title=row.title,
        status=row.status,
        review_status=row.review_status,
        batch_id=row.batch_id,
        error_info=_error_info(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _detail_out(row: CaseRow, markdown: str) -> CaseDetailOut:
    return CaseDetailOut(
        id=row.id,
        point_id=row.point_id,
        stage_version=row.stage_version,
        lineage=row.lineage_obj().model_dump(),
        review_status=row.review_status,
        markdown=markdown,
        content_hash=row.content_hash,
        trace_refs=row.trace_refs_obj().model_dump(),
        error_info=_error_info(row),
    )


def _stores(request: Request):
    return request.app.state.db, request.app.state.file_store


def _export_service(request: Request) -> ExportService:
    app_ctx = getattr(request.app.state, "app_ctx", None)
    if app_ctx is None:
        raise TaskStateConflict(
            "任务执行组件未就绪（模型未配置），导出不可用",
            details={"reason": "app_ctx_not_ready"},
        )
    return ExportService(app_ctx)


# ---- 用例列表 / 详情 ----


@router.get("/tasks/{task_id}/cases")
async def list_cases(
    task_id: str,
    request: Request,
    status: str | None = Query(None),
    review: str | None = Query(None),
    version: int | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=200),
    cursor: str | None = Query(None),
) -> dict:
    db, _ = _stores(request)
    await TaskDAO(db).get(task_id)  # 404 不暴露存在性
    page = await TestcaseDAO(db).list_by_task(
        task_id,
        status=status,
        review=review,
        version=version,
        cursor=checked_cursor(cursor),
        limit=limit,
    )
    return page_response(page, _summary_out)


@router.get("/cases/{case_id}")
async def get_case(case_id: str, request: Request) -> CaseDetailOut:
    db, store = _stores(request)
    row = await TestcaseDAO(db).get(case_id)  # 404
    task = await TaskDAO(db).get(row.task_id)
    md = await store.read_case(task.workspace_id, row.task_id, row.file_path)
    return _detail_out(row, md)


# ---- 编辑（If-Match 乐观锁）----


@router.put("/cases/{case_id}")
async def update_case(
    case_id: str,
    body: CaseUpdateIn,
    request: Request,
    if_match: str | None = Header(None),
) -> CaseDetailOut:
    db, store = _stores(request)
    dao = TestcaseDAO(db)
    row = await dao.get(case_id)  # 404
    if row.status != "active":
        raise ValidationError(
            "仅 active 用例可编辑",
            details={"case_id": case_id, "status": row.status},
        )
    if not if_match:
        raise ValidationError(
            "缺少 If-Match 头（须携带当前 content_hash）",
            details={"header": "If-Match"},
        )
    task = await TaskDAO(db).get(row.task_id)

    # ① 解析用户提交的正文（front-matter 误改以服务端重算为准，dd §10.3④）
    try:
        parsed = parse_case_markdown(body.markdown)
    except ValueError as e:
        raise ValidationError(
            f"用例 Markdown 解析失败：{e}", details={"case_id": case_id}
        ) from e

    # ② 服务端重算元数据：case_id/point_id/stage_version/trace_refs 以 DB 为准，
    #    正文/标题取用户编辑值；priority 从用户 front-matter 解析（非法归 P1）。
    content = CaseFileContent(
        case_id=row.id,
        point_id=row.point_id,
        stage_version=row.stage_version,
        title=parsed.title,
        priority=parsed.priority if parsed.priority in ("P0", "P1", "P2") else "P1",
        preconditions=parsed.preconditions,
        steps=parsed.steps,
        expected=parsed.expected,
        test_data=parsed.test_data,
        trace_refs=row.trace_refs_obj(),
    )

    # ③ 乐观锁覆盖（FileConflict → 409 VERSION_CONFLICT，details 带当前 hash）
    try:
        written = await store.edit_case(
            task.workspace_id, row.task_id, row.file_path, content,
            expected_hash=if_match,
        )
    except FileConflict as e:
        raise VersionConflict(
            "用例已被他人修改",
            details={
                "case_id": case_id,
                "expected": if_match,
                "current": e.details.get("current"),
            },
        ) from e

    # ④ diff 统计（review_record detail；行数粒度）
    diff = _diff_stat(body.markdown, await store.read_case(
        task.workspace_id, row.task_id, row.file_path
    ))

    # ⑤ 落账：内容更新 + review_status→edited_adopted + review_record
    await dao.update_content(
        case_id,
        file_path=written.file_path,
        content_hash=written.content_hash,
        title=content.title,
    )
    await dao.update_review(case_id, ReviewStatus.EDITED_ADOPTED)
    await ReviewDAO(db).append(
        ReviewRow.create(
            id=uuid.uuid4().hex,
            task_id=row.task_id,
            testcase_id=case_id,
            action="edit",
            detail={"diff": diff, "new_hash": written.content_hash},
        )
    )
    logger.info(
        "case edited",
        extra={"case_id": case_id, "task_id": row.task_id, **diff},
    )

    fresh = await dao.get(case_id)
    md = await store.read_case(task.workspace_id, row.task_id, fresh.file_path)
    return _detail_out(fresh, md)


def _diff_stat(old_md: str, new_md: str) -> dict:
    """粗粒度 diff 统计（review_record detail）：新增/删除/变更行数。"""
    old_lines = old_md.splitlines()
    new_lines = new_md.splitlines()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    added = deleted = changed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            deleted += i2 - i1
        elif tag == "replace":
            changed += max(i2 - i1, j2 - j1)
    return {"lines_added": added, "lines_deleted": deleted,
            "lines_changed": changed}


# ---- 批量评审 ----


@router.post("/cases/review")
async def review_cases(
    body: ReviewIn, request: Request,
    idempotency_key: str | None = Header(None),
) -> ReviewOut:
    # WP-29 幂等键去重（tech-design §5.0：批量评审支持 Idempotency-Key）
    cached = idem_check(request, "cases.review", idempotency_key,
                        json.dumps(body.model_dump(), sort_keys=True))
    if cached is not None:
        return ReviewOut(**cached.body)
    db, _ = _stores(request)
    dao = TestcaseDAO(db)
    review_dao = ReviewDAO(db)

    # ① 全量校验（任一非法整体 422，不部分成功，dd §10.3⑤）
    rows: list[CaseRow] = []
    for item in body.items:
        row = await dao.get(item.case_id)  # 404
        rows.append(row)
        if row.status != "active":
            # 非 active（obsolete）用例不可评审（转换表只管 review_status 维度；
            # status 维度的非法操作同走 422，交接单登记）
            raise ReviewTransitionError(
                f"用例 {item.case_id} 非 active（{row.status}），不可评审",
                details={
                    "case_id": item.case_id,
                    "status": row.status,
                    "reason": "case_not_active",
                },
            )
        target = _action_to_status(item.action)
        allowed = _ALLOWED_REVIEW_ACTIONS.get(row.review_status, set())
        if target not in allowed:
            raise ReviewTransitionError(
                f"用例 {item.case_id} 当前评审状态 {row.review_status} "
                f"不允许 {item.action}",
                details={
                    "case_id": item.case_id,
                    "current": row.review_status,
                    "action": item.action,
                    "allowed": sorted(allowed),
                },
            )

    # ② 全部合法才落账（逐条 update_review + review_record，同事务）
    results: list[ReviewResultItem] = []
    async with db.immediate_tx():
        for item, row in zip(body.items, rows):
            target = _action_to_status(item.action)
            await dao.update_review(item.case_id, target)
            await review_dao.append(
                ReviewRow.create(
                    id=uuid.uuid4().hex,
                    task_id=row.task_id,
                    testcase_id=item.case_id,
                    action=item.action,
                    detail={"from": row.review_status, "to": target},
                )
            )
            results.append(
                ReviewResultItem(case_id=item.case_id, ok=True,
                                 review_status=target)
            )
    logger.info(
        "cases reviewed",
        extra={"count": len(results)},
    )
    out = ReviewOut(results=results)
    idem_record(request, "cases.review", idempotency_key,
                json.dumps(body.model_dump(), sort_keys=True), 200, out.model_dump())
    return out


def _action_to_status(action: str) -> str:
    return {
        "adopt": ReviewStatus.ADOPTED.value,
        "reject": ReviewStatus.REJECTED.value,
        "edited_adopted": ReviewStatus.EDITED_ADOPTED.value,
    }[action]


# ---- 局部重生成 ----


@router.post("/cases/regenerate")
async def regenerate(
    body: RegenerateIn, request: Request,
    idempotency_key: str | None = Header(None),
) -> RegenerateOut:
    """dd §7.6 regenerate 行 / tech-design §5.4：走 runtime/regenerate.py
    入口（WP-26 已交付），HTTP 层只做参数组装与 app_ctx 守卫。"""
    # WP-29 幂等键去重（tech-design §5.0：regenerate 必带 Idempotency-Key）
    cached = idem_check(request, "cases.regenerate", idempotency_key,
                        json.dumps(body.model_dump(), sort_keys=True))
    if cached is not None:
        return RegenerateOut(**cached.body)
    app_ctx = getattr(request.app.state, "app_ctx", None)
    if app_ctx is None:
        from ..errors import TaskStateConflict

        raise TaskStateConflict(
            "任务执行组件未就绪（模型未配置），重生成不可用",
            details={"reason": "app_ctx_not_ready"},
        )

    # 任务归属：case_ids 可能跨任务——regenerate_cases 内部逐条校验，
    # 这里先取首个用例定位 task_id（跨任务时 regenerate 内抛 NotFound）。
    db, _ = _stores(request)
    first = await TestcaseDAO(db).get(body.case_ids[0])  # 404

    from ..runtime.regenerate import regenerate_cases

    rows = await regenerate_cases(
        app_ctx,
        first.task_id,
        case_ids=body.case_ids,
        instruction=body.instruction,
        keep_original=body.keep_original,
    )
    out = RegenerateOut(
        task_id=first.task_id,
        status="completed",
        new_case_ids=[r.id for r in rows],
    )
    idem_record(request, "cases.regenerate", idempotency_key,
                json.dumps(body.model_dump(), sort_keys=True), 200, out.model_dump())
    return out


# ---- 导出 ----


@router.post("/tasks/{task_id}/export")
async def export_cases(
    task_id: str, body: ExportIn, request: Request
) -> ExportOutModel:
    svc = _export_service(request)
    out = await svc.export_md_zip(task_id, case_ids=body.case_ids)
    return ExportOutModel(
        status=out.status,
        download_url=out.download_url,
        skipped=out.skipped,
        job_id=out.job_id,
    )


@router.get("/tasks/{task_id}/export/{job_id}")
async def export_result(
    task_id: str,
    job_id: str,
    request: Request,
    download: int = Query(0),
):
    """轮询句柄；``?download=1`` 且 ready 时直接返回 zip 文件流。"""
    svc = _export_service(request)
    if download:
        path = await svc.job_zip_path(task_id, job_id)
        return FileResponse(
            path, media_type="application/zip", filename=f"cases-{job_id[:8]}.zip"
        )
    out = await svc.get_job(task_id, job_id)
    return ExportOutModel(
        status=out.status,
        download_url=out.download_url,
        skipped=out.skipped,
        job_id=out.job_id,
    )
