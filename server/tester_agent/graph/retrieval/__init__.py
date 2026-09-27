"""检索子图（dd §8）：算子 + 管线编排。

- ``ops``：检索算子 A（multi_query / parallel_recall / meta_filter，WP-10）；
- ``ops_b``：检索算子 B（rerank / passage_extract / assemble，WP-11）+
  RetrievalOutcome；
- ``cache``：RetrievalCache（§8.5 进程内 LRU，run 分区清空，WP-11）；
- WP-12 落 retrieve_pipeline 编排与 trace/snapshot 落库。
"""

from .cache import RetrievalCache, entry_key, intent_hash, recall_key
from .ops import (
    RECALL_CONCURRENCY,
    RetrievalTraceBuilder,
    meta_filter,
    multi_query,
    normalize_scope,
    parallel_recall,
)
from .ops_b import (
    ORDERING_STRATEGY,
    PASSAGE_WINDOW_CHARS,
    RERANK_BUCKET_SIZE,
    RERANK_LLM_THRESHOLD,
    RetrievalOutcome,
    assemble,
    estimate_tokens,
    passage_extract,
    rerank,
)
from .pipeline import (
    INLINE_PROMPT_VER,
    WEAK_REF_THRESHOLD,
    ClosedLoop,
    close_loop,
    close_retrieval_trace,
    funnel_counts,
    parse_ids,
    retrieve_pipeline,
)

__all__ = [
    "INLINE_PROMPT_VER",
    "ORDERING_STRATEGY",
    "PASSAGE_WINDOW_CHARS",
    "RECALL_CONCURRENCY",
    "RERANK_BUCKET_SIZE",
    "RERANK_LLM_THRESHOLD",
    "ClosedLoop",
    "RetrievalCache",
    "RetrievalOutcome",
    "RetrievalTraceBuilder",
    "WEAK_REF_THRESHOLD",
    "assemble",
    "close_loop",
    "close_retrieval_trace",
    "entry_key",
    "estimate_tokens",
    "funnel_counts",
    "intent_hash",
    "meta_filter",
    "multi_query",
    "normalize_scope",
    "parallel_recall",
    "parse_ids",
    "passage_extract",
    "recall_key",
    "rerank",
    "retrieve_pipeline",
]
