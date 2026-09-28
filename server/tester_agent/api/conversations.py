"""会话与消息路由（WP-25 API-A；tech-design §5.2 / dd §10.1 §10.2）。

- 会话归属工作区（tech-design §5.2 标题约束）：POST /conversations 前置校验
  workspace 存在（404），GET /workspaces/{id}/conversations 同样先校验；
- GET /conversations/{id}：会话详情（含任务摘要与分页消息，tech-design §5.2）；
- POST /conversations/{id}/messages：落自由消息；kind=chat 跑 tool_agent 并落
  assistant（payload.tool_trace）；kind=change_request 仅持久化。返回
  SendMessageOut{user, assistant?}。刷新会话 updated_at。tech-design §5.2
  续跑/回退属任务编排（regenerate），不在本包；
- MessageOut 对齐 message 表字段 + author 展示名（dd §10.2；v1 无用户体系，
  按 role 映射固定展示名）。
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..domain import MessageKind, MessageRole
from ..store.models import (
    ConversationDAO,
    ConversationRow,
    MessageDAO,
    MessageRow,
    TaskDAO,
    WorkspaceDAO,
)
from .common import checked_cursor, page_response

router = APIRouter(prefix="/api/v1", tags=["conversations"])

_AUTHOR_NAMES = {  # v1 无用户体系：role → 固定展示名
    MessageRole.USER: "用户",
    MessageRole.ASSISTANT: "助手",
    MessageRole.SYSTEM: "系统",
}


# ---- 请求/响应模型（dd §10.2 conversation / message 段） ----


class CreateConversationIn(BaseModel):
    workspace_id: str = Field(min_length=1)
    title: str | None = None


class SendMessageIn(BaseModel):
    content: str = Field(min_length=1)
    kind: Literal["chat", "change_request"] = "chat"
    context: dict | None = None  # change_request 时可携带目标 stage 提示


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    task_id: str | None
    role: str
    kind: str
    content: str
    ref_artifact_id: str | None
    payload: dict
    created_at: str
    author: str


class SendMessageOut(BaseModel):
    user: MessageOut
    assistant: MessageOut | None = None  # set for kind=chat


class TaskSummaryOut(BaseModel):
    """会话详情里的任务摘要（完整 TaskOut 属 WP-26 tasks API）。"""

    id: str
    status: str
    current_stage: str
    created_at: str
    updated_at: str


class ConversationOut(BaseModel):
    id: str
    workspace_id: str
    title: str
    created_at: str
    updated_at: str


class ConversationDetailOut(ConversationOut):
    tasks: list[TaskSummaryOut]
    messages: dict  # §10.1 Page 信封 {items, next_cursor}


def _msg_out(m: MessageRow) -> MessageOut:
    return MessageOut(
        id=m.id,
        conversation_id=m.conversation_id,
        task_id=m.task_id,
        role=m.role,
        kind=m.kind,
        content=m.content,
        ref_artifact_id=m.ref_artifact_id,
        payload=m.payload_dict(),
        created_at=m.created_at,
        author=_AUTHOR_NAMES.get(m.role, m.role),
    )


def _conv_out(c: ConversationRow) -> ConversationOut:
    return ConversationOut(
        id=c.id,
        workspace_id=c.workspace_id,
        title=c.title,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _db(request: Request):
    return request.app.state.db


@router.get("/workspaces/{workspace_id}/conversations")
async def list_workspace_conversations(
    workspace_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
):
    await WorkspaceDAO(_db(request)).get(workspace_id)  # 404 不暴露存在性
    page = await ConversationDAO(_db(request)).list_by_workspace(
        workspace_id, cursor=checked_cursor(cursor), limit=limit
    )
    return page_response(page, _conv_out)


@router.post("/conversations", status_code=201)
async def create_conversation(body: CreateConversationIn, request: Request) -> ConversationOut:
    await WorkspaceDAO(_db(request)).get(body.workspace_id)  # 404 不暴露存在性
    conv = ConversationRow.create(
        id=uuid.uuid4().hex,
        workspace_id=body.workspace_id,
        title=body.title or "",
    )
    await ConversationDAO(_db(request)).create(conv)
    return _conv_out(conv)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> ConversationDetailOut:
    dao = ConversationDAO(_db(request))
    conv = await dao.get(conversation_id)
    tasks = await TaskDAO(_db(request)).list_by_conversation(
        conversation_id, cursor=None, limit=200
    )
    messages = await MessageDAO(_db(request)).list_by_conversation(
        conversation_id, cursor=checked_cursor(cursor), limit=limit
    )
    return ConversationDetailOut(
        **_conv_out(conv).model_dump(),
        tasks=[
            TaskSummaryOut(
                id=t.id,
                status=t.status,
                current_stage=t.current_stage,
                created_at=t.created_at,
                updated_at=t.updated_at,
            )
            for t in tasks.items
        ],
        messages=page_response(messages, _msg_out),
    )


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
):
    await ConversationDAO(_db(request)).get(conversation_id)  # 404
    page = await MessageDAO(_db(request)).list_by_conversation(
        conversation_id, cursor=checked_cursor(cursor), limit=limit
    )
    return page_response(page, _msg_out)


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def send_message(
    conversation_id: str, body: SendMessageIn, request: Request
) -> SendMessageOut:
    from ..runtime.chat_agent import run_chat_turn

    conv_dao = ConversationDAO(_db(request))
    conv = await conv_dao.get(conversation_id)  # 404
    msg = MessageRow.create(
        id=uuid.uuid4().hex,
        conversation_id=conversation_id,
        role=MessageRole.USER,
        kind=MessageKind(body.kind),
        content=body.content,
        payload=body.context or {},
    )
    await MessageDAO(_db(request)).put(msg)
    await conv_dao.touch(conversation_id)  # §5.2 发消息刷新会话 updated_at

    assistant_out: MessageOut | None = None
    if body.kind == "chat":
        assistant = await run_chat_turn(
            db=_db(request),
            conv=conv,
            file_store=request.app.state.file_store,
            user_message=msg,
        )
        assistant_out = _msg_out(assistant)
        await conv_dao.touch(conversation_id)

    return SendMessageOut(user=_msg_out(msg), assistant=assistant_out)
