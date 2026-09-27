"""智能体路由（WP-25 API-A；tech-design §5.1 / dd §10.1）。

- /agents CRUD：一期 UI 不开放创建，接口预留（tech-design §5.1）；硬删，
  agent_workspace 绑定行随 ON DELETE CASCADE 级联清理（AgentDAO.delete）；
- /workspaces/{id}/agents：工作区绑定智能体的列表 / 绑定（多对多，D11）。
  绑定前置校验工作区与智能体均存在——二者缺失同样返回 404 NOT_FOUND，
  不暴露存在性差异（dd §10.1：跨工作区不命中不暴露存在性）。绑定幂等
  （AgentDAO.bind INSERT OR IGNORE），返回 200 与智能体详情。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..errors import NotFoundError
from ..store.models import AgentDAO, AgentRow, WorkspaceDAO
from .common import checked_cursor, page_response

router = APIRouter(prefix="/api/v1", tags=["agents"])


# ---- 请求/响应模型（agent 表字段对齐；builtin 仅系统内置，API 不可写） ----


class CreateAgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    agent_type: str = "case_designer"
    config: dict = {}


class UpdateAgentIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    config: dict | None = None


class BindAgentIn(BaseModel):
    agent_id: str = Field(min_length=1)


class AgentOut(BaseModel):
    id: str
    name: str
    agent_type: str
    config: dict
    builtin: bool
    created_at: str


def _out(agent: AgentRow) -> AgentOut:
    return AgentOut(
        id=agent.id,
        name=agent.name,
        agent_type=agent.agent_type,
        config=agent.config_obj(),
        builtin=bool(agent.builtin),
        created_at=agent.created_at,
    )


def _dao(request: Request) -> AgentDAO:
    return AgentDAO(request.app.state.db)


@router.get("/agents")
async def list_agents(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
):
    page = await _dao(request).list(cursor=checked_cursor(cursor), limit=limit)
    return page_response(page, _out)


@router.post("/agents", status_code=201)
async def create_agent(body: CreateAgentIn, request: Request) -> AgentOut:
    agent = AgentRow.create(
        id=uuid.uuid4().hex,
        name=body.name,
        agent_type=body.agent_type,
        config=body.config,
    )
    await _dao(request).create(agent)
    return _out(agent)


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request) -> AgentOut:
    return _out(await _dao(request).get(agent_id))


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, body: UpdateAgentIn, request: Request) -> AgentOut:
    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.config is not None:
        updates["config"] = body.config
    dao = _dao(request)
    await dao.update(agent_id, **updates)
    return _out(await dao.get(agent_id))


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, request: Request) -> dict:
    await _dao(request).delete(agent_id)
    return {"ok": True}


# ---- 工作区绑定（tech-design §5.1 GET/POST /workspaces/{id}/agents） ----


@router.get("/workspaces/{workspace_id}/agents")
async def list_workspace_agents(
    workspace_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
):
    # 先校验工作区存在（不存在/已删 → 404，不暴露绑定关系存在性）
    await WorkspaceDAO(request.app.state.db).get(workspace_id)
    page = await _dao(request).list_for_workspace(
        workspace_id, cursor=checked_cursor(cursor), limit=limit
    )
    return page_response(page, _out)


@router.post("/workspaces/{workspace_id}/agents")
async def bind_workspace_agent(
    workspace_id: str, body: BindAgentIn, request: Request
) -> AgentOut:
    dao = _dao(request)
    # 工作区与智能体缺失均 404 NOT_FOUND：状态码与 code 一致，不暴露存在性
    try:
        await WorkspaceDAO(request.app.state.db).get(workspace_id)
        agent = await dao.get(body.agent_id)
    except NotFoundError:
        raise NotFoundError("资源不存在") from None
    await dao.bind(body.agent_id, workspace_id)
    return _out(agent)
