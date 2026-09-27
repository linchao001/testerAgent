"""检索算子 A（WP-10，dd §8.2 §8.4）：multi_query / parallel_recall / meta_filter。

六算子为函数式（便于单测与 eval 复用），只依赖接入层 Protocol（LLMClient /
ReMeReader）与 §2.8 领域对象，不触达 DB / 文件系统；WP-12 的 retrieve_pipeline
负责编排、caps 降级决策（plan_fallbacks）与 trace/snapshot 落库。

留痕（dd §8.3 "每算子结束写内存 trace builder"）：算子接受可选 ``sink``
（:class:`RetrievalTraceBuilder`），把本步延迟、degraded、辅助 LLM usage 与
输出候选全集写入；sink=None 时内部用一次性空收集器，保持可独立调用。

与 dd 伪码签名的偏差（不碰 §2.8 冻结字段，交接单已登记）：
- 算子增加 keyword-only ``sink`` —— §8.2 只写"记 degraded"未给通道，按 §8.3
  落为内存 trace builder 注入；
- ``parallel_recall`` 增加 ``scope`` —— §8.4 把 metadata_filter 定义为
  "search 是否支持 types/scope 参数"，能力可用时 scope 在 recall 阶段服务端
  过滤（Candidate 冻结字段不携带 link/story 归属，客户端无从二次判定）；
- ``meta_filter`` 增加 ``mirror`` —— §8.4 镜像降级路径；mirror 由管线方在
  metadata_filter=False 且镜像非空时传入（镜像空则不传＝跳过过滤全量进 rerank，
  该 degraded 由管线经 plan_fallbacks 记录）；类型过滤恒在本算子用
  ``Candidate.entry_type`` 完成（WP-08 交接单⑤的分工约定）；
- ``source_channel`` 并集口径：dd 文字"保留最高分与 source_channel 列表"而冻结
  字段为 str，落为各召回路 tag 的排序逗号拼接（``"raw:0,keyword:1"``）；
- recall 分数来源：search 返回的 Entry 不带分（§9.1 raw 为适配层私有，外部不
  消费），候选分由本模块 :func:`lexical_score` 本地词面重叠分计算（标题 3 倍
  权重，与 FakeReMeReader 打分口径一致）；该函数同时是 WP-11 rerank 规则分
  （"标题/意图 token 重叠 × 类型权重"）的基础；
- 召回失败路产出 error 候选（kept=False、error=异常文本、entry_type 以
  LINK_INDEX 占位——冻结模型必填，调试面板以 error 字段区分）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence

from ...adapters.reme import Entry, IndexMirror, ReMeReader, local_entry_version
from ...domain import Candidate, DegradedStep, EntryType, QueryVariant
from ...errors import LLMBadOutput, ValidationError
from .cache import RetrievalCache, recall_key

logger = logging.getLogger(__name__)

# recall 单路并发上限（对齐 §13.2 llm_concurrency 初值；S5 标定后可经管线注入覆盖）
RECALL_CONCURRENCY = 4

_CHANNEL_TAG_SEP = ","
_TITLE_WEIGHT = 3.0  # 标题命中权重（与 tests/fakes.FakeReMeReader 打分口径一致）

# ---------- 内存留痕收集器（dd §8.3；WP-12 管线持有并最终落 TraceDAO） ----------


class RetrievalTraceBuilder:
    """算子 → 管线 的内存留痕通道：延迟、degraded、辅助 usage、各步候选全集。

    WP-12 在此之上补充 referenced/hallucinated 回填与漏斗计数，最终一次性
    TraceDAO.append；快照三档也以 step_candidates 为数据源之一。
    """

    def __init__(self) -> None:
        self.latencies: dict[str, int] = {}
        self.degraded: list[DegradedStep] = []
        # §8.3 usage.aux.retrieval = {calls, prompt_tokens, completion_tokens}
        self.aux_usage: dict[str, int] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.step_candidates: dict[str, list[Candidate]] = {}
        # §8.5 注入排序策略标识（assemble 写入 "anchor-v1"；WP-12 记入 snapshot）
        self.ordering_strategy: str | None = None

    def record(
        self,
        step: str,
        *,
        latency_ms: int | None = None,
        candidates: Sequence[Candidate] | None = None,
    ) -> None:
        if latency_ms is not None:
            self.latencies[step] = latency_ms
        if candidates is not None:
            self.step_candidates[step] = list(candidates)

    def add_degraded(self, step: str, reason: str, fallback: str) -> None:
        self.degraded.append(DegradedStep(step=step, reason=reason, fallback=fallback))

    def add_aux_usage(self, usage: Mapping[str, int]) -> None:
        """累加一次管线辅助 LLM 调用的 usage（multi_query / rerank）。"""
        self.aux_usage["calls"] += 1
        for key in ("prompt_tokens", "completion_tokens"):
            val = usage.get(key)
            if isinstance(val, int):
                self.aux_usage[key] += val


# ---------- 本地词面打分（recall 候选分；WP-11 rerank 规则分复用） ----------

_ASCII_WORD = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> list[str]:
    """查询词元：ASCII 词（小写）+ CJK 二元组（单字 CJK 也保留）。"""
    text = text.lower()
    tokens = list(_ASCII_WORD.findall(text))
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    if len(cjk) >= 2:
        tokens += ["".join(pair) for pair in zip(cjk, cjk[1:])]
    elif cjk:
        tokens += cjk
    return tokens


def lexical_score(query_text: str, title: str, content: str) -> float:
    """词面重叠分：查询词元在标题命中 ×3、正文命中 ×1，求和。

    确定性、中英混合可用（ASCII 分词 + CJK bigram），无外部依赖；与
    tests/fakes.FakeReMeReader 的词面打分口径一致（其按空白分词，中文查询
    时本函数的 bigram 口径更合理，单测以受控词条断言相对次序）。
    """
    if not query_text:
        return 0.0
    score = 0.0
    title_l, content_l = title.lower(), content.lower()
    for token in _tokens(query_text):
        if not token:
            continue
        score += title_l.count(token) * _TITLE_WEIGHT
        score += content_l.count(token)
    return score


# ---------- multi_query（dd §8.2：raw 路恒在 index 0；LLM 失败 → 仅 raw，记 degraded） ----------

_MULTI_QUERY_SCHEMA = {"type": "object", "required": ["keyword_queries", "rewrite_queries"]}

_STEP_MULTI_QUERY = "multi_query"


class _BadVariants(Exception):
    """LLM 返回内容解析不出任何可用查询变体。"""


def _clean_str_list(val: object) -> list[str]:
    if not isinstance(val, list):
        return []
    return [item.strip() for item in val if isinstance(item, str) and item.strip()]


def _parse_variants(content: str, want: int, *, exclude: str = "") -> list[QueryVariant]:
    """解析 LLM 输出为 n-1 路 variant（keyword/rewrite 交错，keyword 优先）。

    非法/重复条目剔除（含与 ``exclude``——即 raw 路——重复的变体）；一个都
    解析不出 → _BadVariants（调用方记 degraded）；数量不足按实际数量返回
    （部分成功不算失败）。
    """
    try:
        obj = json.loads(content)
    except Exception as e:
        raise _BadVariants(f"JSON 解析失败: {e}") from e
    if not isinstance(obj, dict):
        raise _BadVariants(f"输出不是 JSON 对象（{type(obj).__name__}）")
    kws = _clean_str_list(obj.get("keyword_queries"))
    rws = _clean_str_list(obj.get("rewrite_queries"))
    variants: list[QueryVariant] = []
    seen = {exclude.casefold()} if exclude else set()
    for kw, rw in itertools.zip_longest(kws, rws):
        for channel, text in (("keyword", kw), ("rewrite", rw)):
            if text is None:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            variants.append(QueryVariant(channel=channel, text=text))
            if len(variants) >= want:
                return variants
    if not variants:
        raise _BadVariants("未解析出任何可用查询")
    return variants


async def multi_query(
    llm,
    intent: str,
    n: int,
    *,
    sink: RetrievalTraceBuilder | None = None,
) -> list[QueryVariant]:
    """生成 n 路 query 表述：raw 恒在 index 0，其余 n-1 路由 LLM 生成。

    LLM 调用失败或解析不出可用查询 → 仅返回 raw 路并记 degraded（fallback
    统一 ``raw_only``；reason 区分 llm_failed=上游/调用失败、
    llm_bad_output=内容不可用）；部分成功（解析出的路数不足 n-1）按实际
    数量采用。n < 2 或 intent 为空白时不发起 LLM 调用，直接返回 [raw]。
    """
    sink = sink or RetrievalTraceBuilder()
    variants = [QueryVariant(channel="raw", text=intent)]
    want = n - 1
    if want < 1 or not intent.strip():
        return variants

    from ...prompts import load_prompt

    tpl = load_prompt("retrieve/multi_query")
    system_content = tpl.content.split("\n\n检索意图：")[0].strip()
    user_content = tpl.content.split("\n\n检索意图：")[1].format(intent=intent, want=want)

    t0 = time.perf_counter()
    usage: Mapping[str, int] = {}
    try:
        result = await llm.chat(
            [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            json_schema=_MULTI_QUERY_SCHEMA,
        )
        usage = result.usage or {}
        generated = _parse_variants(result.content, want, exclude=intent)
    except Exception as e:  # noqa: BLE001 —— 降级边界：任何 LLM 失败都退回 raw 路
        bad_output = isinstance(e, (_BadVariants, LLMBadOutput))
        reason = "llm_bad_output" if bad_output else "llm_failed"
        sink.add_degraded(_STEP_MULTI_QUERY, reason, "raw_only")
        logger.warning(
            "multi_query fallback to raw only: %s",
            e.__class__.__name__,
            extra={"step": _STEP_MULTI_QUERY, "reason": reason},
        )
    else:
        variants.extend(generated)
    sink.add_aux_usage(usage)
    sink.record(_STEP_MULTI_QUERY, latency_ms=int((time.perf_counter() - t0) * 1000))
    return variants


# ---------- parallel_recall（dd §8.2：并集去重 + 单路容错） ----------


def normalize_scope(scope: dict | None) -> tuple[set[str] | None, set[str] | None]:
    """scope dict 落型（WP-08 交接单⑤：键 ``link_ids``/``story_ids``）。

    返回 (link_ids, story_ids)，键缺省为 None（不约束）；空列表是合法约束
    （白名单为空）。形状非法抛 ValidationError（调试面板/eval 边界可直传）。
    """
    if scope is None:
        return None, None
    if not isinstance(scope, dict):
        raise ValidationError("scope 必须为对象", details={"got": type(scope).__name__})
    unknown = sorted(set(scope) - {"link_ids", "story_ids"})
    if unknown:
        raise ValidationError("scope 含未知键", details={"unknown": unknown})

    def _ids(key: str) -> set[str] | None:
        val = scope.get(key)
        if val is None:
            return None
        if isinstance(val, str) or not isinstance(val, (list, tuple, set)):
            raise ValidationError(
                f"scope.{key} 必须为字符串数组", details={"got": type(val).__name__}
            )
        out: set[str] = set()
        for i, item in enumerate(val):
            if not isinstance(item, str) or not item.strip():
                raise ValidationError(f"scope.{key} 含非法元素", details={"index": i})
            out.add(item)
        return out

    return _ids("link_ids"), _ids("story_ids")


async def parallel_recall(
    reader: ReMeReader,
    queries: Sequence[QueryVariant],
    topk: int,
    types: list[str] | None = None,
    scope: dict | None = None,
    *,
    concurrency: int = RECALL_CONCURRENCY,
    sink: RetrievalTraceBuilder | None = None,
    cache: RetrievalCache | None = None,
    task_id: str | None = None,
    workspace_id: str = "",
    kb_id: str = "",
) -> list[Candidate]:
    """各路并发 search（信号量限并发），按 (entry_id, entry_version) 并集去重。

    - 同一条目多路命中：保留最高分，source_channel 聚合为排序逗号拼接 tag
      （tag 格式 ``{channel}:{queries 下标}``），latency 取最高分路的耗时；
    - 单路失败产出 error 候选（kept=False），不影响他路；CancelledError 原样
      传播（批次边界由 Runner 控制）；
    - caps.metadata_filter=True 时 types/scope 由服务端过滤（原样透传）；
      False 时服务端忽略，由 meta_filter 降级路径兜底（dd §8.4）；
    - entry_version 能力缺失（上游返回空版本）→ 本地 h-{sha256 前 12 位}
      （dd §8.4，recall 时内容已在手）；
    - 输出排序：正常候选 score 降序（同分 entry_id 升序），error 候选垫底按路序。

    WP-12 增可选缓存接线（dd §8.5，cache.py 交接单约定的管线接线点）：
    传入 ``cache`` 时必须同给 ``task_id``/``workspace_id``；每路先按
    ``recall_key(workspace_id, kb_id, query.text, topk, types)`` 查缓存，
    命中（含空列表）零 search 调用、路径延迟记 0；未命中 search 成功后回填。
    """
    sink = sink or RetrievalTraceBuilder()
    normalize_scope(scope)  # 形状校验前置于并发（非法 scope 快速失败）
    if cache is not None:
        if not task_id or not workspace_id:
            raise ValidationError("parallel_recall 使用 cache 必须提供 task_id 与 workspace_id")
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one_path(
        index: int, query: QueryVariant
    ) -> tuple[str, int, list | None, Exception | None]:
        tag = f"{query.channel}:{index}"
        t0 = time.perf_counter()
        rkey = (
            recall_key(workspace_id, kb_id, query.text, topk, types)
            if cache is not None
            else ""
        )
        if cache is not None:
            cached = cache.get(task_id, rkey)  # type: ignore[arg-type]
            if isinstance(cached, list) and all(isinstance(x, Entry) for x in cached):
                return tag, 0, cached, None
        try:
            async with sem:
                entries = await reader.search(
                    query.text, top_k=topk, types=types, scope=scope
                )
        except Exception as e:  # noqa: BLE001 —— 单路容错边界；CancelledError 属 BaseException 不拦截
            return tag, int((time.perf_counter() - t0) * 1000), None, e
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if cache is not None:
            cache.put(task_id, rkey, entries)  # type: ignore[arg-type]
        return tag, latency_ms, entries, None

    t0 = time.perf_counter()
    results = await asyncio.gather(*(_one_path(i, q) for i, q in enumerate(queries)))

    merged: dict[tuple[str, str], Candidate] = {}
    channels: dict[tuple[str, str], set[str]] = {}
    ok_rows: list[Candidate] = []
    error_rows: list[Candidate] = []
    for query, (tag, latency_ms, entries, err) in zip(queries, results):
        if err is not None:
            error_rows.append(
                Candidate(
                    entry_id="",
                    entry_version="",
                    title="",
                    score=0.0,
                    source_channel=tag,
                    entry_type=EntryType.LINK_INDEX,  # 冻结模型必填的占位；以 error 区分
                    kept=False,
                    latency_ms=latency_ms,
                    error=str(err) or err.__class__.__name__,
                )
            )
            continue
        for e in entries or []:
            version = e.entry_version or local_entry_version(e.content)
            key = (e.entry_id, version)
            score = lexical_score(query.text, e.title, e.content)
            existed = merged.get(key)
            if existed is None:
                merged[key] = Candidate(
                    entry_id=e.entry_id,
                    entry_version=version,
                    title=e.title,
                    score=score,
                    source_channel="",
                    entry_type=e.entry_type,
                    kept=True,
                    latency_ms=latency_ms,
                )
                channels[key] = {tag}
            else:
                channels[key].add(tag)
                if score > existed.score:
                    existed.score = score
                    existed.latency_ms = latency_ms
    for key, cand in merged.items():
        cand.source_channel = _CHANNEL_TAG_SEP.join(sorted(channels[key]))
        ok_rows.append(cand)
    ok_rows.sort(key=lambda c: (-c.score, c.entry_id))

    rows = ok_rows + error_rows
    sink.record(
        "parallel_recall", latency_ms=int((time.perf_counter() - t0) * 1000), candidates=rows
    )
    return rows


# ---------- meta_filter（dd §8.2/§8.4：结构化过滤 + 镜像降级） ----------


async def meta_filter(
    cands: list[Candidate],
    *,
    types: list[str] | None = None,
    scope: dict | None = None,
    mirror: IndexMirror | None = None,
    sink: RetrievalTraceBuilder | None = None,
) -> list[Candidate]:
    """类型/归属结构化过滤，被剔除候选就地标 kept=False + drop_reason 留痕。

    - 类型过滤恒在本算子完成（Candidate.entry_type 恒可得）→ ``filtered_type``；
    - 归属过滤：mirror 非空（§8.4 镜像降级路径）→ 逐候选走 IndexMirror.matches
      （类型判定不在镜像侧重复）→ ``filtered_scope``；mirror 为空时 scope 已在
      recall 阶段服务端过滤（能力可用）或按 §8.4 跳过过滤（镜像空降级），
      本算子不做二次判定直接放行；
    - kept 已为 False 的候选（如 recall error 行）原样透传，不重复裁决；
    - scope 传入且 mirror 非空时先 ensure_fresh（best-effort，TTL 内零开销）。
    """
    sink = sink or RetrievalTraceBuilder()
    link_ids, story_ids = normalize_scope(scope)
    has_scope = link_ids is not None or story_ids is not None
    if has_scope and mirror is not None:
        await mirror.ensure_fresh()

    t0 = time.perf_counter()
    type_set = set(types) if types is not None else None
    for cand in cands:
        if not cand.kept:
            continue
        if type_set is not None and cand.entry_type.value not in type_set:
            cand.kept = False
            cand.drop_reason = "filtered_type"
        elif has_scope and mirror is not None:
            ok, reason = mirror.matches(
                cand.entry_id, link_ids=link_ids, story_ids=story_ids
            )
            if not ok:
                cand.kept = False
                cand.drop_reason = reason or "filtered_scope"
    sink.record(
        "meta_filter", latency_ms=int((time.perf_counter() - t0) * 1000), candidates=cands
    )
    return cands
