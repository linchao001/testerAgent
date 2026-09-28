"""retrieve_pipeline 检索管线编排（WP-12，dd §8.2 §8.3 §8.4 §8.5 §8.6）。

节点内唯一入口（dd §8.2）：按序消费六算子，每次管线构造时读 caps 经
``plan_fallbacks`` 做降级决策（dd §8.4），``RetrievalCache.start_run`` 建立
run 分区（dd §8.5）；管线结束一次性 ``TraceDAO.append``，并按
snapshot_level（off/meta/full）写 context_snapshot（dd §8.3）。trace 行的
referenced/hallucinated/weak 在节点 LLM 生成结束后由
:func:`close_retrieval_trace` 两写回填（dd §8.6 引用闭环）。

与 dd 冻结签名的偏离（WP-12 交接单登记，均不碰 §2.8 冻结字段）：
- ``retrieve_pipeline`` 增 keyword-only ``scope``（§8.2 meta_filter 必需的
  "当前确认 link/story ID 集合"，冻结签名未给通道）与 ``stage_version``
  （trace/snapshot 行必需，默认 1）；
- trace 行 ``query_variant`` 存 ``{"queries": [各路文本]}``（§8.3"存各路
  文本"，TraceRow helper 的单 QueryVariant 形态容纳不下多路）；
- snapshot 的 ``ordering_strategy`` 按 §8.5 明确许可"直接并入 latencies JSON"；
- ``prompt_template_ver`` 在 WP-14 prompts 包落地前以 ``inline-v1`` 占位
  （multi_query/rerank 提示词当前内联）；
- 快照 DB items 一律剥离 passage（dd §8.3"meta 不落正文"；full 正文仅在
  JSONL，经 char_offset/byte_length 定位）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence

from pydantic import BaseModel

from ...adapters.reme import plan_fallbacks
from ...domain import Candidate, RetrievalConfig
from ...errors import ValidationError
from ...runtime.context import TaskContext
from ...store.models import (
    SnapshotDAO,
    SnapshotRow,
    TraceDAO,
    TraceRow,
    WorkspaceDAO,
)
from .ops import (
    RetrievalTraceBuilder,
    meta_filter,
    multi_query,
    parallel_recall,
)
from .ops_b import (
    RetrievalOutcome,
    assemble,
    passage_extract,
    rerank,
)

logger = logging.getLogger(__name__)

SNAPSHOT_LEVELS = ("off", "meta", "full")

# WP-14 起：multi_query/rerank 提示词已迁至 prompts 包，版本号从模板文件头读取
from ...prompts import prompt_version

INLINE_PROMPT_VER = prompt_version("retrieve/multi_query")

# dd §8.6 weak_reference 阈值（字符 bigram Jaccard）
WEAK_REF_THRESHOLD = 0.08

# dd §8.6 引用标签正则 [ID:xxx]
_ID_TAG_RE = re.compile(r"\[ID:\s*([^\[\]\s]+)\s*\]")


# ---------- retrieve_pipeline（dd §8.2：节点内唯一入口） ----------


async def retrieve_pipeline(
    ctx: TaskContext,
    cfg: RetrievalConfig,
    intent: str,
    *,
    batch_id: str | None = None,
    scope: dict | None = None,
    stage_version: int = 1,
    persist: bool = True,
) -> RetrievalOutcome:
    """产出注入块 + 留痕（trace/snapshot）。一行 = 一个批次一次管线调用。

    ``persist=False``（WP-28 playground，dd §8.7）：不落任何业务表——
    跳过 trace 落库（playground 的临时 task_id 不在 task 表，落库会触发
    外键违例），snapshot 由调用方强制 off 天然不写；最终 candidates 挂在
    ``outcome.candidates`` 上随 HTTP 响应直接返回，trace_id 保持空串。
    """
    level = _validate_level(ctx.snapshot_level)
    task = ctx.task
    sink = RetrievalTraceBuilder()

    # ① run 分区：run_id 变化清空该 task 缓存，同 run 批次间保留（dd §8.5）
    ctx.app.retrieval_cache.start_run(task.id, ctx.run_id)

    # ② caps 降级决策（dd §8.4；镜像 best-effort 刷新，空/旧树均不抛）
    mirror = ctx.mirror
    await mirror.ensure_fresh()
    plan = plan_fallbacks(ctx.reader.caps, mirror_empty=mirror.is_empty)
    sink.degraded.extend(plan.degraded)

    types = [t.value for t in cfg.allowed_types]

    # ③ multi_query（raw 恒在；LLM 失败仅 raw 并已由算子记 degraded）
    queries = await multi_query(ctx.app.llm, intent, cfg.query_paths, sink=sink)

    # ④ parallel_recall：能力可用时服务端 types/scope；否则 scope 不透传，
    #    交 meta_filter 镜像路径（dd §8.4）。recall 缓存按路读写。
    recall_scope = scope if ctx.reader.caps.metadata_filter else None
    kb_id = await _resolve_kb_id(ctx, task.workspace_id)
    cands = await parallel_recall(
        ctx.reader,
        queries,
        cfg.recall_topk,
        types,
        scope=recall_scope,
        sink=sink,
        cache=ctx.app.retrieval_cache,
        task_id=task.id,
        workspace_id=task.workspace_id,
        kb_id=kb_id,
    )

    # ⑤ meta_filter：类型过滤恒在；归属过滤仅在"能力可用（已服务端过滤，
    #    此侧为空操作）"或 use_mirror_filter（镜像非空本地过滤）时带 scope；
    #    skip_meta_filter（镜像空）→ 不做归属裁决，全量进 rerank。
    filter_scope = scope if plan.use_mirror_filter else None
    filter_mirror = mirror if plan.use_mirror_filter else None
    cands = await meta_filter(
        cands,
        types=types,
        scope=filter_scope,
        mirror=filter_mirror,
        sink=sink,
    )

    # ⑥ rerank（>30 分桶；失败回规则分；超出 inject_limit 记 rerank_cutoff）
    cands = await rerank(
        ctx.app.llm,
        cands,
        intent,
        cfg.inject_limit,
        sink=sink,
        cache=ctx.app.retrieval_cache,
        task_id=task.id,
    )

    # ⑦ passage_extract：index_line 的 summary 仅在镜像非空时取镜像，缺省退化标题；
    #    passage 走 get_entry + 本地结构化切分（get_entry 失败单候选标记不阻他路）。
    extract_mirror = mirror if not mirror.is_empty else None
    items = await passage_extract(
        cands,
        ctx.reader,
        cfg.inject_form,
        intent=intent,
        mirror=extract_mirror,
        sink=sink,
        cache=ctx.app.retrieval_cache,
        task_id=task.id,
    )

    # ⑧ assemble：去重 → 预算/硬上限截断 → anchor-v1 锚点排序
    outcome = await assemble(cands, items, cfg, sink=sink)

    # ⑨ trace 第一次写：append 拿 id（dd §8.3；candidates 为最终带 kept/drop_reason 全集）
    #    persist=False（playground）时不落库，candidates 挂 outcome 随响应返回（dd §8.7）。
    if not persist:
        outcome.candidates = list(cands)
        return outcome
    trace_id = uuid.uuid4().hex
    trace_row = TraceRow.create(
        id=trace_id,
        task_id=task.id,
        graph_run_id=task.graph_run_id or ctx.run_id,
        stage=cfg.stage,
        stage_version=stage_version,
        node=cfg.stage,
        query_variant={
            "queries": [{"channel": q.channel, "text": q.text} for q in queries]
        },
        candidates=cands,
        injected_ids=outcome.injected,
        batch_id=batch_id,
        degraded=outcome.degraded,
    )
    await TraceDAO(ctx.app.db).append(trace_row)
    outcome.trace_id = trace_id

    # ⑩ snapshot：assemble 后写（dd §8.3；off 不写，meta 只 DB，full 先文件后 DB）
    snapshot_id = await _write_snapshot(
        level,
        ctx=ctx,
        cfg=cfg,
        outcome=outcome,
        sink=sink,
        stage_version=stage_version,
        batch_id=batch_id,
    )
    outcome.snapshot_id = snapshot_id
    return outcome


def _validate_level(level: str) -> str:
    if level not in SNAPSHOT_LEVELS:
        raise ValidationError(
            "snapshot_level 非法",
            details={"got": level, "allowed": list(SNAPSHOT_LEVELS)},
        )
    return level


async def _resolve_kb_id(ctx: TaskContext, workspace_id: str) -> str:
    """recall_key 的 kb_id 段（dd §8.5）：读工作区 kb_config，缺省为空串。

    fkey 已按 task_id 分区，kb_id 仅为 dd 公式的组成部分；缺失不影响隔离正确性。
    """
    ws = await WorkspaceDAO(ctx.app.db).get(workspace_id)
    return str(ws.kb_config_obj().get("kb_id", "") or "")


# ---------- snapshot 三档（dd §8.3） ----------


async def _write_snapshot(
    level: str,
    *,
    ctx: TaskContext,
    cfg: RetrievalConfig,
    outcome: RetrievalOutcome,
    sink: RetrievalTraceBuilder,
    stage_version: int,
    batch_id: str | None,
) -> str | None:
    if level == "off":
        return None
    task = ctx.task
    snapshot_path: str | None = None

    if level == "full":
        # 先逐行 append 得 offset/length 回填 items，再 put（dd §8.3）。
        # 文件短操作集中到一个线程调用，避免 fsync 阻塞事件循环（dd §1.2）。
        def _full_write() -> tuple[str, str]:
            writer = ctx.files.open_snapshot_writer(
                task.workspace_id,
                task.id,
                cfg.stage,
                stage_version,
                cfg.stage,
                batch_id,
            )
            try:
                for item in outcome.items:  # position 序
                    offset, length = writer.append(item.model_dump())
                    item.char_offset = offset
                    item.byte_length = length
                return writer.snapshot_id, writer.rel_path
            finally:
                writer.close()

        snapshot_id, snapshot_path = await asyncio.to_thread(_full_write)
    else:
        snapshot_id = uuid.uuid4().hex

    # DB items 恒剥离 passage（meta/full 正文均不落 DB）；full 的 offset/length 保留
    db_items = [it.model_copy(update={"passage": None}) for it in outcome.items]

    # ordering_strategy 按 §8.5 许可并入 latencies JSON
    snap_latencies: dict = dict(sink.latencies)
    if sink.ordering_strategy:
        snap_latencies["ordering_strategy"] = sink.ordering_strategy

    row = SnapshotRow.create(
        id=snapshot_id,
        task_id=task.id,
        graph_run_id=task.graph_run_id or ctx.run_id,
        stage=cfg.stage,
        stage_version=stage_version,
        node=cfg.stage,
        prompt_template_ver=INLINE_PROMPT_VER,
        items=db_items,
        snapshot_path=snapshot_path,
        total_tokens_est=outcome.token_est,
        budget=cfg.token_budget,
        truncated=outcome.truncated,
        usage={"aux": {"retrieval": dict(sink.aux_usage)}},
        latencies=snap_latencies,
        batch_id=batch_id,
    )
    await SnapshotDAO(ctx.app.db).put(row)
    return snapshot_id


# ---------- §8.6 引用闭环校验（产物侧，纯函数） ----------


def parse_ids(text: str) -> set[str]:
    """解析产物中的引用标签 ``[ID:xxx]``。"""
    return set(_ID_TAG_RE.findall(text or ""))


class ClosedLoop(BaseModel):
    """引用闭环结果（dd §8.6）。weak_refs 仅标记，不改变主口径。"""

    referenced: list[str]
    injected_not_used: list[str]
    hallucinated: list[str]
    weak_refs: list[str]


def close_loop(
    generated_texts: Sequence[str],
    white_list: set[str],
    *,
    passages: dict[str, str] | None = None,
) -> ClosedLoop:
    """产物引用 vs 注入白名单（dd §8.6）。

    - hallucinated = 产物 [ID:xxx] 中不在白名单者（warning，不计 referenced）；
    - referenced  = 产物引用与白名单交集；injected_not_used = 白名单 − referenced；
    - weak_reference：对每个 referenced，其注入 passage 与对应产物文本的字符
      bigram Jaccard < 0.08（passages 缺该条则无法判定，不标 weak）。
    """
    white = set(white_list)
    found: set[str] = set()
    for text in generated_texts:
        found |= parse_ids(text)

    referenced = found & white
    weak: set[str] = set()
    if passages:
        for text in generated_texts:
            for eid in parse_ids(text) & referenced:
                passage = passages.get(eid)
                if passage and _bigram_jaccard(passage, text) < WEAK_REF_THRESHOLD:
                    weak.add(eid)

    return ClosedLoop(
        referenced=sorted(referenced),
        injected_not_used=sorted(white - referenced),
        hallucinated=sorted(found - white),
        weak_refs=sorted(weak),
    )


def _char_bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text or "")
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def _bigram_jaccard(a: str, b: str) -> float:
    bigrams_a, bigrams_b = _char_bigrams(a), _char_bigrams(b)
    if not bigrams_a or not bigrams_b:
        return 0.0
    return len(bigrams_a & bigrams_b) / len(bigrams_a | bigrams_b)


async def close_retrieval_trace(
    ctx: TaskContext,
    outcome: RetrievalOutcome,
    generated_texts: Sequence[str],
) -> ClosedLoop:
    """节点生成结束后：close_loop + TraceDAO.update_referenced 两写回填（dd §8.3 §8.6）。"""
    passages = {
        it.entry_id: (it.passage or it.title) for it in outcome.items
    }
    result = close_loop(
        generated_texts, set(outcome.injected), passages=passages
    )
    await TraceDAO(ctx.app.db).update_referenced(
        outcome.trace_id,
        result.referenced,
        result.weak_refs,
        result.hallucinated,
    )
    return result


# ---------- 漏斗计数（调试面板/eval/playground 共用，dd §8.6 归因口径） ----------


def funnel_counts(cands: Sequence[Candidate]) -> dict:
    """召回全集计数：kept / 路径 error / 被裁（按 drop_reason 分组），三项和=total。"""
    kept = 0
    errors = 0
    dropped: dict[str, int] = {}
    for c in cands:
        if c.kept:
            kept += 1
        elif c.error:
            errors += 1  # recall 路径级 error 行（非条目，不进 drop 分组）
        else:
            reason = c.drop_reason or "unknown"
            dropped[reason] = dropped.get(reason, 0) + 1
    return {
        "total": len(cands),
        "kept": kept,
        "errors": errors,
        "dropped": dropped,
    }
