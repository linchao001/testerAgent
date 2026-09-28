"""阶段/节点常量（dd §1.3）与检索阶段预设（dd §8.1 RETRIEVAL_PRESETS）。

WP-13 提前落地本模块（WBS 将 constants 归在 WP-15 "constants/state" 切片）：
eval 冒烟需要三阶段检索预设；WP-15 build_graph 直接 import 复用本模块，
图 state 定义仍归 WP-15。
"""

from ..domain import EntryType, RetrievalConfig

# ---------- dd §1.3 阶段/节点常量（值即落库字符串） ----------

STAGE_INTAKE = "intake"
STAGE_LINK_IDENTIFY = "link_identify"
STAGE_POINT_WRITE = "point_write"
STAGE_CASE_GENERATE = "case_generate"
STAGE_COVERAGE_CHECK = "coverage_check"
STAGE_REVIEW_EXPORT = "review_export"

BATCH_DEFAULT_SIZE = 5
HEARTBEAT_INTERVAL_SEC = 10
HEARTBEAT_STALE_SEC    = 120
SUSPEND_STALE_DAYS     = 7

# ---------- dd §8.1 检索阶段配置初值（S5 标定后固化） ----------

RETRIEVAL_PRESETS: dict[str, RetrievalConfig] = {
    STAGE_LINK_IDENTIFY: RetrievalConfig(
        stage=STAGE_LINK_IDENTIFY,
        query_paths=3,
        recall_topk=50,
        inject_limit=200,
        inject_form="index_line",
        allowed_types=[EntryType.LINK_INDEX],
        token_budget=12_000,
    ),
    STAGE_POINT_WRITE: RetrievalConfig(
        stage=STAGE_POINT_WRITE,
        query_paths=4,
        recall_topk=40,
        inject_limit=20,
        inject_form="passage",
        allowed_types=[EntryType.BUSINESS, EntryType.FLOW_CASE, EntryType.DEFECT],
        token_budget=16_000,
    ),
    STAGE_CASE_GENERATE: RetrievalConfig(
        stage=STAGE_CASE_GENERATE,
        query_paths=3,
        recall_topk=30,
        inject_limit=15,
        inject_form="passage",
        allowed_types=[
            EntryType.API,
            EntryType.DB,
            EntryType.DEFECT,
            EntryType.BUSINESS,
        ],
        token_budget=12_000,
    ),
}
