"""幂等键存储（WP-29；dd §6.6）。

一期内存 TTL（单机单 worker 内足够，dd §6.6 文档标注简化）：

- 键空间为 ``(endpoint, idempotency_key)``；``endpoint`` 为端点逻辑名
  （如 ``cases.review``），避免跨端点键碰撞；
- 命中同键且 ``request_hash`` 相同 → 重放首次响应（status_code + body）；
- 命中同键但 ``request_hash`` 不同 → 409 VERSION_CONFLICT（防错键复用）；
- TTL 24h（``DEFAULT_TTL_SEC``），过期记录惰性清理（``purge_expired``，
  由维护任务/每次写入时扫）。

kb 提案确认不走本存储——其幂等键由 ``kb_proposal.idempotency_key``
唯一索引落库承担（dd §11.4，WP-28 已交付）。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

from ..errors import VersionConflict

DEFAULT_TTL_SEC = 24 * 3600


@dataclass
class IdemRecord:
    request_hash: str
    status_code: int
    body: dict
    expires_at: float


class IdempotencyStore:
    """进程内幂等键存储（dd §6.6 一期简化）。

    单 worker 进程模型下 in-memory dict 合法（不引入跨进程方案，dd §2.1）。
    写操作持锁，读（lookup）也在锁内完成以保证 hash 比对原子性。
    """

    def __init__(self, ttl_sec: int = DEFAULT_TTL_SEC) -> None:
        self._ttl = float(ttl_sec)
        self._data: dict[tuple[str, str], IdemRecord] = {}
        self._lock = threading.Lock()

    @staticmethod
    def request_hash(body_json: str) -> str:
        """请求体哈希（sha256 hex）；空串对应无 body 端点。"""
        return hashlib.sha256(body_json.encode("utf-8")).hexdigest()

    def lookup(
        self, endpoint: str, key: str, request_hash: str
    ) -> IdemRecord | None:
        """查键：命中返回记录；hash 不一致抛 409；未命中/过期返回 None。"""
        now = time.monotonic()
        with self._lock:
            rec = self._data.get((endpoint, key))
            if rec is None:
                return None
            if rec.expires_at <= now:
                del self._data[(endpoint, key)]
                return None
            if rec.request_hash != request_hash:
                raise VersionConflict(
                    "Idempotency-Key 已被用于不同请求体",
                    details={"endpoint": endpoint},
                )
            return rec

    def record(
        self,
        endpoint: str,
        key: str,
        request_hash: str,
        status_code: int,
        body: dict,
    ) -> None:
        """落键：过期记录被覆盖；每写入顺带清一次过期项（惰性）。"""
        now = time.monotonic()
        with self._lock:
            self._data[(endpoint, key)] = IdemRecord(
                request_hash=request_hash,
                status_code=status_code,
                body=body,
                expires_at=now + self._ttl,
            )
            self._purge_expired_locked(now)

    def purge_expired(self) -> int:
        """显式清理过期键，返回删除数（维护任务调用）。"""
        now = time.monotonic()
        with self._lock:
            return self._purge_expired_locked(now)

    def _purge_expired_locked(self, now: float) -> int:
        expired = [k for k, v in self._data.items() if v.expires_at <= now]
        for k in expired:
            del self._data[k]
        return len(expired)

    def __len__(self) -> int:
        return len(self._data)
