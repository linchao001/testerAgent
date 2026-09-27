"""WP-21 EventBus + SSE 端点测试（dd §6.2 §10.4）。

验收口径：
- EventBus：先落库后广播、订阅闭窗防漏防重（回放与实时恰好一次）、多订阅者各得全量、
  队列满丢实时帧但 DB 不丢、生成器关闭反注册、Emitter 绑定 task_id；
- SSE 端点：text/event-stream、Last-Event-ID / after_event_id 回放、实时事件转发、
  15s ping 心跳、客户端断连清理。

SSE 端点测试直接调用路由处理函数并在当前事件循环内消费 StreamingResponse 的
body 迭代器（避免 httpx ASGITransport 对长连接流的缓冲问题，以及 asyncio 原语跨环）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.api.events import _resolve_after, _serialize, events_endpoint, make_events_router
from tester_agent.runtime.bus import Emitter, EventBus
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import EventDAO, EventRow


# ---------- 夹具 ----------


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "app.db"
    run_migrations(db_path)
    handle = Database(db_path)
    yield handle
    handle.close()


@pytest.fixture()
def event_dao(db):
    return EventDAO(db)


@pytest.fixture()
def bus(event_dao):
    return EventBus(event_dao)


@pytest.fixture()
def app(bus):
    """构造带 EventBus 的 FastAPI 应用（仅 SSE 路由）。"""
    app = FastAPI()
    app.state.bus = bus
    app.include_router(make_events_router())
    return app


async def _call_events(app, task_id: str, *, after: int | None = None,
                       last_event_id: str | None = None):
    """直接调用 SSE 端点处理函数，返回 StreamingResponse（绕过 HTTP 传输层，
    在当前事件循环内消费 body 迭代器，避免 asyncio 原语跨环问题）。"""
    from fastapi.requests import Request

    scope = {"type": "http", "method": "GET",
             "path": f"/api/v1/tasks/{task_id}/events",
             "headers": [], "query_string": b"", "app": app}
    request = Request(scope)
    request._app = app  # 使 request.app 可用

    return await events_endpoint(
        task_id=task_id,
        request=request,
        last_event_id=last_event_id,
        after_event_id=after,
    )


async def _seed_task(db, task_id: str = "t1") -> None:
    """建外键链：workspace → conversation → task（task_event 依赖 task）。"""
    from tester_agent.store.models import (
        ConversationDAO,
        ConversationRow,
        TaskDAO,
        TaskRow,
        WorkspaceDAO,
        WorkspaceRow,
    )

    await WorkspaceDAO(db).create(WorkspaceRow.create(id="ws1", name="ws1"))
    await ConversationDAO(db).create(
        ConversationRow.create(id="c1", workspace_id="ws1")
    )
    await TaskDAO(db).create(
        TaskRow.create(
            id=task_id,
            conversation_id="c1",
            workspace_id="ws1",
            status="running",
            current_stage="link_identify",
            langgraph_thread_id=f"th-{task_id}",
            graph_run_id="r1",
        )
    )


# ---------- EventBus ----------


class TestEventBusEmit:
    async def test_emit_persists_and_returns_id(self, bus, event_dao):
        await _seed_task(bus._events._db)
        eid = await bus.emit("t1", "node_start", {"node": "intake"})
        assert eid == 1
        rows = await event_dao.list_after("t1", 0)
        assert len(rows) == 1
        assert rows[0].id == 1
        assert rows[0].type == "node_start"
        assert rows[0].payload_dict() == {"node": "intake"}

    async def test_emit_broadcasts_to_subscriber(self, bus):
        await _seed_task(bus._events._db)

        async def consume():
            async for row in bus.subscribe("t1", after_id=0):
                return row

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)  # 让订阅者注册
        await bus.emit("t1", "node_end", {"node": "intake", "latency_ms": 10})
        row = await asyncio.wait_for(consumer, timeout=1.0)
        assert row.type == "node_end"
        assert row.payload_dict() == {"node": "intake", "latency_ms": 10}
        assert row.id == 1


class TestEventBusReplayAndDedup:
    async def test_subscribe_replays_history_after_id(self, bus, event_dao):
        await _seed_task(bus._events._db)
        for i in range(3):
            await event_dao.append("t1", f"e{i}", {"i": i})

        rows: list = []
        async for row in bus.subscribe("t1", after_id=1):
            rows.append(row)
            if len(rows) == 2:
                break
        assert [r.id for r in rows] == [2, 3]

    async def test_no_duplicate_no_loss_under_concurrent_emit(self, bus, event_dao):
        """闭窗语义：订阅期间 emit 的事件恰好一次（不重不漏）。"""
        await _seed_task(bus._events._db)
        await event_dao.append("t1", "h0", {})
        await event_dao.append("t1", "h1", {})

        seen_ids: set[int] = set()

        async def consume():
            async for row in bus.subscribe("t1", after_id=0):
                seen_ids.add(row.id)
                if row.id >= 6:
                    break

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        for i in range(2, 6):
            await bus.emit("t1", f"e{i}", {"i": i})
        await asyncio.wait_for(consumer, timeout=2.0)
        assert seen_ids == {1, 2, 3, 4, 5, 6}
        assert len(seen_ids) == 6


class TestEventBusMultipleSubscribers:
    async def test_multiple_subscribers_each_get_all(self, bus):
        await _seed_task(bus._events._db)
        received: dict[str, list] = {"a": [], "b": []}

        async def consume(name: str):
            async for row in bus.subscribe("t1", after_id=0):
                received[name].append(row.id)
                if row.id >= 3:
                    break

        ca = asyncio.create_task(consume("a"))
        cb = asyncio.create_task(consume("b"))
        await asyncio.sleep(0.05)
        for i in range(3):
            await bus.emit("t1", f"e{i}", {})
        await asyncio.wait_for(asyncio.gather(ca, cb), timeout=2.0)
        assert received["a"] == [1, 2, 3]
        assert received["b"] == [1, 2, 3]


class TestEventBusQueueOverflow:
    async def test_full_queue_drops_live_but_keeps_db(self, bus, event_dao, monkeypatch):
        await _seed_task(bus._events._db)
        monkeypatch.setattr("tester_agent.runtime.bus._QUEUE_MAXSIZE", 1)

        async def slow_consumer():
            async for _ in bus.subscribe("t1", after_id=0):
                await asyncio.Event().wait()

        consumer = asyncio.create_task(slow_consumer())
        await asyncio.sleep(0.05)
        await bus.emit("t1", "e0", {})
        await bus.emit("t1", "e1", {})
        await bus.emit("t1", "e2", {})
        await asyncio.sleep(0.05)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

        rows = await event_dao.list_after("t1", 0)
        assert [r.id for r in rows] == [1, 2, 3]


class TestEventBusUnsubscribe:
    async def test_generator_close_removes_queue(self, bus, event_dao):
        await _seed_task(bus._events._db)
        assert "t1" not in bus._subs

        await event_dao.append("t1", "e0", {})
        gen = bus.subscribe("t1", after_id=0)
        row = await gen.__anext__()
        assert row.id == 1
        assert "t1" in bus._subs
        assert len(bus._subs["t1"]) == 1

        await gen.aclose()
        assert "t1" not in bus._subs


class TestEmitter:
    async def test_emitter_binds_task_id(self, bus, event_dao):
        await _seed_task(bus._events._db)
        emitter = Emitter(bus, "t1")
        await emitter("node_start", {"node": "intake"})
        rows = await event_dao.list_after("t1", 0)
        assert rows[0].type == "node_start"
        assert rows[0].payload_dict() == {"node": "intake"}


# ---------- SSE 序列化与游标解析（纯函数单测） ----------


class TestSSEHelpers:
    def test_serialize_frame(self):
        row = EventRow(id=42, task_id="t1", type="node_start",
                       payload=json.dumps({"node": "intake"}))
        frame = _serialize(row).decode("utf-8")
        assert frame == 'id: 42\nevent: node_start\ndata: {"node": "intake"}\n\n'

    def test_serialize_chinese_payload(self):
        row = EventRow(id=1, task_id="t1", type="coverage_ready",
                       payload=json.dumps({"warnings": ["未覆盖条款"]}))
        frame = _serialize(row).decode("utf-8")
        assert "未覆盖条款" in frame

    @pytest.mark.parametrize(
        "after_param,header,expected",
        [
            (5, None, 5),
            (None, "10", 10),
            (3, "10", 3),          # query 优先
            (None, None, 0),
            (None, "not-a-number", 0),
            (-1, None, 0),          # 负值归零
        ],
    )
    def test_resolve_after(self, after_param, header, expected):
        assert _resolve_after(after_param, header) == expected


# ---------- SSE 端点（直接调用路由 + 消费 body 迭代器） ----------


async def _collect_lines(resp, *, stop_after=None, stop_on_substr=None,
                         max_chunks=200):
    """消费 StreamingResponse 的 body 迭代器，返回非空行列表。"""
    lines = []
    count = 0
    async for chunk in resp.body_iterator:
        count += 1
        for raw in chunk.decode("utf-8").split("\n"):
            if raw:
                lines.append(raw)
        if stop_after is not None and len(lines) >= stop_after:
            break
        if stop_on_substr is not None and stop_on_substr in "\n".join(lines):
            break
        if count >= max_chunks:
            break
    return lines


class TestSSEEndpoint:
    async def test_content_type_and_headers(self, app, bus):
        await _seed_task(bus._events._db)
        resp = await _call_events(app, "t1")
        assert resp.status_code == 200
        assert resp.media_type == "text/event-stream"
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"

        lines = await _collect_lines(resp, stop_after=1)
        assert lines[0] == ": ping"  # 首字节刷新

    async def test_replay_via_after_event_id(self, app, bus, event_dao):
        await _seed_task(bus._events._db)
        for i in range(3):
            await event_dao.append("t1", f"e{i}", {"i": i})

        resp = await _call_events(app, "t1", after=1)
        lines = await _collect_lines(resp, stop_after=6)
        text = "\n".join(lines)
        assert "id: 2" in text
        assert "event: e1" in text
        assert "id: 3" in text
        assert 'data: {"i": 2}' in text
        assert "id: 1\n" not in text

    async def test_replay_via_last_event_id_header(self, app, bus, event_dao):
        await _seed_task(bus._events._db)
        await event_dao.append("t1", "e0", {"i": 0})
        await event_dao.append("t1", "e1", {"i": 1})

        resp = await _call_events(app, "t1", last_event_id="1")
        lines = await _collect_lines(resp, stop_after=3)
        text = "\n".join(lines)
        assert "id: 2" in text
        assert "id: 1\n" not in text

    async def test_live_event_streamed(self, app, bus):
        await _seed_task(bus._events._db)

        async def emit_later():
            await asyncio.sleep(0.1)
            await bus.emit("t1", "node_start", {"node": "intake"})

        emitter = asyncio.create_task(emit_later())
        resp = await _call_events(app, "t1")
        lines = await _collect_lines(resp, stop_on_substr="node_start")
        await emitter
        text = "\n".join(lines)
        assert "event: node_start" in text
        assert 'data: {"node": "intake"}' in text

    async def test_ping_heartbeat(self, app, bus, monkeypatch):
        await _seed_task(bus._events._db)
        monkeypatch.setattr("tester_agent.api.events._PING_INTERVAL_SEC", 0.05)

        resp = await _call_events(app, "t1")
        lines = await _collect_lines(resp, stop_on_substr=None, max_chunks=10)
        pings = sum(1 for ln in lines if ln == ": ping")
        assert pings >= 2

    async def test_disconnect_cleans_up_subscription(self, app, bus, monkeypatch):
        await _seed_task(bus._events._db)
        monkeypatch.setattr("tester_agent.api.events._PING_INTERVAL_SEC", 0.05)
        assert "t1" not in bus._subs

        resp = await _call_events(app, "t1")
        # 消费两帧：初始 ping + 一次超时 ping（后者说明 bus.subscribe 已注册队列并阻塞）
        count = 0
        async for _ in resp.body_iterator:
            count += 1
            if count >= 2:
                break
        await asyncio.sleep(0.05)
        assert "t1" in bus._subs

        await resp.body_iterator.aclose()
        await asyncio.sleep(0.1)
        assert "t1" not in bus._subs

