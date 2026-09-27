"""领域 Pydantic 契约（dd §2）——跨层共享的结构化类型唯一来源。

分层依赖 ``api → runtime/graph → adapters → store``：各层都可 import 本模块，
本模块不反向依赖任何层；JSON 列的序列化/反序列化由 DAO 负责（dd §1.2/§2.9）。

WP-03 落地 task / stage_artifact / testcase 三表 Row 转换所需类型；
WP-04 补检索/消息/提案枚举与 §2.8 检索域对象；WP-05 补 CaseFileContent/CaseStep/FileRef；
WP-17 补 §2.3 LinkPlan（LinkRef/StoryRef/NewLinkSuggestion）；
WP-18 补 §2.4 PointPlan（TestPoint）；
WP-20 补 §2.6 CoverageMatrix（CoverageRow）；
其余 §2 类型随对应 WP 增补。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

# ---------- §2.1 枚举（值即落库字符串；WP-03 仅列三表用到的五个） ----------


class TaskStatus(StrEnum):
    RUNNING = "running"
    WAITING_CONFIRM = "waiting_confirm"
    WAITING_INPUT = "waiting_input"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


class ArtifactStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    OBSOLETE = "obsolete"


class ArtifactOrigin(StrEnum):
    SYSTEM = "system"
    USER_REVISED = "user_revised"


class CaseStatus(StrEnum):
    ACTIVE = "active"
    OBSOLETE = "obsolete"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    ADOPTED = "adopted"
    EDITED_ADOPTED = "edited_adopted"
    REJECTED = "rejected"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageKind(StrEnum):
    CHAT = "chat"
    CLARIFICATION_QA = "clarification_qa"
    CHECKPOINT_REVISION = "checkpoint_revision"
    CHANGE_REQUEST = "change_request"
    REGEN_INSTRUCTION = "regen_instruction"


class EntryType(StrEnum):
    """ReMe 五类知识（LINK_INDEX=链路/故事索引摘要）。"""

    BUSINESS = "business"
    FLOW_CASE = "flow_case"
    DEFECT = "defect"
    API = "api"
    DB = "db"
    LINK_INDEX = "link_index"


# ---------- §2.2 需求条款（task.clauses） ----------


class ClauseRef(BaseModel):
    clause_id: str
    level: int
    title_path: list[str]
    anchor: str
    text_hash: str
    status: Literal["active", "deleted"] = "active"


# ---------- §2.3 链路识别产物（LinkPlan；WP-17） ----------


class LinkRef(BaseModel):
    link_id: str  # 命中已有链路：稳定沿用知识库 ID；新增："new-link-{n}"
    title: str
    summary: str  # 一句话
    hit: bool  # True=命中知识库已有条目；False=建议新增（US1.3）
    entry_id: str | None = None  # hit=True 时的知识条目
    entry_version: str | None = None
    confidence: float  # 0~1，命中置信度，供前端标灰
    story_ids: list[str] = []


class StoryRef(BaseModel):
    story_id: str
    link_id: str
    title: str
    summary: str
    hit: bool
    entry_id: str | None = None
    entry_version: str | None = None
    confidence: float
    rationale: str  # 为什么与本需求相关（引用 clause_id）
    related_clause_ids: list[str] = []


class NewLinkSuggestion(BaseModel):
    """hit=False 项的结构化新增建议（仅提示，不写库）。"""

    suggested_link_title: str
    suggested_story_title: str
    reason: str
    source_clause_ids: list[str]


class LinkPlan(BaseModel):
    links: list[LinkRef]
    stories: list[StoryRef]
    new_suggestions: list[NewLinkSuggestion]


# ---------- §2.4 测试点产物（PointPlan；WP-18） ----------


class TestPoint(BaseModel):
    point_id: str  # "pt-{story 序号}-{批内序号}"，CP2 修订时保持稳定
    story_id: str
    title: str  # 测试意图一句话
    angle: str  # 覆盖角度：正常/异常/边界/权限/兼容/回归…
    method: str  # 设计方法标签：等价类/边界值/状态迁移/场景法…
    clause_ids: list[str]  # 需求条款溯源
    source_entry_ids: list[str]  # 知识条目溯源（entry_id 带版本另在 trace 查）
    priority: Literal["P0", "P1", "P2"] = "P1"


class PointPlan(BaseModel):
    points: list[TestPoint]


# ---------- §2.6 覆盖矩阵（CoverageMatrix；WP-20） ----------


class CoverageRow(BaseModel):
    clause_id: str
    object_type: Literal["point", "case"]
    object_id: str  # point_id / case_id
    covered: bool
    evidence: str  # 命中依据摘要（测试点标题/用例标题）


class CoverageMatrix(BaseModel):
    rows: list[CoverageRow]
    uncovered_clauses: list[str]
    supplemental_rounds: int  # 已执行的补充生成轮次（上限 2）
    degraded: bool = False  # 达上限仍有未覆盖→告警降级


# ---------- task.requirement_ref（DDL §3.1 注释形态 {path,content_hash,clause_count}） ----------


class RequirementRef(BaseModel):
    path: str
    content_hash: str
    clause_count: int


# ---------- §2.5 用例产物（testcase 行领域形态） ----------


class TraceRefs(BaseModel):
    clause_ids: list[str] = []
    entry_ids: list[str] = []
    point_ids: list[str] = []


class Lineage(BaseModel):
    root_case_id: str
    regenerated_from_case_id: str | None = None


class CaseRecord(BaseModel):
    """testcase 行的领域形态（dd §2.5）。task_id/error_info/updated_at 为存储侧字段，
    由 CaseRow 承载，不进本模型。"""

    case_id: str
    point_id: str
    stage_version: int
    lineage: Lineage
    status: CaseStatus
    review_status: ReviewStatus
    file_path: str
    content_hash: str
    title: str
    trace_refs: TraceRefs


class CaseStep(BaseModel):
    """用例单步（dd §4.2：步骤行 + 预期行）。"""

    seq: int
    action: str
    expect: str = ""


class CaseFileContent(BaseModel):
    """用例 MD 文件的结构化形态（dd §2.5/§4.2），与 front-matter/正文一一对应。

    ``expected`` 在 v1 模板下不单独渲染：每步预期以 ``steps[].expect`` 为准
    （dd §2.5"Q1 未定前 v1 等长"）；解析时该字段保持缺省，不做等长强校验。
    """

    case_id: str
    point_id: str
    stage_version: int
    title: str
    priority: Literal["P0", "P1", "P2"] = "P1"
    preconditions: list[str] = []
    steps: list[CaseStep]
    expected: list[str] = []
    test_data: str | None = None
    trace_refs: TraceRefs


# ---------- §2.9 运行期辅助类型（task.error_info 强类型） ----------


class ErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool
    node: str | None = None
    trace_id: str | None = None
    details: dict = {}


class FileRef(BaseModel):
    """文件写入回执（dd §2.9）：path 为相对 data/ 的 POSIX 相对路径。"""

    path: str
    content_hash: str
    size_bytes: int | None = None


# ---------- §2.8 检索域对象（retrieval_trace / context_snapshot JSON 列） ----------


class QueryVariant(BaseModel):
    channel: Literal["raw", "keyword", "rewrite"]
    text: str


class Candidate(BaseModel):
    entry_id: str
    entry_version: str
    title: str
    score: float
    source_channel: str  # 召回路标识："raw:0"/"keyword:1"…
    entry_type: EntryType
    kept: bool
    drop_reason: str | None = None  # filtered_type / rerank_cutoff / budget_cut / dedup
    latency_ms: int | None = None
    error: str | None = None


class InjectedItem(BaseModel):
    entry_id: str
    entry_version: str
    title: str
    tokens_est: int
    position: int  # 在 prompt 知识块中的序号（0 起）
    char_offset: int | None = None  # full 快照 JSONL 行偏移
    byte_length: int | None = None
    passage: str | None = None  # 仅节点内存使用；meta 快照不落正文


class RetrievalConfig(BaseModel):
    """检索阶段配置（dd §2.8/§8.1；各阶段初值见 dd §8.1 RETRIEVAL_PRESETS）。"""

    stage: str
    query_paths: int
    recall_topk: int
    inject_limit: int
    inject_form: Literal["index_line", "passage"]
    allowed_types: list[EntryType]
    token_budget: int
    batch_unit_id: str | None = None  # case_generate 时为 point_id


class DegradedStep(BaseModel):
    step: str
    reason: str
    fallback: str
