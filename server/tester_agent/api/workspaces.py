"""工作区路由（WP-25 API-A；tech-design §5.1 / dd §10.1 §10.2 §5.1）。

- CRUD + 软删除：软删行与不存在同等待遇（404 不暴露存在性，WorkspaceDAO 已保证）；
  删除守卫"需确认无活跃任务"→ 有活跃任务 409 TASK_STATE_CONFLICT；
  文件按保留期惰性清理（§6.5/WP-29），本端点只置 deleted_at；
- POST /workspaces/{id}/kb/test：只读探活（factory.probe 一次性连接探测 +
  能力位，dd §8.4），失败按 AppError 类属性映射 HTTP（KbUnreachable→502，
  dd §17.1 为 HTTP 映射唯一来源）；未注册 mode → 400（真实 ReMe 适配
  在 WP-09 注册 builder 后即插即用）。

201 响应带 Location 头（dd §10.1）。
"""

from __future__ import annotations

import uuid
from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from ..errors import TaskStateConflict
from ..store.models import TaskDAO, WorkspaceDAO, WorkspaceRow
from .common import checked_cursor, page_response

router = APIRouter(prefix="/api/v1", tags=["workspaces"])


# ---- 请求/响应模型（dd §10.2 workspace 段） ----


class KbConfigIn(BaseModel):
    """Embedded ReMe kb_config（废除 mode/target HTTP 字段）。"""

    kb_id: str = ""
    knowledge_bases_dir: str = ""
    knowledge_dir: str = "knowledge"
    create_knowledge_base: bool = False
    options: dict = {}

    @model_validator(mode="before")
    @classmethod
    def _reject_service_and_strip_legacy(cls, data):
        if not isinstance(data, dict):
            return data
        if data.get("mode") == "service":
            raise ValueError("mode=service 已废除，请使用嵌入式 sdk")
        return {
            k: v
            for k, v in data.items()
            if k not in ("mode", "target")
        }


class CreateWorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    kb_config: KbConfigIn


class UpdateWorkspaceIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    kb_config: KbConfigIn | None = None


class WorkspaceOut(BaseModel):
    id: str
    name: str
    description: str
    kb_config: dict
    created_at: str


class KbTestOut(BaseModel):
    ok: bool
    latency_ms: int
    capabilities: dict | None = None
    error_code: str | None = None


def _out(ws: WorkspaceRow) -> WorkspaceOut:
    return WorkspaceOut(
        id=ws.id,
        name=ws.name,
        description=ws.description,
        kb_config=ws.kb_config_obj(),
        created_at=ws.created_at,
    )


def _dao(request: Request) -> WorkspaceDAO:
    return WorkspaceDAO(request.app.state.db)


@router.get("/workspaces")
async def list_workspaces(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
):
    page = await _dao(request).list(
        cursor=checked_cursor(cursor), limit=limit
    )
    return page_response(page, _out)


@router.post("/workspaces", status_code=201)
async def create_workspace(
    body: CreateWorkspaceIn, request: Request, response: Response
) -> WorkspaceOut:
    ws = WorkspaceRow.create(
        id=uuid.uuid4().hex,
        name=body.name,
        description=body.description,
        kb_config=body.kb_config.model_dump(),
    )
    await _dao(request).create(ws)
    response.headers["Location"] = f"/api/v1/workspaces/{ws.id}"
    return _out(ws)


@router.get("/workspaces/{workspace_id}")
async def get_workspace(workspace_id: str, request: Request) -> WorkspaceOut:
    return _out(await _dao(request).get(workspace_id))


@router.put("/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str, body: UpdateWorkspaceIn, request: Request
) -> WorkspaceOut:
    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if body.kb_config is not None:
        updates["kb_config"] = body.kb_config.model_dump()
    await _dao(request).update(workspace_id, **updates)
    pool = getattr(request.app.state, "memory_pool", None)
    if pool is not None and body.kb_config is not None:
        await pool.invalidate(workspace_id)
        factory = getattr(request.app.state, "reme_factory", None)
        if factory is not None:
            await factory.invalidate(workspace_id)
    return _out(await _dao(request).get(workspace_id))


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(workspace_id: str, request: Request) -> dict:
    db = request.app.state.db
    dao = WorkspaceDAO(db)
    await dao.get(workspace_id)  # 404（不存在/已删同形态，不暴露存在性）
    if await TaskDAO(db).count_active_by_workspace(workspace_id) > 0:
        raise TaskStateConflict(
            "工作区存在活跃任务，无法删除",
            details={"workspace_id": workspace_id},
        )
    await dao.soft_delete(workspace_id)
    return {"ok": True}


@router.post("/workspaces/{workspace_id}/kb/test")
async def test_kb(workspace_id: str, request: Request) -> KbTestOut:
    ws = await _dao(request).get(workspace_id)  # 404 不暴露存在性
    factory = request.app.state.reme_factory
    probe = await factory.probe(
        workspace_id, ws.kb_config_obj()
    )  # AppError → 信封（§17.1）
    return KbTestOut(
        ok=True,
        latency_ms=probe.latency_ms,
        capabilities=probe.caps.model_dump(),
        error_code=None,
    )
