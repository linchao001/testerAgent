"""SSE 事件流端点（dd §6.2 §10.4）。

``GET /api/v1/tasks/{task_id}/events``：

- 响应 ``Content-Type: text/event-stream``，每事件帧格式：
  ``id: {event_id}\\nevent: {type}\\ndata: {payload_json}\\n\\n``；
- 支持 ``Last-Event-ID`` 头（或 ``?after_event_id=`` 查询参数）回放：服务端按
  ``task_event.id`` 顺序补发 ``id > after_id`` 的历史事件后再接实时流；
- 每 15s 发一行 ``: ping`` 注释心跳保活；客户端断连则生成器 ``aclose`` 反注册。

EventBus 经 ``request.app.state.bus`` 取（main.py lifespan 构造），端点本身
不直接依赖 DAO。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from ..runtime.bus import EventBus
from ..store.models import EventRow

_PING_INTERVAL_SEC = 15  # dd §6.2：每 15s 心跳

# WP-32/33：上下文层 SSE 事件名（api/context 与 intervention 共用登记面）
CONTEXT_COMMAND_EXECUTED = "context_command_executed"
CONTEXT_POLICY_CHANGED = "context_policy_changed"
CONTEXT_EVENT_TYPES = frozenset(
    {CONTEXT_COMMAND_EXECUTED, CONTEXT_POLICY_CHANGED}
)


async def events_endpoint(
    task_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after_event_id: int | None = Query(default=None),
) -> StreamingResponse:
    """SSE 事件流端点处理函数（``GET /api/v1/tasks/{task_id}/events``）。

    抽到模块顶层以便测试直接调用（不经路由分发）。
    """
    bus: EventBus = request.app.state.bus
    after = _resolve_after(after_event_id, last_event_id)

    async def event_stream() -> AsyncIterator[bytes]:
        # 先发一帧注释立即使响应头刷新（SSE 惯例；避免首字节延迟）
        yield b": ping\n\n"
        try:
            async for row in bus.subscribe(task_id, after_id=after):
                yield _serialize(row)
        except asyncio.CancelledError:
            # 客户端断连：StreamingResponse 取消生成器，EventBus.subscribe 的
            # finally 负责反注册队列；此处静默退出。
            return

    return StreamingResponse(
        _ping_wrapper(event_stream()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用反向代理缓冲
        },
    )


def make_events_router() -> APIRouter:
    router = APIRouter()
    router.add_api_route(
        "/api/v1/tasks/{task_id}/events",
        events_endpoint,
        methods=["GET"],
    )
    return router


def _resolve_after(after_param: int | None, last_event_id: str | None) -> int:
    """解析回放游标：``?after_event_id=`` 优先，其次 ``Last-Event-ID`` 头。

    非法值退回 0（从头回放）；dd §6.2 允许不支持自定义头的环境用查询参数。
    """
    if after_param is not None:
        return max(0, int(after_param))
    if last_event_id is not None:
        try:
            return max(0, int(last_event_id))
        except ValueError:
            return 0
    return 0


def _serialize(row: EventRow) -> bytes:
    """序列化为 SSE 帧（dd §6.2 §10.4）：id/event/data 三段，空行分隔。"""
    data = json.dumps(row.payload_dict(), ensure_ascii=False)
    frame = f"id: {row.id}\nevent: {row.type}\ndata: {data}\n\n"
    return frame.encode("utf-8")


async def _ping_wrapper(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """在事件流外包一层 15s 心跳：无事件时发 ``: ping`` 注释行保活（dd §6.2）。

    实现：把 ``stream`` 包成任务，用 ``asyncio.wait`` 带 15s 超时取事件；超时
    则发 ping 继续。事件帧与 ping 都按 SSE 协议以 ``\\n\\n`` 结尾。
    """
    PING = b": ping\n\n"
    pending: set[asyncio.Task] = set()

    def _next() -> asyncio.Task:
        t = asyncio.create_task(stream.__anext__())
        pending.add(t)
        return t

    task = _next()
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_PING_INTERVAL_SEC)
            if not done:
                yield PING
                continue
            # 取出已完成任务的结果
            pending.discard(task)
            try:
                chunk = task.result()
            except StopAsyncIteration:
                return
            yield chunk
            task = _next()
    finally:
        task.cancel()
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
