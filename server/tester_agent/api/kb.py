"""知识库写入提案路由（WP-28 API-D；tech-design §5.6 / dd §11.4 §10.2 §10.3⑥）。

PRD 7 硬性要求：ReMe 写路径物理上只对 L2 暴露——本模块是全系统唯一
import ``ReMeWriter`` 的 API 模块（dd §9.2），writer 实例经
``app.state.kb_writer`` 注入（WP-09：``WorkspaceRoutingWriter`` 按工作区
``kb_config`` 路由 HTTP 写入；非 service 模式仍 502），AppContext/TaskContext
不设写字段，图/运行时不可达。

端点：

- ``POST /kb/proposals``：创建 pending 提案——生成 32 字节一次性确认令牌，
  仅 sha256 入库（confirm_token_hash），**明文令牌仅在本响应返回一次**
  （dd §9.2）；201 + Location；
- ``GET /workspaces/{id}/kb/proposals``：提案列表（``?status=`` 过滤 +
  键集分页）；响应永不携带令牌哈希；
- ``POST /kb/proposals/{id}/confirm``：两阶段确认（dd §11.4 时序）——
  Idempotency-Key 头必带（tech-design §5.0）；事务内校验状态/期限/令牌
  hash 并原子绑定幂等键 → 事务外调 ReMeWriter → 成功置 confirmed +
  write_result 终态；写失败保持 pending + fail_count+1（有效期内可带同
  token 重试，dd §11.4 失败语义）；``verified=False`` 时 write_result 标
  ``needs_manual_check``（S6 预案）；已 confirmed 且幂等键一致 → 重放首次
  WriteResult（dd §9.2"成功带幂等键重放返回首次结果"），键不一致 → 409。

与设计偏离（交接单登记）：

- **令牌有效期 24h**（``PROPOSAL_TOKEN_TTL_SEC``）：dd §11.4 要求
  expires_at 但未给时长，§13.2 亦无对应配置项，v1 取常量 86400s；
- **写失败错误码复用 KB_UNREACHABLE(502, retryable)**：dd §17.1 错误码
  目录（已冻结）无 KB_WRITE_FAILED 专码，网络失败与写入拒绝同码，
  details.error_code 透传 writer 返回值区分；
- 过期判定在 confirm 触达时惰性执行（置 expired + 409 PROPOSAL_EXPIRED），
  与 dd §6.5"过期由惰性任务置 expired"一致（本包不为其新增调度器）。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, Field

from ..adapters.reme import ReMeWriter, UnavailableWriter
from ..domain import ProposalStatus
from ..errors import (
    KbTokenInvalid,
    KbUnreachable,
    NotFoundError,
    ProposalExpired,
    TaskStateConflict,
    ValidationError,
)
from ..logging_config import get_logger
from ..store.db import utcnow_iso
from ..store.models import ProposalDAO, ProposalRow, TaskDAO, WorkspaceDAO
from .common import DEFAULT_LIMIT, checked_cursor, page_response

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["kb-proposals"])

# 确认令牌/提案有效期（dd §11.4 要求 expires_at，未定时长——交接单登记）
PROPOSAL_TOKEN_TTL_SEC = 24 * 3600


# ---- 请求/响应模型（dd §10.2 config/kb proposal 段） ----


class ProposalIn(BaseModel):
    workspace_id: str = Field(min_length=1)
    task_id: str | None = None
    payload: dict = Field(default_factory=dict)


class ProposalCreatedOut(BaseModel):
    id: str
    confirm_token: str  # 明文仅此一次
    expires_at: str


class ProposalOut(BaseModel):
    id: str
    workspace_id: str
    task_id: str | None
    payload: dict
    status: str
    fail_count: int
    idempotency_key: str | None
    expires_at: str
    confirmed_at: str | None
    write_result: dict | None
    created_at: str


class ProposalConfirmIn(BaseModel):
    confirm_token: str = Field(min_length=1)


class WriteResultOut(BaseModel):
    ok: bool
    remote_ref: str | None
    verified: bool
    error_code: str | None
    needs_manual_check: bool = False


# ---- 组装 ----


def _proposal_out(row: ProposalRow) -> ProposalOut:
    """列表/详情响应：永不携带 confirm_token_hash（dd §9.2 只存哈希）。"""
    return ProposalOut(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        payload=row.payload_dict(),
        status=row.status,
        fail_count=row.fail_count,
        idempotency_key=row.idempotency_key,
        expires_at=row.expires_at,
        confirmed_at=row.confirmed_at,
        write_result=row.write_result_dict(),
        created_at=row.created_at,
    )


def _expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=PROPOSAL_TOKEN_TTL_SEC)) \
        .isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _writer(request: Request) -> ReMeWriter:
    return getattr(request.app.state, "kb_writer", None) or UnavailableWriter()


# ---- 创建 / 列表 ----


@router.post("/kb/proposals", status_code=201)
async def create_proposal(
    body: ProposalIn, request: Request, response: Response
) -> ProposalCreatedOut:
    db = request.app.state.db
    await WorkspaceDAO(db).get(body.workspace_id)  # 404
    if body.task_id is not None:
        task = await TaskDAO(db).get(body.task_id)  # 404
        if task.workspace_id != body.workspace_id:
            # 跨工作区不暴露存在性（dd §10.1）
            raise NotFoundError(
                f"任务不存在：{body.task_id}",
                details={"task_id": body.task_id},
            )
    token = secrets.token_hex(32)  # 32 字节随机令牌（dd §9.2）
    row = ProposalRow.create(
        id=uuid.uuid4().hex,
        workspace_id=body.workspace_id,
        task_id=body.task_id,
        payload=body.payload,
        status=ProposalStatus.PENDING.value,
        confirm_token_hash=_token_hash(token),
        expires_at=_expires_at(),
    )
    await ProposalDAO(db).create(row)
    logger.info(
        "kb proposal created",
        extra={"proposal_id": row.id, "workspace_id": body.workspace_id},
    )
    response.headers["Location"] = f"/api/v1/kb/proposals/{row.id}"
    return ProposalCreatedOut(
        id=row.id, confirm_token=token, expires_at=row.expires_at
    )


@router.get("/workspaces/{workspace_id}/kb/proposals")
async def list_proposals(
    workspace_id: str,
    request: Request,
    status: str | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=200),
    cursor: str | None = Query(None),
) -> dict:
    db = request.app.state.db
    await WorkspaceDAO(db).get(workspace_id)  # 404
    page = await ProposalDAO(db).list_by_workspace(
        workspace_id,
        status=status,
        cursor=checked_cursor(cursor),
        limit=limit,
    )
    return page_response(page, _proposal_out)


# ---- 两阶段确认（dd §11.4） ----


@router.post("/kb/proposals/{proposal_id}/confirm")
async def confirm_proposal(
    proposal_id: str,
    body: ProposalConfirmIn,
    request: Request,
    idempotency_key: str | None = Header(None),
) -> WriteResultOut:
    db = request.app.state.db
    dao = ProposalDAO(db)
    if not idempotency_key:
        # tech-design §5.0：kb confirm 幂等键必带
        raise ValidationError(
            "缺少 Idempotency-Key 头（kb 提案确认必带）",
            details={"header": "Idempotency-Key"},
        )

    # ① 校验事务：状态 / 期限 / 令牌 hash / 幂等键绑定（dd §11.4"校验状态/
    #    期限/hash（事务）"；写调用为网络 IO，不入库事务）。
    #    过期需先提交 expired 终态再在事务外抛 409（同事务内 raise 会回滚）。
    expired: str | None = None
    async with db.immediate_tx():
        row = await dao.get(proposal_id)  # 404
        if row.status == ProposalStatus.CONFIRMED.value:
            if row.idempotency_key == idempotency_key:
                # 幂等重放：返回首次 WriteResult（dd §9.2 / tech-design §5.6）
                wr = row.write_result_dict() or {}
                return WriteResultOut(**wr)
            raise TaskStateConflict(
                "提案已确认（幂等键与首次确认不一致）",
                details={"proposal_id": proposal_id, "status": row.status},
            )
        if row.status != ProposalStatus.PENDING.value:
            raise TaskStateConflict(
                f"提案状态 {row.status} 不可确认",
                details={"proposal_id": proposal_id, "status": row.status},
            )
        if not row.confirm_token_hash or (
            _token_hash(body.confirm_token) != row.confirm_token_hash
        ):
            raise KbTokenInvalid(
                "确认令牌缺失或不匹配",
                details={"proposal_id": proposal_id},
            )
        if utcnow_iso() > row.expires_at:
            await dao.set_status(proposal_id, ProposalStatus.EXPIRED.value)
            expired = row.expires_at
        else:
            if row.idempotency_key is None:
                try:
                    await dao.bind_idempotency_key(proposal_id, idempotency_key)
                except sqlite3.IntegrityError as exc:
                    # 幂等键 UNIQUE：另一提案已占用同键
                    raise TaskStateConflict(
                        "Idempotency-Key 已被其他提案占用",
                        details={"idempotency_key": idempotency_key},
                    ) from exc
            elif row.idempotency_key != idempotency_key:
                raise TaskStateConflict(
                    "幂等键与首次确认不一致",
                    details={"proposal_id": proposal_id},
                )
            payload = row.payload_dict()
            # WP-09：注入工作区 id，供 WorkspaceRoutingWriter 解析 kb_config
            # （Protocol 仅 (token, proposal)，不扩形参）
            if isinstance(payload, dict):
                payload = {**payload, "_workspace_id": row.workspace_id}
    if expired is not None:
        raise ProposalExpired(
            "提案确认令牌已过期",
            details={"proposal_id": proposal_id, "expires_at": expired},
        )

    # ② 事务外调 ReMeWriter（dd §11.4"标记 confirming → write"——单 worker
    #    下幂等键已先绑定，并发重入同键走到同一写调用或撞已确认态）
    try:
        result = await _writer(request).write_proposal(
            body.confirm_token, payload
        )
    except Exception:
        # 写失败（网络/拒绝）：保持 pending + fail_count+1，允许令牌有效期内
        # 带同 token 重试（dd §11.4 失败语义）
        await dao.increment_fail_count(proposal_id)
        raise
    if not result.ok:
        fail_count = await dao.increment_fail_count(proposal_id)
        raise KbUnreachable(
            "ReMe 写入失败（可带同令牌与幂等键重试）",
            details={
                "proposal_id": proposal_id,
                "error_code": result.error_code,
                "fail_count": fail_count,
            },
        )

    # ③ 终态落账：confirmed + write_result；verified=False → needs_manual_check
    #    （S6 预案，dd §11.4）
    wr = {
        **result.model_dump(),
        "needs_manual_check": not result.verified,
    }
    await dao.mark_confirmed(proposal_id, wr)
    logger.info(
        "kb proposal confirmed",
        extra={"proposal_id": proposal_id, "verified": result.verified},
    )
    return WriteResultOut(**wr)
