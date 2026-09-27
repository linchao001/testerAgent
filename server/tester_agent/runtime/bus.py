"""EventBus：进程内发布订阅（dd §6.2）。

核心语义（D15 / §6.2）：
1. **先落库后广播**：``emit`` 先写 ``task_event`` 表拿到自增 id，再向订阅者
   队列投递；DB 是事件的唯一事实源，实时队列仅做"快路径"投递。
2. **订阅闭窗防漏防重**：``subscribe`` 在 per-task 锁内完成"注册队列 → 查库回放"，
   与 ``emit``（锁内"落库 → 投递"）互斥，保证事件恰好被消费一次：
   - 落库早于订阅查询 → 走回放；队列注册晚于该次 emit，不进队列 → 不重复；
   - 落库晚于订阅查询 → 队列已注册，进队列走实时 → 不重复、不丢失。
3. **队列满丢弃实时帧但不丢库**：订阅者卡住导致 ``put_nowait`` 抛 QueueFull 时，
   记 warning 并丢弃该实时帧；事件仍在 DB，客户端下次重连靠 ``Last-Event-ID``
   回放补齐（dd §6.2）。

``Emitter`` 是绑定 ``task_id`` 的薄封装，作为 ``TaskContext.emit`` 注入节点，
使节点侧调用形如 ``ctx.emit("node_start", {...})``。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from ..logging_config import get_logger
from ..store.models import EventDAO, EventRow

logger = get_logger(__name__)

# 单订阅者实时队列容量（dd §6.2）；满则丢实时帧保 DB。
_QUEUE_MAXSIZE = 1000


class EventBus:
    """进程内事件总线（dd §6.2）。

    构造时注入 ``EventDAO``；``emit`` 先落库后广播，``subscribe`` 闭窗回放+实时。
    单 worker 进程模型下无需跨进程协调。
    """

    def __init__(self, events: EventDAO) -> None:
        self._events = events
        # task_id -> 订阅者队列集合
        self._subs: dict[str, set[asyncio.Queue]] = {}
        # task_id -> 互斥锁（闭窗：emit 与 subscribe 的注册+回放互斥）
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, task_id: str) -> asyncio.Lock:
        lock = self._locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[task_id] = lock
        return lock

    async def emit(self, task_id: str, type_: str, payload: dict) -> int:
        """先落库后广播，返回自增 event_id（dd §6.2）。

        落库与投递在 per-task 锁内原子完成，确保订阅闭窗语义。
        """
        async with self._lock_for(task_id):
            event_id = await self._events.append(task_id, type_, payload)
            for q in self._subs.get(task_id, ()):
                try:
                    q.put_nowait((event_id, type_, payload))
                except asyncio.QueueFull:
                    # 消费者卡住：丢实时帧，事件已在库，靠重连回放补齐
                    logger.warning(
                        "event queue full, dropping live frame",
                        extra={"task_id": task_id, "event_id": event_id, "type": type_},
                    )
            return event_id

    async def subscribe(
        self, task_id: str, after_id: int = 0
    ) -> AsyncIterator[EventRow]:
        """异步生成器：先回放 ``id > after_id`` 的历史事件，再接实时流。

        注册队列与查库回放在 per-task 锁内完成（闭窗），回放完成后释放锁，
        实时帧经队列投递。生成器关闭（客户端断连）时清理队列注册。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        lock = self._lock_for(task_id)
        async with lock:
            subs = self._subs.setdefault(task_id, set())
            subs.add(q)
            # 回放：与 emit 互斥，保证不重不漏
            replay_rows = await self._events.list_after(task_id, after_id)

        try:
            for row in replay_rows:
                yield row
            # 实时帧
            while True:
                event_id, type_, payload = await q.get()
                yield EventRow(
                    id=event_id,
                    task_id=task_id,
                    type=type_,
                    payload=json.dumps(payload, ensure_ascii=False),
                    created_at="",  # 实时帧不落 created_at；消费方只依赖 id/type/payload
                )
        finally:
            # 客户端断连/生成器关闭：反注册队列
            subs = self._subs.get(task_id)
            if subs is not None:
                subs.discard(q)
                if not subs:
                    self._subs.pop(task_id, None)


class Emitter:
    """绑定 ``task_id`` 的事件发射器，注入 ``TaskContext.emit``（dd §6.1 §6.2）。

    节点侧调用 ``await ctx.emit(type_, payload)``；本类把 task_id 闭包进
    ``EventBus.emit``。``emit`` 为 None 时由 wrap/batch 层静默（单测/eval）。
    """

    def __init__(self, bus: EventBus, task_id: str) -> None:
        self._bus = bus
        self._task_id = task_id

    async def __call__(self, type_: str, payload: dict) -> None:
        await self._bus.emit(self._task_id, type_, payload)
