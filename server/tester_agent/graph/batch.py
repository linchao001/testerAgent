"""批次执行器（dd §7.3 批处理节点共用骨架）。

point_write / case_generate / coverage_check 的补充生成都走本骨架。职责：

1. **取消检查**：每批边界调 ``ctx.cancelled()``，为真抛 ``TaskCancelled``
   （节点入口另有一次，此处防长批中途取消，dd §6.4③）；
2. **断点跳过**：``artifact.progress`` 中 status=done 的批直接跳过——崩溃
   恢复时不重做已完成批；started 批视为未完成重做（崩溃点可能在落库前）；
3. **幂等**：``idempotency_nonce`` 由 ``(task_id, run_id, node)`` 确定性
   派生（uuid5），同 run 崩溃恢复恒等、新 run 自动变化；批 idem =
   ``deterministic_key(task_id, run_id, node, batch_id, nonce)``，供
   worker 做孤儿清理（dd §7.3 非确定性防护）；
4. **事件 / progress 落盘**：每批 started/done 写 ``artifact.progress``
   并发 ``batch_progress`` 事件；失败时 mark_failed 后原样上抛（Runner
   置 failed，批可整批重做）。

与设计偏离（WP-18 交接单登记）：① ``idempotency_nonce`` 改为确定性派生
而非随机生成——确保同 run 崩溃恢复时 nonce 不变（state.batch_cursor 仅在
节点出口回写，崩溃时可能丢失，确定性 nonce 是崩溃恢复幂等的兜底）；
② ``units`` 接受单元对象列表（非纯 ID 列表），由 ``unit_id``/``result_id``
回调取 id，避免 worker 二次查表；③ 返回 ``(全部结果, 更新后 cursor dict)``，
cursor 由节点写回 state.batch_cursor（DD 伪码在骨架内直接改 cursor，
此处显式返回以便节点统一组装状态增量）。
"""

from __future__ import annotations

import hashlib
import uuid
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..context.models import Phase
from ..context.scopes import scope
from ..errors import TaskCancelled
from ..logging_config import get_logger

if TYPE_CHECKING:
    from ..runtime.context import TaskContext
    from ..store.models import ArtifactRow

logger = get_logger(__name__)

# 进度状态枚举值（与 artifact.progress 内每条记录的 status 字段一致）
STATUS_STARTED = "started"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


# ---------- 幂等键 / 游标 ----------


def start_cursor(task_id: str, run_id: str, node: str) -> dict:
    """初始批次游标（dd §7.3）。

    ``idempotency_nonce`` 由 ``(task_id, run_id, node)`` 经 uuid5 确定性
    派生：同 run 崩溃恢复再生同一 nonce → 批 idem 不变 → 严格幂等；
    新 run（run_id 变）→ 新 nonce → 允许产生新版本（dd §7.3"随新 run 重新
    生成"）。``next_index`` 仅作跳过优化，崩溃恢复时即便为 0 也靠 progress
    的 done 标记跳过已完成批。
    """
    nonce = uuid.uuid5(
        uuid.NAMESPACE_URL, f"{task_id}|{run_id}|{node}"
    ).hex
    return {"idempotency_nonce": nonce, "next_index": 0}


def deterministic_key(
    task_id: str, run_id: str, node: str, batch_id: str, nonce: str
) -> str:
    """批幂等键（dd §7.3）：同 run 同批恒等，供 worker 失败重做后清理孤儿。"""
    raw = f"{task_id}|{run_id}|{node}|{batch_id}|{nonce}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------- BatchProgress：artifact.progress 的内存管理 ----------


class BatchProgress:
    """``artifact.progress``（list[dict]，每批一条记录）的内存视图。

    每条记录形态::

        {"batch_id": str, "node": str, "unit_ids": list[str],
         "status": "started"|"done"|"failed", "idem": str,
         "result_ids": list[str]}

    崩溃恢复语义：``done`` 跳过；``started``/``failed`` 重做（落库可能未完成
    或产物不完整）。
    """

    def __init__(self, node: str, records: list[dict]):
        self._node = node
        self._records: list[dict] = []
        for r in records:
            if isinstance(r, dict) and r.get("batch_id"):
                self._records.append(dict(r))

    @classmethod
    def from_artifact(
        cls, node: str, artifact: "ArtifactRow | None"
    ) -> "BatchProgress":
        if artifact is None or not artifact.progress:
            return cls(node, [])
        return cls(node, artifact.progress_list())

    def _find(self, batch_id: str) -> dict | None:
        for r in self._records:
            if r.get("batch_id") == batch_id:
                return r
        return None

    def status(self, batch_id: str) -> str | None:
        r = self._find(batch_id)
        return r.get("status") if r else None

    def mark_started(
        self, batch_id: str, unit_ids: list[str], idem: str
    ) -> None:
        record = {
            "batch_id": batch_id,
            "node": self._node,
            "unit_ids": list(unit_ids),
            "status": STATUS_STARTED,
            "idem": idem,
            "result_ids": [],
        }
        existing = self._find(batch_id)
        if existing is not None:
            existing.update(record)
        else:
            self._records.append(record)

    def mark_done(self, batch_id: str, result_ids: list[str]) -> None:
        r = self._find(batch_id)
        if r is not None:
            r["status"] = STATUS_DONE
            r["result_ids"] = list(result_ids)

    def mark_failed(self, batch_id: str) -> None:
        r = self._find(batch_id)
        if r is not None:
            r["status"] = STATUS_FAILED

    def dump(self) -> list[dict]:
        return [dict(r) for r in self._records]


# ---------- run_in_batches 骨架 ----------


async def run_in_batches(
    ctx: "TaskContext",
    node: str,
    units: list,
    worker: Callable[["TaskContext", str, list], Awaitable[list]],
    *,
    size: int,
    state: dict | None,
    unit_id: Callable[[Any], str],
    result_id: Callable[[Any], str],
) -> tuple[list, dict]:
    """dd §7.3 批次执行器。

    :param ctx: 任务上下文（须注入 daos.artifact）
    :param node: 阶段名（落库字符串，如 STAGE_POINT_WRITE）
    :param units: 全部工作单元（顺序稳定，如 StoryRef 列表）
    :param worker: ``(ctx, batch_id, unit_subset) -> list[产物]``，产物由
        ``result_id`` 取 id 写入 progress.result_ids
    :param size: 每批单元数
    :param state: 图状态（读 ``batch_cursor[node]``；崩溃恢复时复用游标）
    :param unit_id: 从单元取 id（写 progress.unit_ids）
    :param result_id: 从产物取 id（写 progress.result_ids）
    :return: ``(全部产物列表, 更新后 cursor dict)``；cursor 由调用方写回
        ``state.batch_cursor[node]``。

    前置条件：调用方须已创建该阶段的 active artifact（progress 写入目标）。
    """
    if ctx.daos is None or ctx.daos.artifact is None:
        raise RuntimeError(
            f"run_in_batches 缺少 ctx.daos.artifact（node={node}）"
        )
    artifact = await ctx.daos.artifact.get_active(ctx.task.id, node)
    if artifact is None:
        raise RuntimeError(
            f"run_in_batches 前置 artifact 不存在（node={node}）"
        )

    progress = BatchProgress.from_artifact(node, artifact)
    cursor = (state or {}).get("batch_cursor", {}).get(node)
    if not isinstance(cursor, dict) or "idempotency_nonce" not in cursor:
        cursor = start_cursor(ctx.task.id, ctx.run_id, node)
    nonce = cursor["idempotency_nonce"]

    all_results: list = []
    n = len(units)
    for start in range(0, n, size):
        if await ctx.cancelled():
            raise TaskCancelled(f"批次执行被取消：node={node} batch=b{start // size}")
        subset = units[start : start + size]
        batch_id = f"b{start // size}"

        if progress.status(batch_id) == STATUS_DONE:
            logger.debug(
                "batch skip done",
                extra={"node": node, "batch_id": batch_id},
            )
            continue

        idem = deterministic_key(ctx.task.id, ctx.run_id, node, batch_id, nonce)
        progress.mark_started(batch_id, [unit_id(u) for u in subset], idem)
        await ctx.daos.artifact.write_progress(artifact.id, progress.dump())
        await _emit_progress(ctx, node, batch_id, done=False, total=n)

        try:
            # WP-31：批内 worker 的条目产生即定性为 WRITE/batch_id
            async with scope(phase=Phase.WRITE, batch_id=batch_id):
                batch_results = await worker(ctx, batch_id, subset)
        except Exception:
            progress.mark_failed(batch_id)
            await ctx.daos.artifact.write_progress(artifact.id, progress.dump())
            raise

        progress.mark_done(batch_id, [result_id(r) for r in batch_results])
        await ctx.daos.artifact.write_progress(artifact.id, progress.dump())
        await _emit_progress(ctx, node, batch_id, done=True, total=n)
        all_results.extend(batch_results)

        # WP-31：批次成功收口——evict 本批 BATCH/ITEM 条目（batch_closed）。
        # ctx.context_store 为 None（旧夹具/开关关闭）时整体跳过。
        if ctx.context_store is not None:
            await ctx.context_store.close_batch(batch_id)

    cursor = dict(cursor)
    cursor["next_index"] = n
    return all_results, cursor


async def _emit_progress(
    ctx: "TaskContext", node: str, batch_id: str, *, done: bool, total: int
) -> None:
    emit = getattr(ctx, "emit", None)
    if emit is None:
        return
    await emit(
        "batch_progress",
        {"node": node, "batch_id": batch_id, "done": done, "total": total},
    )
