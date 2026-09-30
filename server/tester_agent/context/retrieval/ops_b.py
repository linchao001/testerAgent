"""检索算子 B（WP-11，dd §8.2 §8.5 §7.4④）：rerank / passage_extract / assemble。

与 dd 伪码签名的偏差（不碰 §2.8 冻结字段，交接单登记）：
- 三算子沿 WP-10 例增 keyword-only ``sink``；``rerank``/``passage_extract``
  另增 ``cache``/``task_id``——§8.5 run 内复用缓存的接入口（cache 与 task_id
  必须同给，分区由管线 :meth:`RetrievalCache.start_run` 建立）；
- ``passage_extract`` 增 ``intent``（dd 签名未带，但"选与意图重叠最高的 1~2
  段"必须按意图算重叠分）与 ``mirror``（index_line 的 summary 来源：镜像
  命中取 IndexEntryMeta.summary，不拉正文；镜像缺省/未收录退化为仅标题）；
- ``assemble`` 增 ``cands``——budget_cut/dedup 需回写 Candidate.drop_reason，
  且"按 score 排序/同分位置"需要分数（InjectedItem 无 score 字段）；
- 规则分的"×类型权重"dd 未给数值 → v1 恒 1.0，留 ``type_weights`` 注入点
  待 S5 标定；
- rerank 输出 schema 落 ``{"scores": [...]}`` 对象根（§7.4④ 文字为裸数组，
  但 §9.3 JSON 模式 = DeepSeek json_object 强制对象根，与 multi_query 同例）；
- passage_extract 每候选产出恰好 1 个 InjectedItem（1~2 段按原文顺序拼接）；
  assemble 的同 (entry_id,version) 去重为防御性（recall 已按并集去重）；
- passage_api 能力位在算子层无消费点（§9.1 Protocol 无段落级 API），passage
  形态恒走 get_entry 拉正文 + 本地结构化切分；get_entry 失败仅标记该候选
  （kept=False + error），不阻他候选，不记 degraded；
- assemble 的去重前置于截断（常态"每候选单 item"下与 dd 文字序等价，防御
  分支下避免重复条目白占预算）；截断为尾部截断（按 score 排序后首个超预算/
  超限条目及其余全部裁掉），不做装箱式跳项；
- extract 阶段 InjectedItem.position/tokens_est 为占位（0），由 assemble 在
  锚点排序后统一重排 position、按 §8.2 口径统一估算 tokens。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping

from pydantic import BaseModel

from ...adapters.reme import IndexMirror, ReMeReader
from ...domain import Candidate, EntryType, InjectedItem, RetrievalConfig
from ...errors import LLMBadOutput, ValidationError
from .cache import RetrievalCache, entry_key
from .ops import RECALL_CONCURRENCY, RetrievalTraceBuilder, lexical_score

logger = logging.getLogger(__name__)

# ---------- 常量（dd §7.4④ / §8.2 / §8.5；S5 标定后可调整） ----------

RERANK_LLM_THRESHOLD = 30  # 候选超过 30 条才按通道分桶
RERANK_BUCKET_SIZE = 10  # 每桶最多送 10 条（桶内按召回分）
PASSAGE_WINDOW_CHARS = 800  # 无结构信息时的固定窗口（前 800 字）
PASSAGE_MAX_SEGMENTS = 2  # 选与意图重叠最高的 1~2 段
ORDERING_STRATEGY = "anchor-v1"  # §8.5 锚点排序策略标识（WP-12 记入 snapshot）

_STEP_RERANK = "rerank"
_STEP_EXTRACT = "passage_extract"
_STEP_ASSEMBLE = "assemble"


class RetrievalOutcome(BaseModel):
    """管线产出（dd §8.2）。degraded/latencies 由 assemble 从 sink 快照；
    管线（WP-12）可在后续节点继续补充。

    WP-12 增 ``trace_id``/``snapshot_id``（dd §8.3 trace"先 append 拿 id、
    结束 update 回填"的两写句柄；snapshot_id 供节点侧/面板对账，off 档为 None）。
    """

    items: list[InjectedItem]  # 已按锚点排序、已截断
    injected: list[str]  # entry_id 顺序
    degraded: list = []  # list[DegradedStep]，免循环 import 用裸 list
    latencies: dict[str, int] = {}
    token_est: int
    truncated: bool
    trace_id: str = ""
    snapshot_id: str | None = None
    # WP-28 playground（dd §8.7，persist=False）：最终候选全集（含 kept/
    # drop_reason）随响应回显；节点路径（persist=True）恒为空，避免冗余驻留。
    candidates: list = []


# ---------- token 估算（dd §8.2：中英混合） ----------
# 实现在同层 context/tokens.py；此处 re-export 供检索算子与测试沿用旧符号名。

from ..tokens import estimate_tokens  # noqa: E402,F401


# ---------- rerank（dd §8.2：>30 分桶；LLM 失败 → 规则分） ----------

_RERANK_SCHEMA = {
    "type": "object",
    "required": ["scores"],
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["entry_id", "score"],
                "properties": {
                    "entry_id": {"type": "string"},
                    "score": {"type": "number"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}



# 规则分类型权重（dd "×类型权重" 未给数值 → v1 恒 1.0，S5 标定后经参数覆盖）
_DEFAULT_TYPE_WEIGHTS: dict[EntryType, float] = {t: 1.0 for t in EntryType}


class _BadRerankOutput(Exception):
    """LLM 打分内容解析不出任何可用分数。"""


def _primary_channel(cand: Candidate) -> str:
    """候选的主召回通道（source_channel 聚合串的首个 tag 的通道段）。"""
    first = (cand.source_channel or "").split(",")[0]
    return first.split(":")[0] if first else ""


def _parse_rerank_scores(content: str, known_ids: set[str]) -> dict[str, float]:
    """解析 LLM 打分为 {entry_id: score}；非法/未知条目剔除，全不可用 → 抛错。"""
    try:
        obj = json.loads(content)
    except Exception as e:
        raise _BadRerankOutput(f"JSON 解析失败: {e}") from e
    if not isinstance(obj, dict) or not isinstance(obj.get("scores"), list):
        raise _BadRerankOutput("输出不是含 scores 数组的 JSON 对象")
    out: dict[str, float] = {}
    for item in obj["scores"]:
        if not isinstance(item, dict):
            continue
        eid, score = item.get("entry_id"), item.get("score")
        if not isinstance(eid, str) or eid not in known_ids:
            continue
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            continue
        out[eid] = float(score)
    if not out:
        raise _BadRerankOutput("未解析出任何可用打分")
    return out


async def rerank(
    llm,
    cands: list[Candidate],
    intent: str,
    limit: int,
    *,
    sink: RetrievalTraceBuilder | None = None,
    cache: RetrievalCache | None = None,
    task_id: str | None = None,
    type_weights: Mapping[EntryType, float] | None = None,
) -> list[Candidate]:
    """LLM 批量打分重排；失败/不可用路回退规则分（标题/意图重叠 × 类型权重）。

    - 仅处理 kept=True 候选；kept=False（如 recall error 行）原样透传不裁决；
    - kept 数 > ``RERANK_LLM_THRESHOLD`` 时按主通道分桶，每桶按召回分取前
      ``RERANK_BUCKET_SIZE`` 条送 LLM（一桶一次调用）；桶外候选保留规则分；
    - LLM 调用失败或整桶解析不出 → 该桶保留规则分并记 degraded
      （reason=llm_failed / llm_bad_output，fallback=rule_score）；输出未覆盖
      的候选保留规则分（部分成功不记 degraded，口径同 multi_query）；
    - cache 非空时按 ``entry_id|entry_version|intent_hash`` 命中跳过 LLM
      （命中也计 score；usage 只统计实际发起的调用）；
    - 最终按新分数降序（同分 entry_id 升序），超出 ``limit`` 的 kept 候选
      kept=False + drop_reason='rerank_cutoff'；
    - intent 为空白时跳过 LLM（规则分全 0，cutoff 按 entry_id 确定性裁决）。
    """
    sink = sink or RetrievalTraceBuilder()
    if cache is not None and not task_id:
        raise ValidationError("rerank 使用 cache 必须同时提供 task_id")
    weights = type_weights or _DEFAULT_TYPE_WEIGHTS

    def _rule_score(c: Candidate) -> float:
        return lexical_score(intent, c.title, "") * weights.get(c.entry_type, 1.0)

    t0 = time.perf_counter()
    kept_rows = [c for c in cands if c.kept]
    recall_scores = {id(c): c.score for c in kept_rows}  # 分桶选择用召回分快照

    # ① 规则分打底 + 缓存命中收集
    llm_pending: list[Candidate] = []
    for c in kept_rows:
        cached = (
            cache.get(task_id, entry_key("rerank", c.entry_id, c.entry_version, intent))
            if cache is not None
            else None
        )
        if isinstance(cached, (int, float)) and not isinstance(cached, bool):
            c.score = float(cached)
        else:
            c.score = _rule_score(c)
            llm_pending.append(c)

    # ② LLM 打分（≤阈值单批；>阈值按主通道分桶，每桶取召回分前 N）
    if llm_pending and intent.strip():
        if len(kept_rows) > RERANK_LLM_THRESHOLD:
            buckets: dict[str, list[Candidate]] = {}
            for c in kept_rows:
                buckets.setdefault(_primary_channel(c), []).append(c)
            pending_ids = {id(c) for c in llm_pending}
            units = [
                [c for c in unit if id(c) in pending_ids]
                for unit in (
                    sorted(g, key=lambda c: (-recall_scores[id(c)], c.entry_id))[
                        :RERANK_BUCKET_SIZE
                    ]
                    for g in buckets.values()
                )
            ]
            units = [u for u in units if u]
        else:
            units = [llm_pending]
        known_ids = {c.entry_id for c in llm_pending}
        for unit in units:
            await _rerank_one_unit(
                llm, unit, intent, known_ids,
                sink=sink, cache=cache, task_id=task_id,
            )

    # ③ 截断：按新分数降序，超出 limit 记 rerank_cutoff
    kept_rows.sort(key=lambda c: (-c.score, c.entry_id))
    for rank, c in enumerate(kept_rows):
        if rank >= limit:
            c.kept = False
            c.drop_reason = "rerank_cutoff"

    kept_obj_ids = {id(c) for c in kept_rows}
    dropped = [c for c in cands if not c.kept and id(c) not in kept_obj_ids]
    rows = kept_rows + dropped
    sink.record(
        _STEP_RERANK, latency_ms=int((time.perf_counter() - t0) * 1000), candidates=rows
    )
    return rows


async def _rerank_one_unit(
    llm,
    unit: list[Candidate],
    intent: str,
    known_ids: set[str],
    *,
    sink: RetrievalTraceBuilder,
    cache: RetrievalCache | None,
    task_id: str | None,
) -> None:
    """单批（单桶）LLM 打分；失败保留规则分并记 degraded，不抛。"""
    from ...prompts import load_prompt

    tpl = load_prompt("retrieve/rerank")
    parts = tpl.content.split("\n\n检索意图：")
    system_content = parts[0].strip()
    user_content = parts[1].format(intent=intent, entries=json.dumps(
        [
            {"entry_id": c.entry_id, "type": c.entry_type.value, "title": c.title}
            for c in unit
        ],
        ensure_ascii=False,
    ))
    usage: Mapping[str, int] = {}
    try:
        result = await llm.chat(
            [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            json_schema=_RERANK_SCHEMA,
        )
        usage = result.usage or {}
        scores = _parse_rerank_scores(result.content, known_ids)
    except Exception as e:  # noqa: BLE001 —— 降级边界：任何 LLM 失败都回规则分
        reason = (
            "llm_bad_output"
            if isinstance(e, (_BadRerankOutput, LLMBadOutput))
            else "llm_failed"
        )
        sink.add_degraded(_STEP_RERANK, reason, "rule_score")
        logger.warning(
            "rerank fallback to rule score: %s",
            e.__class__.__name__,
            extra={"step": _STEP_RERANK, "reason": reason},
        )
    else:
        for c in unit:
            if c.entry_id in scores:
                c.score = scores[c.entry_id]
                if cache is not None:
                    cache.put(
                        task_id,  # type: ignore[arg-type]
                        entry_key("rerank", c.entry_id, c.entry_version, intent),
                        c.score,
                    )
    sink.add_aux_usage(usage)


# ---------- passage_extract（dd §8.2：结构化切分为主） ----------

_ATX_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_PARA_SPLIT = re.compile(r"\n\s*\n")


def _split_segments(content: str) -> list[str]:
    """结构化切分：优先按 ATX 标题切段（标题计入段首），不足两段再按空行分段。

    返回 strip 后非空段列表；长度为 1 表示"无结构信息"（调用方走固定窗口）。
    """
    segments: list[str] = []
    current: list[str] = []
    for line in content.splitlines():
        if _ATX_HEADING.match(line) and any(x.strip() for x in current):
            segments.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    segments.append("\n".join(current).strip())
    segments = [s for s in segments if s]
    if len(segments) >= 2:
        return segments
    return [p for p in (seg.strip() for seg in _PARA_SPLIT.split(content)) if p]


def _select_passage(content: str, intent: str) -> str:
    """选与意图重叠最高的 1~2 段（按原文顺序拼接）；无结构 → 前 800 字窗口。"""
    segments = _split_segments(content)
    if len(segments) <= 1:
        return content[:PASSAGE_WINDOW_CHARS]
    scored = sorted(
        enumerate(segments), key=lambda p: (-lexical_score(intent, "", p[1]), p[0])
    )
    picked = {scored[0][0]}
    if len(scored) > 1 and PASSAGE_MAX_SEGMENTS >= 2:
        second_idx, second = scored[1]
        if lexical_score(intent, "", second) > 0:
            picked.add(second_idx)
    return "\n\n".join(segments[i] for i in sorted(picked))


async def passage_extract(
    cands: list[Candidate],
    reader: ReMeReader,
    form: str,
    *,
    intent: str = "",
    mirror: IndexMirror | None = None,
    sink: RetrievalTraceBuilder | None = None,
    cache: RetrievalCache | None = None,
    task_id: str | None = None,
) -> list[InjectedItem]:
    """kept 候选 → 注入项。index_line 只取 title+summary（镜像取摘要，不拉正文）；
    passage 走 get_entry 拉正文 + 本地结构化切分（并发信号量同 recall）。

    - kept=False 候选不参与产出；get_entry 失败的候选 kept=False + error 标记，
      不产出注入项，不阻他候选；
    - cache 仅作用于 passage 形态（index_line 无 I/O，命中开销大于收益）；
    - 产出顺序 = 输入候选顺序；position/tokens_est 为占位，assemble 统一重排。
    """
    sink = sink or RetrievalTraceBuilder()
    if cache is not None and not task_id:
        raise ValidationError("passage_extract 使用 cache 必须同时提供 task_id")
    if form not in ("index_line", "passage"):
        raise ValidationError("inject_form 非法", details={"form": form})

    t0 = time.perf_counter()
    kept_rows = [c for c in cands if c.kept]
    items: list[InjectedItem] = []

    if form == "index_line":
        if mirror is not None:
            await mirror.ensure_fresh()  # best-effort，TTL 内零开销
        for c in kept_rows:
            summary = ""
            if mirror is not None:
                meta = mirror.meta(c.entry_id)
                summary = meta.summary if meta is not None else ""
            passage = f"{c.title}\n{summary}" if summary else c.title
            items.append(
                InjectedItem(
                    entry_id=c.entry_id,
                    entry_version=c.entry_version,
                    title=c.title,
                    tokens_est=0,
                    position=len(items),
                    passage=passage,
                )
            )
    else:
        passages = await _fetch_passages(
            kept_rows, reader, intent=intent, cache=cache, task_id=task_id
        )
        for c, passage in zip(kept_rows, passages):
            if passage is None:  # get_entry 失败，已在 _fetch_passages 内标记
                continue
            items.append(
                InjectedItem(
                    entry_id=c.entry_id,
                    entry_version=c.entry_version,
                    title=c.title,
                    tokens_est=0,
                    position=len(items),
                    passage=passage,
                )
            )

    sink.record(
        _STEP_EXTRACT, latency_ms=int((time.perf_counter() - t0) * 1000), candidates=cands
    )
    return items


async def _fetch_passages(
    kept_rows: list[Candidate],
    reader: ReMeReader,
    *,
    intent: str,
    cache: RetrievalCache | None,
    task_id: str | None,
) -> list[str | None]:
    """并发拉正文并本地切段；单候选失败置 None 并标记 error，不阻他路。"""
    sem = asyncio.Semaphore(RECALL_CONCURRENCY)

    async def _one(c: Candidate) -> str | None:
        cache_k = (
            entry_key("extract", c.entry_id, c.entry_version, intent)
            if cache is not None
            else None
        )
        if cache is not None and cache_k is not None:
            cached = cache.get(task_id, cache_k)  # type: ignore[arg-type]
            if isinstance(cached, str):
                return cached
        try:
            async with sem:
                entry = await reader.get_entry(c.entry_id)
        except Exception as e:  # noqa: BLE001 —— 单候选容错；CancelledError 不拦截
            c.kept = False
            c.error = str(e) or e.__class__.__name__
            return None
        passage = _select_passage(entry.content, intent)
        if cache is not None and cache_k is not None:
            cache.put(task_id, cache_k, passage)  # type: ignore[arg-type]
        return passage

    return list(await asyncio.gather(*(_one(c) for c in kept_rows)))


# ---------- assemble（dd §8.2/§8.5：预算 + 硬上限 + 锚点排序 + 去重） ----------


def _anchor_order(items: list[InjectedItem]) -> list[InjectedItem]:
    """anchor-v1：第 1 名首位、第 2 名末位、第 3 名次位、第 4 名次末位……
    其余按序填中间（输入须已按 score 降序）。同一条目不重复（调用方已去重）。"""
    n = len(items)
    out: list[InjectedItem | None] = [None] * n
    front, back = 0, n - 1
    for rank, item in enumerate(items):
        if rank % 2 == 0:
            out[front] = item
            front += 1
        else:
            out[back] = item
            back -= 1
    return [it for it in out if it is not None]


async def assemble(
    cands: list[Candidate],
    items: list[InjectedItem],
    cfg: RetrievalConfig,
    *,
    sink: RetrievalTraceBuilder | None = None,
) -> RetrievalOutcome:
    """token 估算 → 去重 → 截断（inject_limit / token_budget）→ 锚点排序。

    - token 估算口径集中于本算子（dd §8.2），覆盖 extract 阶段的占位值；
    - 去重：同 (entry_id, entry_version) 只留最高分位置（常态每候选单 item，
      为防御性分支），被去重条目对应候选 kept=False + drop_reason='dedup'；
    - 截断：按 score 降序（同分 entry_id 升序）尾部截断——首个超
      ``cfg.inject_limit`` 或累计超 ``cfg.token_budget`` 的条目及其余全部
      kept=False + drop_reason='budget_cut'；
    - 锚点排序策略标识 ``anchor-v1`` 经 sink.ordering_strategy 暴露给 WP-12
      写 snapshot；outcome.degraded/latencies 取自 sink 快照（管线全流程）。
    """
    sink = sink or RetrievalTraceBuilder()
    t0 = time.perf_counter()

    by_key: dict[tuple[str, str], Candidate] = {}
    key_group: dict[tuple[str, str], list[Candidate]] = {}
    for c in cands:
        key = (c.entry_id, c.entry_version)
        by_key.setdefault(key, c)
        key_group.setdefault(key, []).append(c)

    def _score_of(item: InjectedItem) -> float:
        cand = by_key.get((item.entry_id, item.entry_version))
        return cand.score if cand is not None else 0.0

    for item in items:  # 口径集中：tokens 以 assemble 为准
        item.tokens_est = estimate_tokens(item.passage or item.title)

    # ① 去重（同 entry_id+version 只留最高分位置；同分取先出现者）
    deduped: list[InjectedItem] = []
    seen: set[tuple[str, str]] = set()
    for _, it in sorted(
        enumerate(items), key=lambda p: (-_score_of(p[1]), p[0])
    ):
        key = (it.entry_id, it.entry_version)
        if key in seen:
            # 精确归因：同键存在多个候选时标非最高分的那个；单候选 + 孤儿
            # 重复 item（无对应候选）不误标。
            group = key_group.get(key, [])
            if len(group) > 1:
                survivor = max(group, key=lambda c: c.score)
                for c in group:
                    if c is not survivor and c.kept:
                        c.kept = False
                        c.drop_reason = "dedup"
            continue
        seen.add(key)
        deduped.append(it)

    # ② 尾部截断（inject_limit 硬上限 + token_budget 软预算）
    ranked = sorted(deduped, key=lambda it: (-_score_of(it), it.entry_id))
    kept_items: list[InjectedItem] = []
    total = 0
    truncated = False
    for it in ranked:
        over_limit = len(kept_items) >= cfg.inject_limit
        over_budget = total + it.tokens_est > cfg.token_budget
        if over_limit or over_budget:
            truncated = True
            cand = by_key.get((it.entry_id, it.entry_version))
            if cand is not None and cand.kept:
                cand.kept = False
                cand.drop_reason = "budget_cut"
            continue
        kept_items.append(it)
        total += it.tokens_est

    # ③ 锚点排序 + position 重排
    ordered = _anchor_order(kept_items)
    for pos, it in enumerate(ordered):
        it.position = pos

    sink.ordering_strategy = ORDERING_STRATEGY
    sink.record(
        _STEP_ASSEMBLE, latency_ms=int((time.perf_counter() - t0) * 1000), candidates=cands
    )
    return RetrievalOutcome(
        items=ordered,
        injected=[it.entry_id for it in ordered],
        degraded=list(sink.degraded),
        latencies=dict(sink.latencies),
        token_est=total,
        truncated=truncated,
    )
