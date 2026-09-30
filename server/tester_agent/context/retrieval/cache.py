"""RetrievalCache（dd §8.5）：进程内 LRU，容量 512，按 task 分区、run 开始清空。

- key 口径（dd §8.5）：
  - recall：``sha1(ws|kb_id|query.text|topk|types)`` → 候选列表；
  - rerank/extract：``entry_id|entry_version|intent_hash`` → 分数/段落
    （entry_version 在键内，即 §8.5"value 带 entry_version 校验"的落法：
    版本变化天然换键，不会命中陈旧内容）。
- 分区语义：缓存只在**同一 graph_run_id 内**复用。任务方（WP-12 管线）在新
  run 启动时调 :meth:`RetrievalCache.start_run`——run_id 与当前分区不一致则
  清空该 task 的全部条目；一致（同 run 内重入/批次间）则保留。
- 实现：内部键 = ``task_id`` + 业务键（避免同 ws/kb 的不同任务互相命中）；
  OrderedDict 头出尾入实现 LRU；全程无 await，事件循环内天然原子，不加锁。

本模块只提供容器与键构造函数；recall 结果的读写接线在 WP-12 管线，
rerank/extract 由 :mod:`.ops_b` 算子直接消费。
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Sequence

DEFAULT_CACHE_CAPACITY = 512

_SEP = "\x1f"


def intent_hash(intent: str) -> str:
    """rerank/extract 键的意图摘要（sha1 前 12 位，与本地 hash 风格一致）。"""
    return hashlib.sha1(intent.encode("utf-8")).hexdigest()[:12]


def recall_key(
    workspace_id: str,
    kb_id: str,
    query_text: str,
    topk: int,
    types: Sequence[str] | None,
) -> str:
    """recall 缓存键（dd §8.5）；types 排序归一，避免顺序差造成假 miss。"""
    types_part = ",".join(sorted(types)) if types else ""
    raw = _SEP.join([workspace_id, kb_id, query_text, str(topk), types_part])
    return "recall:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def entry_key(kind: str, entry_id: str, entry_version: str, intent: str) -> str:
    """rerank/extract 缓存键：``{kind}:{entry_id}|{entry_version}|{intent_hash}``。"""
    return f"{kind}:{entry_id}|{entry_version}|{intent_hash(intent)}"


class RetrievalCache:
    """进程内 LRU 缓存（task 分区 + run 级失效）。容量默认 512（dd §8.5）。"""

    def __init__(self, capacity: int = DEFAULT_CACHE_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity 必须 >= 1")
        self._capacity = capacity
        # fkey(task_id+业务键) -> value；OrderedDict 顺序即 LRU 次序（头=最久未用）
        self._store: OrderedDict[str, object] = OrderedDict()
        # task_id -> 当前 run_id（分区持有权）
        self._runs: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._store)

    # ---- run 分区生命周期 ----

    def start_run(self, task_id: str, run_id: str) -> None:
        """新 run 开始：run_id 变化则清空该 task 分区；同 run 重入为幂等 no-op。"""
        if self._runs.get(task_id) == run_id:
            return
        prefix = task_id + _SEP
        for fkey in [k for k in self._store if k.startswith(prefix)]:
            del self._store[fkey]
        self._runs[task_id] = run_id

    # ---- 读写（调用方约定：必须先 start_run 建立分区） ----

    def get(self, task_id: str, key: str) -> object | None:
        """读缓存；命中则提升为最近使用。未命中返回 None。"""
        fkey = task_id + _SEP + key
        if fkey not in self._store:
            return None
        self._store.move_to_end(fkey)
        return self._store[fkey]

    def put(self, task_id: str, key: str, value: object) -> None:
        """写缓存；超出容量逐出最久未用条目（可跨 task 分区）。"""
        fkey = task_id + _SEP + key
        if fkey in self._store:
            self._store[fkey] = value
            self._store.move_to_end(fkey)
            return
        while len(self._store) >= self._capacity:
            self._store.popitem(last=False)
        self._store[fkey] = value
