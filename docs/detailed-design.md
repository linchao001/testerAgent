# TesterAgent 详细设计文档

| 项 | 内容 |
|---|---|
| 版本 | v0.4 |
| 状态 | 一期候选发布 |
| 日期 | 2026-09-28 |
| 上游设计 | [tech-design.md v0.3](file:///D:/code/github/testerAgent/docs/tech-design.md) |
| 对应需求 | [PRD.md v0.7](file:///D:/code/github/testerAgent/docs/PRD.md) |
| 控制面范式 | [plan-execute-reflexion 设计](file:///D:/code/github/testerAgent/docs/superpowers/specs/2026-09-28-plan-execute-reflexion-design.md) |

> v0.4 变更摘要（Plan-Execute）：① 生产主图为控制环 `plan → dispatch → execute_step → await_human → reflect`（§7）；阶段节点函数能力化（`invoke_capability`）；② §2.10 增补 AgentPlan / ReviewProposal / HumanDecision；③ §3 迁移 004（`current_plan_artifact_id`、`artifact.kind`、`subtask`）；④ §10 ConfirmIn 增 `gate_kind`（缺省 `plan_confirm`）及 plan/review-proposals 端点；⑤ §12 会话优先 SessionPage + GateConfirmCard / ReviewProposalCard。
> v0.3 变更摘要（WP-X2 / C6 收口）：① Q1 定稿为本节 §4.2 v1 模板；Q2 定稿为 §9.4 MD zip（Excel 二期）；② §19.5 `cli backup <out_dir>` 已接线（SQLite online backup + workspaces/）；③ `cleanup_exports` 路径对齐 `workspaces/{ws}/{task}/exports`（修正误扫 `*/tasks/*`）；④ 发布门禁见 `docs/plan/release-checklist.md`；S3~S7 结论回灌 tech-design §8。
> v0.2 变更摘要：补全 §2.9 运行期辅助类型（ImpactAnalysis/BatchProgress/ErrorInfo/ClosedLoop）；新增 §17 异常体系（类层次、翻译边界、trace_id）、§18 端到端时序（主场景/崩溃恢复/回退继承/提案异常路径）、§19 依赖与一键部署（pyproject、dev.sh、init-db 种子、备份）、§20 Prompt 模板 v1 正文、§21 开工前检查清单与文档维护约定；kb_proposal 增 fail_count 列。
> v0.2 裁决补记（2026-09-26）：testcase 增 `created_at` 列（002 迁移，schema_version=2），用例列表分页游标时间列由 updated_at 改为 created_at；task.requirement_ref 补领域类型名 RequirementRef。
> v0.1：首版 16 章（领域模型、DDL、文件规范、存储/运行时、图与检索子图、适配器、API、一致性协议、前端、配置、错误码、测试、实施切片）。

> 本文是 tech-design 的实现级细化：精确 DDL、领域对象 schema、模块/函数级接口、图节点与 Prompt 契约、关键流程伪代码、API 请求响应模型、错误码目录与测试策略。文中 Python 代码为接口契约与核心逻辑示意（类型签名即约定），非最终实现逐行代码。**编排真相以控制环 + 能力函数为准**；§7.2 起的阶段节点描述继续作为能力实现契约（由 `invoke_capability` 调度）。

---

## 1. 概述与约定

### 1.1 阅读路径

| 读者 | 建议章节 |
|---|---|
| 后端开发 | §3 DDL/DAO → §5 存储 → §6 运行时 → **§7 控制环+能力** → §8 检索 → §10 API |
| 前端开发 | §10 API schema/错误码 → **§12 SessionPage** → §6.2/§10.4 SSE |
| 测试 | §14 错误码 → §15 测试策略 → §6.3 状态机 → §7.1 控制环挂起语义 |
| 评审 | §2 领域模型（含 §2.10 Plan-Execute）→ §7.4 Prompt 契约 → §11 一致性协议 |

范式权威：控制面行为以 [plan-execute-reflexion 设计](file:///D:/code/github/testerAgent/docs/superpowers/specs/2026-09-28-plan-execute-reflexion-design.md) 为准；tech-design §4 已标注遗留阶段图语义。

### 1.2 通用约定

- Python 3.11+；Pydantic v2；异步 IO（`async/await`）为默认风格，仅 SQLite/文件短操作用 `anyio.to_thread` 包裹。
- 所有时间存 UTC ISO8601 字符串（`datetime.now(timezone.utc).isoformat()`），前端本地渲染。
- ID：业务主键一律 uuid4 hex（32 位）；对外展示用短码（前 8 位）。**确定性重算 ID** 用 uuid5，命名空间固定 `NAMESPACE = UUID("6f3d...固定")`（代码内常量 `ID_NS`）。
- JSON 字段存库前统一 `model_dump_json()`；读出后由 DAO 层解析为对应 Pydantic 类型，业务层不接触原始字符串。
- 分层依赖方向单向：`api → runtime/graph → adapters → store`；**store、adapters 不 import graph**；`reme_writer` 仅被 `api/kb.py` import（tech-design D4，CI 加 import-linter 规则）。
- 配置注入：各层构造函数显式传依赖（Context 对象，见 §6.1），不使用全局单例；仅 FastAPI app 持有一个组合根（composition root）。

### 1.3 代码内命名与阶段常量

阶段/节点名集中在 `server/graph/constants.py`，值即落库字符串（D11：不建 SQL/语言枚举强耦合，但代码内仍有常量防拼写错误）：

```python
STAGE_INTAKE          = "intake"
STAGE_LINK_IDENTIFY   = "link_identify"
STAGE_POINT_WRITE     = "point_write"
STAGE_CASE_GENERATE   = "case_generate"
STAGE_COVERAGE_CHECK  = "coverage_check"
STAGE_REVIEW_EXPORT   = "review_export"
CHECKPOINT_1, CHECKPOINT_2 = "checkpoint1", "checkpoint2"

# Plan-Execute 步骤 kind 见 domain.PlanStepKind（字符串，不建 SQL 枚举）
# 能力映射：intake_parse→intake / coverage_design→link_identify /
#           point_design→point_write / case_generate→case_generate

BATCH_DEFAULT_SIZE = 5
HEARTBEAT_INTERVAL_SEC = 10
HEARTBEAT_STALE_SEC    = 120
SUSPEND_STALE_DAYS     = 7
```

---

## 2. 领域模型（Pydantic 契约）

所有跨节点、跨层传递的结构化对象在此定义；`stage_artifact.payload`、message.payload、API body 均为这些类型的（反）序列化。

### 2.1 枚举（值即落库字符串）

```python
from enum import StrEnum

class TaskStatus(StrEnum):
    RUNNING = "running"
    WAITING_CONFIRM = "waiting_confirm"
    WAITING_INPUT = "waiting_input"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"

class ArtifactStatus(StrEnum):
    ACTIVE = "active"; SUPERSEDED = "superseded"; OBSOLETE = "obsolete"

class ArtifactOrigin(StrEnum):
    SYSTEM = "system"; USER_REVISED = "user_revised"

class CaseStatus(StrEnum):
    ACTIVE = "active"; OBSOLETE = "obsolete"

class ReviewStatus(StrEnum):
    PENDING = "pending"; ADOPTED = "adopted"
    EDITED_ADOPTED = "edited_adopted"; REJECTED = "rejected"

class ProposalStatus(StrEnum):
    PENDING = "pending"; CONFIRMED = "confirmed"
    REJECTED = "rejected"; EXPIRED = "expired"

class MessageRole(StrEnum):
    USER = "user"; ASSISTANT = "assistant"; SYSTEM = "system"

class MessageKind(StrEnum):
    CHAT = "chat"
    CLARIFICATION_QA = "clarification_qa"
    CHECKPOINT_REVISION = "checkpoint_revision"
    CHANGE_REQUEST = "change_request"
    REGEN_INSTRUCTION = "regen_instruction"
    PLAN_REVISION = "plan_revision"
    REVIEW_DECISION = "review_decision"
    GATE_CONFIRM = "gate_confirm"

class EntryType(StrEnum):   # ReMe 五类知识
    BUSINESS = "business"; FLOW_CASE = "flow_case"; DEFECT = "defect"
    API = "api"; DB = "db"; LINK_INDEX = "link_index"   # LINK_INDEX=链路/故事索引摘要
```

### 2.2 需求条款（intake 产物，溯源锚点）

```python
class ClauseRef(BaseModel):
    clause_id: str            # 规则见 §7.5①，如 "h2-1-h3-2"
    level: int                # 标题层级 2~6
    title_path: list[str]     # 从首个有效标题到本条的标题链，如 ["支付流程","退款","超时处理"]
    anchor: str               # 原文中本段起始的原文片段（前 32 字），人工定位用
    text_hash: str            # sha1(本段原文规范化后)，变更检测
    status: Literal["active", "deleted"] = "active"
```

需求正文不进 state、不进 clauses；节点需要时按 `task.requirement_ref.path` + clause_id 区间从文件读取（§5.2）。

### 2.3 链路识别产物（LinkPlan）

```python
class LinkRef(BaseModel):
    link_id: str                     # 命中已有链路：稳定沿用知识库 ID；新增："new-link-{n}"
    title: str
    summary: str                     # 一句话
    hit: bool                        # True=命中知识库已有条目；False=建议新增（US1.3）
    entry_id: str | None = None      # hit=True 时的知识条目
    entry_version: str | None = None
    confidence: float                # 0~1，命中置信度，供前端标灰
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
    rationale: str                   # 为什么与本需求相关（引用 clause_id）
    related_clause_ids: list[str] = []

class NewLinkSuggestion(BaseModel):  # hit=False 项的结构化新增建议（仅提示，不写库）
    suggested_link_title: str
    suggested_story_title: str
    reason: str
    source_clause_ids: list[str]

class LinkPlan(BaseModel):
    links: list[LinkRef]
    stories: list[StoryRef]
    new_suggestions: list[NewLinkSuggestion]
```

用户在 CP1 增删改后放行；**修改契约**：允许改 title/summary、删条目、把 `hit=false` 项绑定到某个已有 entry_id（用户在知识库索引树上手选）；不允许新增任意字段。修订后 origin=user_revised。

### 2.4 测试点产物（PointPlan）

```python
class TestPoint(BaseModel):
    point_id: str                    # "pt-{story 局部序号}-{n}"，CP2 修订时保持稳定
    story_id: str
    title: str                       # 测试意图一句话
    angle: str                       # 覆盖角度：正常/异常/边界/权限/兼容/回归…
    method: str                      # 设计方法标签：等价类/边界值/状态迁移/场景法…
    clause_ids: list[str]            # 需求条款溯源
    source_entry_ids: list[str]      # 知识条目溯源（entry_id 带版本另在 trace 查）
    priority: Literal["P0","P1","P2"] = "P1"

class PointPlan(BaseModel):
    points: list[TestPoint]
```

### 2.5 用例产物（CaseBatch / TestCaseContent）

```python
class CaseFileContent(BaseModel):     # 与 MD 文件 front-matter/正文一一对应（§4.2）
    case_id: str
    point_id: str
    stage_version: int
    title: str
    priority: Literal["P0","P1","P2"]
    preconditions: list[str]
    steps: list[CaseStep]
    expected: list[str]               # v1 不单独渲染；每步预期以 steps[].expect 为准（Q1 定稿）
    test_data: str | None = None
    trace_refs: TraceRefs

class CaseStep(BaseModel):
    seq: int
    action: str
    expect: str

class TraceRefs(BaseModel):
    clause_ids: list[str] = []
    entry_ids: list[str] = []         # 仅允许出现在注入白名单中的 ID
    point_ids: list[str] = []

class CaseRecord(BaseModel):          # testcase 行的领域形态
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

class Lineage(BaseModel):
    root_case_id: str                 # 重生成链的根；首版=自身 case_id
    regenerated_from_case_id: str | None = None

class BatchResult(BaseModel):         # 节点内一批的执行记录（写 progress + 结果）
    batch_id: str
    node: str
    unit_ids: list[str]               # 本批故事/测试点 ID
    status: Literal["started","done","failed"]
    case_ids: list[str] = []
```

### 2.6 覆盖矩阵（CoverageMatrix）

```python
class CoverageRow(BaseModel):
    clause_id: str
    object_type: Literal["point","case"]
    object_id: str                    # point_id / case_id
    covered: bool
    evidence: str                     # 命中依据摘要（如测试点标题/步骤关键词）

class CoverageMatrix(BaseModel):
    rows: list[CoverageRow]
    uncovered_clauses: list[str]
    supplemental_rounds: int          # 已执行的补充生成轮次（上限 2）
    degraded: bool = False            # 达上限仍有未覆盖→告警降级
```

### 2.7 澄清与运行控制

```python
class Clarification(BaseModel):
    question_id: str                  # uuid5(task_id, node, index)
    node: str
    question: str
    options: list[str] | None = None
    related_clause_ids: list[str] = []
    answer: str | None = None         # 用户答复后回填（同步写 message）

class BatchCursor(BaseModel):         # state.batch_cursor[node] 与 progress 行的内存形态
    next_index: int                   # 下一个待处理单元序号
    done_batch_ids: list[str]
    idempotency_nonce: str            # 本 run 本节点的重算盐（见 §7.3）
```

### 2.8 检索域对象

```python
class QueryVariant(BaseModel):
    channel: Literal["raw","keyword","rewrite"]
    text: str

class Candidate(BaseModel):
    entry_id: str
    entry_version: str
    title: str
    score: float
    source_channel: str               # recall 路标识："raw:0"/"keyword:1"...
    entry_type: EntryType
    kept: bool
    drop_reason: str | None = None    # filtered_type / rerank_cutoff / budget_cut / dedup
    latency_ms: int | None = None
    error: str | None = None

class InjectedItem(BaseModel):
    entry_id: str
    entry_version: str
    title: str
    tokens_est: int
    position: int                     # 在 prompt 知识块中的序号（0 起）
    char_offset: int | None = None    # full 快照 JSONL 行偏移
    byte_length: int | None = None
    passage: str | None = None        # 仅节点内存使用；meta 快照不落正文

class RetrievalConfig(BaseModel):
    stage: str
    query_paths: int
    recall_topk: int
    inject_limit: int
    inject_form: Literal["index_line","passage"]
    allowed_types: list[EntryType]
    token_budget: int
    batch_unit_id: str | None = None  # case_generate 时为 point_id

class DegradedStep(BaseModel):
    step: str; reason: str; fallback: str
```

### 2.9 运行期辅助类型（被多章引用，集中定义）

```python
# ---- 回退影响面（§11.2 的输入输出契约）----
class IdChange(BaseModel):
    id: str
    kind: Literal["link", "story", "point"]
    fields_changed: list[str]          # 变化字段名，用于 affected/unaffected 判定

class StageImpact(BaseModel):
    stage: str
    affected_ids: list[str] = []
    unaffected_ids: list[str] = []
    added_ids: list[str] = []
    removed_ids: list[str] = []

class ImpactAnalysis(BaseModel):
    target_stage: str
    link_changes: list[IdChange] = []
    story_changes: list[IdChange] = []
    point_changes: list[IdChange] = []
    downstream: list[StageImpact] = []
    affected_point_ids: list[str] = []    # 直达 case 作废判定的汇总
    summary: str = ""                    # 给前端 ImpactPreview 的一句话：影响 N 条测试点/M 条用例

    def affected_points(self) -> list[str]: ...

# ---- 批次进度（stage_artifact.progress 行内存形态）----
class BatchEntry(BaseModel):
    batch_id: str
    unit_ids: list[str]
    status: Literal["started", "done", "failed"]
    idem_key: str
    result_ids: list[str] = []

class BatchProgress(BaseModel):
    node: str
    entries: list[BatchEntry] = []
    def get(self, batch_id: str) -> Literal["started","done","failed"] | None: ...
    def mark_started(self, batch_id: str, unit_ids: list[str], idem_key: str) -> None: ...
    def mark_done(self, batch_id: str, result_ids: list[str]) -> None: ...
    def mark_failed(self, batch_id: str) -> None: ...
    def dump(self) -> str: ...              # JSON 写入 artifact.progress

# ---- Runner / API 通用 ----
class RunHandle(BaseModel):
    task_id: str
    events_url: str
    resume_from: ResumePoint | None = None

class ResumePoint(BaseModel):
    node: str
    batch_id: str | None
    done: int                            # 已完成工作单元数
    total: int

class FileRef(BaseModel):
    path: str                            # 相对 data/
    content_hash: str
    size_bytes: int | None = None

class ErrorInfo(BaseModel):             # task.error_info 的强类型
    code: str                            # §14 目录
    message: str
    retryable: bool
    node: str | None = None
    trace_id: str | None = None
    details: dict = {}

class ClosedLoop(BaseModel):            # §8.6 输出
    referenced: list[str]
    injected_not_used: list[str]
    hallucinated: list[str]
    weak_refs: list[str]
```

DB 行类型（`store/models.py` 内）与上述领域类型分离：`TaskRow/ArtifactRow/CaseRow/TraceRow/SnapshotRow/EventRow` 为接近表结构的 dataclass（JSON 字段保持原始字符串），DAO 负责 `Row ↔ Pydantic` 转换；业务层只收 Pydantic 对象，不接触 Row 与 JSON 字符串。

### 2.10 Plan-Execute 控制面（AgentPlan / 评审 / 人决策）

生产主图以 Plan 为编排主键；阶段产物（LinkPlan/PointPlan/…）仍作 capability 输出与 `artifacts` 载荷。权威定义见 `server/tester_agent/domain.py`。

```python
class PlanStepKind(StrEnum):
    INTAKE_PARSE = "intake_parse"
    COVERAGE_DESIGN = "coverage_design"   # 映射原 CP1 / link_identify
    POINT_DESIGN = "point_design"         # 映射原 CP2 / point_write
    CASE_GENERATE = "case_generate"
    REVIEW_COVERAGE = "review_coverage"
    REVIEW_QUALITY = "review_quality"
    REVIEW_ADOPTION = "review_adoption"
    REPAIR = "repair"
    AWAIT_HUMAN = "await_human"

class PlanStep(BaseModel):
    step_id: str
    kind: PlanStepKind
    goal: str
    input_refs: list[str] = []
    output_ref: str | None = None          # artifact_id
    status: Literal["pending","running","done","failed","skipped"] = "pending"
    requires_confirm: bool = False
    max_reflect: int = 2

class AgentPlan(BaseModel):
    plan_id: str
    version: int
    goal: str
    steps: list[PlanStep]
    status: Literal["draft","active","completed","failed"] = "active"
    replan_count: int = 0

class SubtaskResult(BaseModel):
    subtask_id: str
    kind: str
    thread_id: str                         # `{parent}::sub::{subtask_id}`
    status: Literal["running","done","failed","cancelled"]
    summary: str = ""
    output_ref: str | None = None

class ReviewProposalItem(BaseModel):
    target_id: str
    action: Literal["adopt","edit_adopt","reject","add_point","add_case","repair"]
    rationale: str
    confidence: float = 0.5
    patch: dict | None = None

class ReviewProposal(BaseModel):
    scope: str                             # coverage | quality | adoption | ...
    items: list[ReviewProposalItem]
    matrix_ref: str | None = None
    degraded: bool = False

class HumanDecision(BaseModel):
    gate_kind: Literal["plan_confirm", "review_decision"]
    action: Literal["confirm", "modify", "reject_rerun"]
    artifact_id: str
    payload: dict | None = None            # modify 时为修订后产物 / ReviewProposal
```

持久化：`AgentPlan` 落 `stage_artifact`（`kind=agent_plan`，`task.current_plan_artifact_id` 指向 active）；评审提案 `kind=review_proposal`；子任务行见 §3.1.1。控制 state 另持 `artifacts` / `plan_cursor` / `human_gates` / `reflection_log`（§7.1）。

---

## 3. 数据库详细设计

### 3.1 DDL（SQLite，最新 schema_version = 4）

> 001 为首版 15 表；002（2026-09-26）testcase 增 `created_at`；003 增 `batch_id`；**004（Plan-Execute）** 增 `task.current_plan_artifact_id`、`stage_artifact.kind`、`subtask` 表。下列 DDL 以 001+002 主体为主；004 增量见 §3.1.1。历史迁移文件只增不改（§3.2）。

两个库文件，均在 data 根下：

- `app.db`：业务库（WAL，busy_timeout=5000）；
- `checkpoints.db`：LangGraph checkpointer 独占库（同样 WAL），不手工建表（由 langgraph-checkpoint-sqlite 初始化）。

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS schema_meta (
  schema_version INTEGER PRIMARY KEY,
  applied_at     TEXT NOT NULL
);

-- ---------- workspace ----------
CREATE TABLE workspace (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  kb_config   TEXT NOT NULL DEFAULT '{}',   -- JSON: {mode:'sdk'|'service', target, kb_id, options}
  created_at  TEXT NOT NULL,
  deleted_at  TEXT
);

-- ---------- agent ----------
CREATE TABLE agent (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  agent_type TEXT NOT NULL,                 -- 'case_designer'
  config     TEXT NOT NULL DEFAULT '{}',    -- JSON: strategies/prompts/snapshot_level
  builtin    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE agent_workspace (
  agent_id     TEXT NOT NULL REFERENCES agent(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  PRIMARY KEY (agent_id, workspace_id)
);

-- ---------- conversation / message ----------
CREATE TABLE conversation (
  id           TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspace(id),
  title        TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE message (
  id              TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversation(id),
  task_id         TEXT,                       -- 任务前自由对话可空
  role            TEXT NOT NULL,              -- user/assistant/system
  kind            TEXT NOT NULL,              -- 见 MessageKind
  content         TEXT NOT NULL,
  ref_artifact_id TEXT,
  payload         TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL
);
CREATE INDEX idx_message_conv ON message(conversation_id, created_at);
CREATE INDEX idx_message_task ON message(task_id);

-- ---------- task ----------
CREATE TABLE task (
  id                  TEXT PRIMARY KEY,
  conversation_id     TEXT NOT NULL REFERENCES conversation(id),
  workspace_id        TEXT NOT NULL REFERENCES workspace(id),
  status              TEXT NOT NULL,
  current_stage       TEXT NOT NULL,
  requirement_ref     TEXT NOT NULL DEFAULT '{}',  -- {path,content_hash,clause_count}
  clauses             TEXT NOT NULL DEFAULT '[]',  -- list[ClauseRef]
  langgraph_thread_id TEXT NOT NULL,
  graph_run_id        TEXT NOT NULL,
  runner_heartbeat    TEXT,
  cancel_requested    INTEGER NOT NULL DEFAULT 0,
  snapshot_level      TEXT NOT NULL DEFAULT 'meta',
  error_info          TEXT,                        -- {code,message,retryable,node,details}
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL
);
CREATE INDEX idx_task_ws_status ON task(workspace_id, status);
CREATE INDEX idx_task_conv ON task(conversation_id);
CREATE INDEX idx_task_heartbeat ON task(runner_heartbeat);

-- ---------- stage_artifact ----------
CREATE TABLE stage_artifact (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES task(id),
  stage         TEXT NOT NULL,
  graph_run_id  TEXT NOT NULL,
  stage_version INTEGER NOT NULL,
  origin        TEXT NOT NULL DEFAULT 'system',
  status        TEXT NOT NULL DEFAULT 'active',
  payload       TEXT NOT NULL DEFAULT '{}',
  progress      TEXT,                              -- list[BatchResult]（仅批处理节点）
  confirmed_by  TEXT,                              -- user / null(auto)
  created_at    TEXT NOT NULL,
  UNIQUE (task_id, stage, stage_version)
);
CREATE INDEX idx_artifact_active ON stage_artifact(task_id, stage, status);

-- ---------- testcase ----------
CREATE TABLE testcase (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES task(id),
  point_id      TEXT NOT NULL,
  stage_version INTEGER NOT NULL,
  lineage       TEXT NOT NULL DEFAULT '{}',
  status        TEXT NOT NULL DEFAULT 'active',
  review_status TEXT NOT NULL DEFAULT 'pending',
  file_path     TEXT NOT NULL,
  content_hash  TEXT NOT NULL,
  title         TEXT NOT NULL,
  trace_refs    TEXT NOT NULL DEFAULT '{}',
  error_info    TEXT,                              -- file_missing / hash_conflict 等对账标记
  created_at    TEXT NOT NULL,                     -- 002 增：首次落库时间，列表游标时间列
  updated_at    TEXT NOT NULL
);
CREATE INDEX idx_case_task_review ON testcase(task_id, status, review_status);
CREATE INDEX idx_case_point ON testcase(point_id);

-- ---------- retrieval_trace ----------
CREATE TABLE retrieval_trace (
  id            TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES task(id),
  graph_run_id  TEXT NOT NULL,
  stage         TEXT NOT NULL,
  stage_version INTEGER NOT NULL,
  node          TEXT NOT NULL,
  batch_id      TEXT,
  query_variant TEXT NOT NULL DEFAULT '{}',        -- QueryVariant JSON
  candidates    TEXT NOT NULL DEFAULT '[]',        -- list[Candidate]
  injected_ids  TEXT NOT NULL DEFAULT '[]',
  referenced_ids TEXT NOT NULL DEFAULT '[]',   -- 白名单内实际引用
  hallucinated_ids TEXT NOT NULL DEFAULT '[]', -- 引用了白名单外 ID
  weak_ref_ids  TEXT NOT NULL DEFAULT '[]',    -- 疑似贴标签（Jaccard 低）
  degraded      TEXT NOT NULL DEFAULT '[]',
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_trace_task ON retrieval_trace(task_id, stage, stage_version);

-- ---------- context_snapshot ----------
CREATE TABLE context_snapshot (
  id                  TEXT PRIMARY KEY,
  task_id             TEXT NOT NULL REFERENCES task(id),
  graph_run_id        TEXT NOT NULL,
  stage               TEXT NOT NULL,
  stage_version       INTEGER NOT NULL,
  node                TEXT NOT NULL,
  batch_id            TEXT,
  items               TEXT NOT NULL DEFAULT '[]',  -- list[InjectedItem 元数据]
  snapshot_path       TEXT,
  total_tokens_est    INTEGER NOT NULL DEFAULT 0,
  budget              INTEGER,
  truncated           INTEGER NOT NULL DEFAULT 0,
  prompt_template_ver TEXT NOT NULL,
  model_ref           TEXT NOT NULL DEFAULT '{}',
  usage               TEXT NOT NULL DEFAULT '{}',  -- {prompt_tokens,completion_tokens,total,aux:{...}}
  latencies           TEXT NOT NULL DEFAULT '{}',
  created_at          TEXT NOT NULL
);
CREATE INDEX idx_snapshot_task ON context_snapshot(task_id, stage, stage_version);

-- ---------- task_event ----------
CREATE TABLE task_event (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL REFERENCES task(id),
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_event_task ON task_event(task_id, id);

-- ---------- review_record ----------
CREATE TABLE review_record (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES task(id),
  testcase_id TEXT NOT NULL REFERENCES testcase(id),
  action      TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_review_task ON review_record(task_id, created_at);

-- ---------- kb_proposal ----------
CREATE TABLE kb_proposal (
  id                 TEXT PRIMARY KEY,
  workspace_id       TEXT NOT NULL REFERENCES workspace(id),
  task_id            TEXT REFERENCES task(id),
  payload            TEXT NOT NULL DEFAULT '{}',
  status             TEXT NOT NULL DEFAULT 'pending',
  confirm_token_hash TEXT,
  idempotency_key    TEXT,
  fail_count         INTEGER NOT NULL DEFAULT 0,  -- ReMe 写失败次数（§18.4）
  confirmed_at       TEXT,
  expires_at         TEXT NOT NULL,
  write_result       TEXT,                          -- ReMeWriter 回查结果
  created_at         TEXT NOT NULL,
  UNIQUE (idempotency_key)
);
CREATE INDEX idx_proposal_ws ON kb_proposal(workspace_id, status);

-- ---------- config（单行 id=1） ----------
CREATE TABLE config (
  id             INTEGER PRIMARY KEY CHECK (id = 1),
  model_config   TEXT NOT NULL DEFAULT '{}',  -- {base_url,api_key,model,temperature,top_p,timeout}
  runtime_config TEXT NOT NULL DEFAULT '{}'
);
INSERT OR IGNORE INTO config (id, model_config, runtime_config) VALUES (1, '{}', '{}');
```

### 3.1.1 迁移 004（Plan-Execute）

```sql
-- server/store/migrations/004_plan_execute.sql
ALTER TABLE task ADD COLUMN current_plan_artifact_id TEXT;
ALTER TABLE stage_artifact ADD COLUMN kind TEXT;   -- 缺省回填为 stage；agent_plan/review_proposal/...
CREATE TABLE IF NOT EXISTS subtask (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  result_artifact_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subtask_task ON subtask(task_id, created_at);
```

约定：`stage_artifact.kind` 与 `stage` 可并存——遗留阶段产物 `kind≈stage`；控制面产物用语义 kind（`agent_plan` / `coverage_design` / `point_plan` / `case_set` / `review_proposal`）。同一 run 内 `(task_id, stage|kind, stage_version)` 仍靠既有唯一索引与 DAO 写入路径保证。

### 3.2 迁移机制

- `server/store/migrations/001_init.sql` 保存上述 DDL；启动时读 `schema_meta`，缺失或版本低则按文件名顺序执行迁移并在同一事务写版本号。
- 迁移文件只增不改；需要改表时新增 `002_xxx.sql`（SQLite 支持的 ALTER 范围内操作；超出范围用"建新表→搬数据→改名→重建索引"模板，模板注释写在迁移文件头）。
- checkpoints.db 不纳入业务迁移，由 LangGraph 库版本负责。

### 3.3 DAO 接口约定

`server/store/models.py` 为每张表定义一个 DAO，构造时传入 `sqlite3.Connection`（async 包装层 `store/db.py` 统一提供 `aexecute/aquery`）。**所有业务查询方法第一个参数为 workspace_id（除配置表与按主键查单行外）**，在 DAO 层强制隔离。

```python
class TaskDAO:
    async def create(self, task: TaskRow) -> None: ...
    async def get(self, task_id: str) -> TaskRow: ...                     # NotFoundError
    async def get_for_update(self, task_id: str) -> TaskRow: ...          # 立即事务(BEGIN IMMEDIATE)
    async def list_by_workspace(self, workspace_id: str, *, status: str|None,
                                cursor: str|None, limit: int) -> Page[TaskRow]: ...
    async def update_status(self, task_id: str, *, status: str,
                            current_stage: str|None = None,
                            error_info: dict|None = None,
                            heartbeat: bool = False) -> None: ...
    async def request_cancel(self, task_id: str) -> None: ...
    async def is_cancel_requested(self, task_id: str) -> bool: ...
    async def heartbeat(self, task_id: str, graph_run_id: str) -> None: ...
    async def list_stale_running(self, before: str) -> list[TaskRow]: ...
    async def start_new_run(self, task_id: str, *, graph_run_id: str,
                            thread_id: str, stage: str) -> None: ...       # 回退落账事务内调用

class ArtifactDAO:
    async def put(self, a: ArtifactRow) -> None: ...
    async def get_active(self, task_id: str, stage: str) -> ArtifactRow|None: ...
    async def get(self, artifact_id: str) -> ArtifactRow: ...
    async def list_active_chain(self, task_id: str) -> list[ArtifactRow]: ...
    async def supersede(self, artifact_id: str) -> None: ...
    async def mark_obsolete(self, task_id: str, stages: list[str],
                            graph_run_id: str) -> int: ...
    async def next_version(self, task_id: str, stage: str) -> int: ...
    async def write_progress(self, artifact_id: str, batches: list[dict]) -> None: ...

class TestcaseDAO:
    async def put_batch(self, rows: list[CaseRow]) -> None: ...           # INSERT OR IGNORE（幂等）
    async def get(self, case_id: str) -> CaseRow: ...
    async def list_by_task(self, task_id: str, *, status: str|None,
                           review: str|None, version: int|None,
                           cursor: str|None, limit: int) -> Page[CaseRow]: ...
    async def update_content(self, case_id: str, *, file_path: str,
                             content_hash: str, title: str) -> None: ...
    async def update_review(self, case_id: str, status: ReviewStatus) -> None: ...
    async def mark_obsolete_by_version(self, task_id: str, versions: list[int]) -> int: ...
    async def mark_error(self, case_id: str, code: str) -> None: ...

class TraceDAO:
    async def append(self, row: TraceRow) -> str: ...                 # 返回 trace id
    async def update_referenced(self, trace_id: str,
                                referenced: list[str], weak: list[str],
                                hallucinated: list[str]) -> None: ... # 生成后闭环回填
    async def list_by_task(self, task_id: str, *, stage: str|None,
                           version: int|None, cursor, limit) -> Page[TraceRow]: ...

class SnapshotDAO:
    async def put(self, row: SnapshotRow) -> str: ...
    async def list_by_task(self, task_id: str, cursor, limit) -> Page[SnapshotRow]: ...
    async def get(self, snapshot_id: str) -> SnapshotRow: ...

class EventDAO:
    async def append(self, task_id: str, type_: str, payload: dict) -> int: ...   # 返回自增 id
    async def list_after(self, task_id: str, after_id: int, limit: int = 500) -> list[EventRow]: ...
    async def purge_before(self, before: str) -> int: ...

class MessageDAO / ProposalDAO / ConversationDAO / WorkspaceDAO / ReviewDAO / ConfigDAO: ...
```

分页统一：`Page[T] = {items: list[T], next_cursor: str|None}`，游标为 base64(`{last_id}|{last_created_at}`)；无更多数据时 next_cursor=None。

---

## 4. 文件格式规范

### 4.1 目录与路径

```
data/
├── app.db / app.db-wal / app.db-shm
├── checkpoints.db(-wal/-shm)
└── workspaces/{workspace_id}/{task_id}/
    ├── requirement.md
    ├── snapshots/{stage}/v{n}/{batch_id}-{node}-{snapshot_id}.jsonl
    └── cases/v{stage_version}/{case_id8}--{point_id8}--{slug}.md
```

- 根目录可由环境变量 `TESTER_AGENT_DATA_DIR` 覆盖，默认仓库内 `data/`。
- 所有写入路径必须经 `workspace_files.py` 拼出（禁止业务代码自己 join），路径越界检查（resolve 后必须仍在对应 task 目录下）。
- slug：标题 → NFKC → 小写 → 非字母数字转 `-` → 折叠连字符 → 截断 40 字符；**slug 冲突时追加 `-2/-3`，不靠 slug 保证唯一性**（唯一性由 case_id 前缀保证）。

### 4.2 用例 Markdown 格式（v1 模板，**Q1 一期定稿**）

```markdown
---
case_id: 9f3a1c2b7e4d...
point_id: pt-1-3
stage_version: 2
priority: P1
content_hash: sha256:...
trace_refs:
  clause_ids: [h2-1-h3-2]
  entry_ids: [ent_8812, ent_3301]
  point_ids: [pt-1-3]
---

# 退款超时后状态回滚校验

## 前置条件
- 订单处于"已支付待发货"状态
- 支付通道 mock 超时开关可开启

## 步骤
1. 用户发起退款，mock 支付通道返回超时
   - 预期：前端提示"处理中"，本地订单状态保持"已支付"
2. 通道异步回调失败
   - 预期：订单状态回滚为"已支付"，退款单标记失败
3. 用户再次发起退款
   - 预期：正常受理，不产生重复退款单

## 测试数据
订单金额 0.01 元（沙箱）

## 说明
_本文件由用例智能体生成，评审状态以平台为准；手工编辑后平台将提示 hash 变化。_
```

规则：

1. YAML front-matter（`---` 包裹）为元数据区，平台编辑时保留；正文模板段标题固定（`## 前置条件 / ## 步骤 / ## 测试数据 / ## 说明`，可缺省可加段）。
2. 步骤与预期：v1 采用"步骤行 + 紧跟两个空格换行（软换行）书写 `- 预期：...`"或"步骤下一行 `   - 预期：`"二选一，解析器两者兼容（列表项内嵌缩进子项）。
3. `content_hash = sha256(去掉 front-matter 中 content_hash 字段后的整文件规范化文本)`；规范化：CRLF→LF、末尾去空行。front-matter 里写 hash 是为外部搬动文件后仍可自证（对账时以 DB hash 为准，front-matter 仅辅助）。
4. 解析为 `CaseFileContent` 用严格但宽容的 Markdown 解析（mistune + 自定义渲染），解析失败不阻断读取，原始 MD 仍可渲染/编辑，仅结构化字段（steps）降级。

### 4.3 requirement.md

- 任务创建时原文逐字写入（UTF-8，LF），路径与 hash 存 `task.requirement_ref`。
- intake 在文件**同目录**写 `requirement.clauses.json`（非 MD 内联，避免污染原文）：`list[ClauseRef]` 含每个条款在原文中的 `[start_offset, end_offset)`（字节偏移，扩展 ClauseRef 运行期字段，不入 DB）。该文件为可再生缓存，丢失后由 intake 重算（clause_id 确定性生成，重算后 ID 稳定）。

### 4.4 快照 JSONL

每行一个 JSON 对象，UTF-8：

```json
{"entry_id":"ent_8812","entry_version":"2026-09-20T03:11:00Z","title":"退款超时规则","tokens_est":412,"position":0,"content":"……段落正文……"}
```

- 写入顺序 = position 顺序；append 前记录 `fp.tell()`（字节偏移）与编码后字节数，落入 context_snapshot.items。
- 同批次同节点一个文件；文件名 `{batch_id or 'na'}-{node}-{snapshot_id8}.jsonl`。
- meta/off 档不产生文件；off 档连 DB snapshot 行也不写（retrieval_trace 始终写）。

---

## 5. 存储层接口（store/workspace_files.py）

文件存储是独立于 DAO 的组件，统一封装"原子写、偏移读、路径安全、哈希"。

```python
class FileStore:
    def __init__(self, data_dir: Path): ...

    # ---- 需求 ----
    async def save_requirement(self, ws: str, task: str, md: str) -> FileRef: ...
    async def read_requirement(self, ws: str, task: str) -> str: ...
    async def read_clause(self, ws, task, span: tuple[int,int]) -> str:
        """按字节偏移读单条款原文（span 来自 clauses 缓存）。"""

    # ---- 用例（原子提交）----
    async def write_case(self, ws: str, task: str, version: int,
                         content: CaseFileContent) -> WrittenCase:
        """渲染 MD → tmp 同目录写盘 fsync → rename；返回 file_path/content_hash。
           目标路径已存在且内容 hash 相同 → 视为已提交，直接返回（幂等重放）。"""

    async def read_case(self, ws: str, task: str, rel_path: str) -> str: ...
    async def overwrite_case(self, ws, task, rel_path, md: str,
                             expected_hash: str | None) -> WrittenCase:
        """手工编辑：expected_hash 非空时先读盘比对，不一致抛 FILE_CONFLICT。"""

    # ---- 快照 ----
    def open_snapshot_writer(self, ws, task, stage: str, version: int,
                             node: str, batch_id: str | None) -> "SnapshotWriter": ...
    async def read_snapshot_line(self, ws, task, rel_path: str,
                                 offset: int, length: int) -> str: ...

    # ---- 维护 ----
    async def list_case_files(self, ws, task) -> list[PathInfo]: ...
    async def soft_cleanup(self, retention_days: int) -> CleanupReport: ...

class SnapshotWriter:                     # 同步 IO（append 很快），节点内顺序使用
    rel_path: str
    def append(self, line: dict) -> tuple[int, int]:
        """写入一行，返回 (byte_offset, byte_length)。"""
    def close(self) -> None: ...

@dataclass
class WrittenCase:
    file_path: str; content_hash: str
```

实现约束：

- 临时文件后缀 `.tmp.{uuid}`，与目标在同一文件系统（同目录）保证 rename 原子；写完 `flush()+os.fsync()`。
- `write_case` 渲染 MD 时 front-matter 先写占位 hash，计算"规范化文本"的 sha256 后再二次渲染写入；读侧校验同一算法（§4.2 规则 3）。
- 路径越界统一抛 `PathEscapeError`（→ INTERNAL，属于编程错误而非用户错误）。
- 对账（Reconciler）与清理逻辑在 `runtime/maintenance.py`，只调用 FileStore + DAO 公共接口，伪代码见 §11.3。

---

## 6. 运行时详细设计（server/runtime/）

### 6.1 组合根与服务上下文

```python
@dataclass
class AppContext:                          # 进程内唯一组合根，FastAPI lifespan 构造
    db: Database                           # app.db 连接池（单 worker 下 1 写连接 + N 读连接）
    file_store: FileStore
    llm: LLMClient
    reme_factory: ReMeReaderFactory        # ws.kb_config -> ReMeReader（带镜像缓存）
    graphs: GraphRegistry
    bus: EventBus
    registry: TaskRegistry
    config: ConfigStore

@dataclass
class TaskContext:                         # 单次图运行内的依赖包，注入每个节点
    app: AppContext
    task: TaskRow
    run_id: str
    daos: DAOs                             # 一组 DAO
    files: FileStore
    reader: ReMeReader                     # 本工作区只读适配器（检索隔离边界）
    emit: Emitter                          # (type, payload) -> EventBus + task_event
    snapshot_level: str
```

### 6.2 EventBus 与 SSE 线协议

```python
class EventBus:
    def __init__(self, events: EventDAO): ...
    async def emit(self, task_id: str, type_: str, payload: dict) -> int:
        event_id = await self.events.append(task_id, type_, payload)  # 先落库
        for q in self._subs.get(task_id, ()): q.put_nowait((event_id, type_, payload))
        return event_id

    async def subscribe(self, task_id: str, after_id: int = 0):
        # 先回放 task_event (id > after_id)，yield 完历史后注册 queue 实时转发；
        # 注册与回放之间用 per-task 条件锁闭窗：先占座再回放，防漏。
        q = asyncio.Queue(maxsize=1000)
        ...
        yield event
```

SSE 响应（`GET /tasks/{id}/events`）：

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive

id: 102
event: node_start
data: {"node":"point_write","stage_version":1,"batch_id":"b0"}

: ping
```

- 每 15s 发一行 `: ping` 注释心跳；服务端不依赖心跳判活。
- 客户端重连带 `Last-Event-ID: 102`（不支持自定义头的环境用 `?after_event_id=102`）。
- queue 满（消费者卡住）时丢弃实时帧但**不丢库**，客户端下次重连靠回放补齐；服务端记一条 warning 日志。

### 6.3 任务状态机

合法迁移表（API 层与 Runner 共用 `validate_transition()`，非法迁移 → `TASK_STATE_CONFLICT`）：

| 当前状态 | 事件 | 目标状态 | 动作要点 |
|---|---|---|---|
| waiting_input（含"已建未启动"初始态） | run | running | 获取锁、首 invoke；澄清答复与首次启动共用此迁移 |
| running | 图到达 CP 中断 | waiting_confirm | 写 artifact、发 checkpoint_waiting |
| running | 节点挂澄清 | waiting_input | 发 clarification_needed |
| running | 图正常结束 | completed | coverage artifact active |
| running | 节点不可恢复错误 | failed | 写 error_info(retryable)、发 task_error |
| running | cancel 标志在批次边界被检查 | aborted | 已完成批次产物保留 |
| waiting_confirm | confirm/modify | running | 校验 artifact_id+version，写 user_revised 版本后 resume |
| waiting_confirm | rollback | running | 走 §11.2 回退协议，派生新 run |
| waiting_input | answer | running | 写 message 后 resume |
| waiting_confirm / waiting_input | cancel | aborted | 直接生效（无在飞批次） |
| running | cancel 请求 | cancelling | 仅置标志；由批次边界转 aborted |
| cancelling | 批次边界检查 | aborted | |
| failed | run（重试） | running | 从最近批次游标恢复（R3） |
| aborted | run | running | 同上；waiting_* 中断点也允许直接续跑 |
| completed | rollback | running | 评审期允许回退（US8.3） |
| completed | regenerate | running | 增补批次执行完回 completed（§7.6） |
| waiting_* 超过 7 天被触达 | — | 原状态不变 | 响应中带 stale 标记，前端弹变更确认 |

### 6.4 TaskRegistry 与 Runner

```python
class TaskRegistry:
    _owners: dict[str, str]                  # task_id -> owner_token
    _locks:  dict[str, asyncio.Lock]
    _tasks: dict[str, asyncio.Task]

    async def acquire(self, task_id: str) -> str:
        """返回 owner_token；已被占用（且心跳新鲜）→ TaskBusyError(409)。"""

    async def release(self, task_id: str, token: str) -> None: ...
    def is_running(self, task_id: str) -> bool: ...

class Runner:
    async def start(self, task_id: str) -> RunHandle:
        token = await self.registry.acquire(task_id)          # 409 见 §14
        t = asyncio.create_task(self._run(task_id, token), name=f"task-{task_id}")
        self.registry.attach(task_id, t)
        return RunHandle(events_url=f"/api/v1/tasks/{task_id}/events")

    async def _run(self, task_id: str, token: str) -> None:
        ctx = await self._build_ctx(task_id)                  # 含配置快照（R21 冻结）
        hb = asyncio.create_task(self._heartbeat_loop(ctx))
        try:
            graph = ctx.app.graphs.get("case_designer")
            await graph.ainvoke(
                initial_or_resume_input(ctx),
                config={"configurable": {"thread_id": ctx.task.langgraph_thread_id},
                        "callbacks": [EventBridge(ctx)]},      # 节点事件桥到 EventBus
                ctx=ctx,
            )
            await self._settle_terminal(ctx)                  # 按图结果置 completed/failed
        except GraphInterrupt:
            await ctx.daos.task.update_status(..., status=_interrupt_kind(ctx))  # waiting_*
        except Exception as e:
            await self._fail(ctx, e)                          # 错误码映射 §14
        finally:
            hb.cancel(); await self.registry.release(task_id, token)
```

要点：

1. **锁的崩溃安全**：锁在内存，进程死了锁自然消失；存活判据是 DB 心跳（acquire 前先查 heartbeat，新鲜才拒）。内存锁防同进程并发，心跳防跨重启误判，两者分工。
2. **心跳循环**：每 10s `UPDATE task SET runner_heartbeat=now WHERE id=? AND graph_run_id=?`；更新 0 行说明任务已被 Reaper/取消改判，Runner 应在下个批次边界自杀。
3. **取消检查**：`ctx.cancelled()` 辅助方法读 cancel_requested（节点在每批开头调一次），为真抛 `TaskCancelled`（Runner 内部异常 → aborted）。
4. **EventBridge**：LangGraph callback 把节点开始/结束、自定义流事件转成 §10.5 的 SSE 事件；LLM token 流通过图的 stream writer（`get_stream_writer()`）透出。

### 6.5 Reaper 与启动序列

FastAPI lifespan 启动顺序（任一步失败阻断启动并明确报错）：

```
1. 打开/迁移 app.db（WAL）；初始化 FileStore（目录校验）
2. 加载 config；构造 LLMClient/ReMeFactory（不做远端探活）
3. GraphRegistry 编译主图，checkpointer 指向 checkpoints.db
4. Reaper.reap_on_startup()：
     UPDATE task SET status='failed',
       error_info={code:'INTERRUPTED_BY_RESTART', retryable:true}
     WHERE status IN ('running','cancelling')
       AND (runner_heartbeat IS NULL OR runner_heartbeat < now-120s)
   （cancelling 同样改 failed，由用户决定是否重跑；waiting_* 不动）
5. maintenance.lazy_purge()：清理过期 event/快照/提案（超保留期）
6. 挂载路由 + StaticFiles，开始服务
```

关闭序列：SIGTERM → uvicorn 停止接单 → 向所有在飞 asyncio.Task 发取消信号并等待最多 30s（节点在批次边界或当前 LLM 调用结束处退出）→ 关闭 DB。**不做强制 kill 中途写盘**，原子写协议保证现场可恢复。

### 6.6 幂等键服务

- `IdempotencyStore`：内存 TTL map + DB 落表复用（kb_proposal 已唯一索引；其余端点落一张轻量 `idempotency_record(key, endpoint, request_hash, response_code, response_body, created_at)`，DDL 迁移 002 预留，一期可先用内存 24h TTL——单机单 worker 内足够；文档标注此简化）。
- 命中同 key 且 request_hash 相同：重放首次响应；request_hash 不同：409。

---

## 7. LangGraph 图详细设计

> **生产路径 = 控制环**（`graph/control/` + `tools/capabilities.invoke_capability`）。  
> 阶段节点函数是能力实现真相源，不再作为 StateGraph 拓扑节点。完整行为见 plan-execute-reflexion 设计文档。

### 7.1 State 定义与图装配

```python
# server/graph/state.py — 控制面字段 + 阶段产物镜像（能力写入后仍可读）
class TaskState(TypedDict, total=False):
    task_id: str
    graph_run_id: str
    workspace_id: str
    clauses: list[dict]
    link_plan: dict | None
    point_plan: dict | None
    case_batch: dict | None
    coverage: dict | None
    clarification_questions: list[dict]
    current_stage_version: dict[str, int]
    batch_cursor: dict[str, dict]
    # ---- Plan-Execute ----
    agent_plan: dict
    plan_cursor: str | None
    artifacts: dict                  # artifact_id → {kind, version, payload, confirmed_by}
    subtask: dict | None
    reflection_log: list[dict]
    human_gates: dict                # {link, point, review} bool
    reflect_counts: dict
    _reflect_decision: str

# 生产：GraphRegistry.create_production → build_control_graph / build_graph
def build_control_graph(checkpointer=None, *, caps=None) -> CompiledStateGraph:
    g = StateGraph(TaskState)
    g.add_node("plan", plan_node)
    g.add_node("dispatch", dispatch_node)
    g.add_node("execute_step", execute_step_node)   # async；config.configurable.ctx；caps 可覆盖能力
    g.add_node("await_human", await_human_node)     # interrupt() when gate
    g.add_node("reflect", reflect_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "dispatch")
    g.add_conditional_edges("dispatch", route_after_dispatch,
                            {"execute": "execute_step", "end": END})
    g.add_edge("execute_step", "await_human")
    g.add_edge("await_human", "reflect")
    g.add_conditional_edges("reflect", route_after_reflect,
                            {"dispatch": "dispatch", "plan": "plan",
                             "execute": "execute_step", "end": END})
    return g.compile(checkpointer=checkpointer)
```

- `execute_step`：非 `review_*` 步调用 `invoke_capability(kind, ctx, state)` → 真实节点函数；`review_*` 构建 `ReviewProposal` artifact。无 ctx 时 stub（单测）。
- `await_human`：`requires_confirm` 且对应 `human_gates` 开启且未 `confirmed_by=user` 时 `interrupt({gate_kind, artifact_id, step_id, kind})`；resume 后同步 `link_plan`/`point_plan`（`legacy_state_from_artifact`）。
- `human_gates` 默认 `{link:true, point:true, review:true}`；全关可跑通 stub 闭环（场景测）。
- waiting_input 仍用节点内 `interrupt()`（intake/link_identify 澄清）；answer API `Command(resume=answers)`（§7.6）。

### 7.2 节点契约总表（能力函数；由控制环 `execute_step` / 遗留图 `wrap` 调用）

| 节点 / PlanStep.kind | 输入（state/文件） | LLM 调用 | 检索 | 产物与落库 | 中断 |
|---|---|---|---|---|---|
| intake / `intake_parse` | requirement.md | 歧义检测 1 次（可关） | 否 | task.clauses；artifacts.kind=`clauses` | interrupt() 提问 |
| link_identify / `coverage_design` | clauses + 索引树 | 1 次（结构化） | 子图 index_line 档 | stage_artifact(link_identify) + artifacts | interrupt()；人门 `plan_confirm` |
| point_write / `point_design` | 已确认 LinkPlan、按故事分批 | 每批 1 次 | 每批子图 passage 档 | artifact(point_write) + progress | 人门 `plan_confirm` |
| case_generate / `case_generate` | PointPlan、按测试点分批 | 每批 N 次生成 | 每批子图 passage 档 | MD + testcase + trace/snapshot | 否 |
| coverage_check（工具/矩阵） | clauses + PointPlan + cases | 补充生成有上限（遗留图） | 随补充批次 | CoverageMatrix；PE 下改由 review_coverage 提案驱动补例 | 否 |
| review_* | 上游 artifacts / cases | 子任务或 stub builder | 只读工具为主 | ReviewProposal artifact；人门 `review_decision` | interrupt() |

每个节点统一出口返回**状态增量 dict**（LangGraph reducer：plan 类字段整体替换；batch_cursor 按 node key merge）；节点不直接改 state 其他字段。

### 7.3 批次执行器（批处理节点共用骨架）

```python
async def run_in_batches(ctx, node, units, version_getter, worker, *, size):
    """
    units: 本节点全部工作单元（story_id 或 point_id 列表，顺序稳定）
    worker(ctx, batch_id, unit_subset) -> list[产物]
    负责：取消检查、断点跳过、幂等、事件、progress 落盘。
    """
    artifact = await ctx.daos.artifact.get_active(ctx.task.id, node)
    progress  = BatchProgress.from_json(artifact.progress)
    cursor    = ctx.state["batch_cursor"].get(node) or start_cursor(units)

    for start in range(cursor.next_index, len(units), size):
        if await ctx.cancelled():
            raise TaskCancelled()
        subset = units[start:start + size]
        batch_id = f"b{start // size}"
        idem = deterministic_key(ctx.task.id, ctx.run_id, node, batch_id,
                                 cursor.idempotency_nonce)
        if progress.get(batch_id) == "done":
            continue                                      # 崩溃恢复：跳过已完成批
        progress.mark_started(batch_id, subset, idem)
        await ctx.daos.artifact.write_progress(artifact.id, progress.dump())
        await ctx.emit("batch_progress", {"node": node, "batch_id": batch_id,
                                          "done": False, "total": len(units)})
        try:
            results = await worker(ctx, batch_id, subset)   # 内部全走原子写+幂等行
        except Exception as e:
            progress.mark_failed(batch_id)
            await ctx.daos.artifact.write_progress(artifact.id, progress.dump())
            raise                                        # Runner 置 failed，批可整批重做
        progress.mark_done(batch_id, [r.id for r in results])
        await ctx.daos.artifact.write_progress(artifact.id, progress.dump())
        await ctx.emit("batch_progress", {"node": node, "batch_id": batch_id,
                                          "done": True, "total": len(units)})
    cursor.next_index = len(units)
```

幂等要点：

- **case_id 确定性**：`case_id = uuid5(ID_NS, f"{task_id}|{stage_version}|{batch_id}|{point_id}|{seq}")`，seq 为模型输出在该批中的序号。重跑同一批产出相同 ID；`testcase` 行 `INSERT OR IGNORE`，文件同名同内容 rename 覆盖（hash 相同即无副作用）。
- **非确定性防护**：批真正失败重做后若模型输出条数/顺序变化，可能留下上一轮的多余行——worker 在写批前先按 `(task_id, stage_version, batch_id)` 维度登记本批 idem 集合，完成时把"本批 idem 集合内、本次未产出"的旧行置 obsolete（孤儿文件留盘由保留期清理）。
- `idempotency_nonce` 随新 run 重新生成：回退重跑是新 run，允许产生新版本而非复用旧批结果；**同 run 内崩溃恢复**nonce 不变，严格幂等。

### 7.4 Prompt 契约

所有 Prompt 模板放 `server/prompts/{node}.{role}.md`，文件头 YAML 声明 `version`；agent.config 可覆盖模板目录。统一系统约束（所有生成类 Prompt 共享一段）：

```
你是测试设计助手。只依据<knowledge>中给出的知识与<requirement>条款生成；
引用知识时只能使用 <knowledge> 条目提供的 [ID:xxx]，禁止杜撰 ID；
无法判断时输出 clarification 请求而不是假设；输出必须是符合给定 JSON Schema 的 JSON。
```

**① link_identify 输出 schema（LLM 直出 JSON，经 Pydantic 校验）**

```json
{
  "links": [{"link_id":"", "title":"", "summary":"", "hit": true,
             "entry_id": null, "confidence": 0.0}],
  "stories": [{"story_id":"", "link_id":"", "title":"", "summary":"", "hit": true,
               "entry_id": null, "confidence": 0.0, "rationale":"",
               "related_clause_ids":[]}],
  "new_suggestions": [],
  "clarifications": [{"question":"", "options":[]}]
}
```

- 输入：`task.clauses` 全量索引（标题路径+anchor，非正文）+ 需求前 N 字摘要（S3 定 N）+ 子图注入的索引摘要块（每条带 `[ID]`）。
- link_id/story_id 规则：命中项由模型回填知识库 ID；新增项由 API 侧统一改写为 `new-link-{n}/new-story-{n}`（不信模型自造的临时 ID）。

**② point_write 输出 schema（每批：一组故事）**

```json
{"points": [{"point_id":"", "story_id":"", "title":"", "angle":"",
             "method":"", "clause_ids":[], "source_entry_ids":[], "priority":"P1"}],
 "clarifications": []}
```

point_id 由 API 侧确定性赋值（`pt-{story 在 LinkPlan 中的序号}-{批内序号}`），模型只回填 story_id；source_entry_ids 必须是白名单子集。

**③ case_generate 输出 schema（每点/每批）**

```json
{"cases": [{
  "title":"", "priority":"P1",
  "preconditions":[""],
  "steps":[{"seq":1,"action":"","expect":""}],
  "test_data": null,
  "trace_refs": {"clause_ids":[], "entry_ids":[]}
}]}
```

- 输入：本批测试点 + 每点相关条款原文（按需读文件）+ 子图 passage 注入块。
- 解析后由代码补 case_id、point_id 关联（按输出顺序与请求点顺序对齐；模型对每个 point 输出一个 `cases` 数组，prompt 要求按 `<point id="">` 分段）。

**④ 检索辅助 Prompt**

- `retrieve/multi_query.md`：输入单条意图文本，输出 `{"queries":[{"channel":"keyword","text":""}, ...]}`，数量取 config.query_paths−1（raw 路不经 LLM）。
- `retrieve/rerank.md`：批量打分，输出 `[{"entry_id":"","score":0.0,"reason":""}]`；为控成本，候选超过 30 条时按通道分桶每桶最多送 10 条（桶内按召回分），打分后合并。

**⑤ 结构化失败处理**：JSON 解析/校验失败 → LLMClient 自动带校验错误重请 1 次（§9.3）；仍失败：该节点 failed（retryable=true, code=LLM_BAD_OUTPUT），**绝不**带着半成品产物过检查点。

### 7.5 关键节点处理逻辑

**① intake（条款切分）**

1. 确定性切分：按 Markdown ATX 标题（`##`~`######`）切分，忽略代码块/引用块内的标题；无标题文档整体为一个 clause（clause_id=`root`）。
2. clause_id 规则：标题路径各层的同级序号拼接，如 `h2-1-h3-2`；同级序号按"同标题层级、同父路径下出现次序"计数。
3. 每条款算 text_hash（规范化原文 sha1），写 requirement.clauses.json（含偏移），clauses 索引写 task 行。
4. 歧义检测（可由 agent.config 关闭）：LLM 判断需求是否缺少关键测试信息（入口/角色/数据/约束），有则 interrupt() 挂澄清；澄清答复以 message 落库后重跑 intake（条款 ID 保持，见 §3.2 tech-design）。

**② link_identify**

1. 子图 index_line 档检索（allowed_types=[LINK_INDEX]，注入 ≤200 硬上限）；
2. LLM 产出 LinkPlan；API 侧重写临时 ID、校验 hit 项 entry_id 必须存在于注入白名单（不在则降级为 hit=false 并记 degraded）；
3. 写 artifact(active, confirmed_by=null)；图在 cp1_gate 前中断，Runner 置 waiting_confirm。

**③ point_write / case_generate**：走 §7.3 批次骨架；point_write 单元=已确认 stories（按 link 分组排序），case_generate 单元=active 测试点（默认每批 5 点）。每批独立调检索子图（config 按 §8.1 取），每批独立写 trace/snapshot（batch_id 贯穿）。

**④ coverage_check**

1. 程序化矩阵：对每条 active clause，检查是否有 point 的 clause_ids 命中（点覆盖）、该 point 下是否有 active case（例覆盖）；evidence 取对象标题。
2. uncovered_clauses 非空且轮次 <2：以未覆盖条款为虚拟单元调一次 case_generate 单批函数（走同一写入路径，batch_id=`sup{round}`），重算矩阵。
3. 达上限仍有未覆盖：CoverageMatrix.degraded=true，发 `coverage_ready` 时带 warning，任务仍可 completed（告警降级，tech-design §4.2）。

### 7.6 检查点确认 / 澄清答复 / 回退 / 重生成的入口处理

| 入口 | API 层动作 | 图恢复方式 |
|---|---|---|
| confirm（遗留：`stage`∈{link_identify,point_write} + `expected_version`） | 校验 active；confirm→`confirmed_by=user`；modify→supersede+v+1 user_revised + message(checkpoint_revision) + `aupdate_state` 写 plan 字段 | Runner.start；越过静态 gate |
| confirm（PE：`gate_kind` 缺省 `plan_confirm`） | `HumanDecision` → `apply_human_decision`；落确认、可选改写 plan artifact；`aupdate_state` 同步 `artifacts` + 遗留 `link_plan`/`point_plan` | Runner.start(resume=决策 payload) 越过 `await_human` interrupt |
| confirm（`gate_kind=review_decision`） | 同 PE；adoption 写 case review_status；coverage/quality 可插入 repair/case_generate steps；`reject_rerun` 将评审步置 pending | 同上 |
| answer | 写 message(clarification_qa) | `Command(resume=answers)` |
| rollback | §11.2；新 run、新 thread | 遗留：`run_from_stage` / 派生 thread；PE：入口为修订 AgentPlan + 继承 artifacts（后续完善） |
| regenerate | 调 case_generate worker（batch_id=`regen-*`） | 不 invoke 主图 |

> `gate_kind` 可省略，服务端默认 `plan_confirm`，兼容旧客户端（e2e / StageConfirmPage）。

---

## 8. 检索子图详细设计

### 8.1 阶段配置（初值，S5 标定后固化）

```python
RETRIEVAL_PRESETS = {
    STAGE_LINK_IDENTIFY: RetrievalConfig(
        stage=STAGE_LINK_IDENTIFY, query_paths=3, recall_topk=50,
        inject_limit=200, inject_form="index_line",
        allowed_types=[EntryType.LINK_INDEX], token_budget=12_000),
    STAGE_POINT_WRITE: RetrievalConfig(
        stage=STAGE_POINT_WRITE, query_paths=4, recall_topk=40,
        inject_limit=20, inject_form="passage",
        allowed_types=[BUSINESS, FLOW_CASE, DEFECT], token_budget=16_000),
    STAGE_CASE_GENERATE: RetrievalConfig(
        stage=STAGE_CASE_GENERATE, query_paths=3, recall_topk=30,
        inject_limit=15, inject_form="passage",
        allowed_types=[API, DB, DEFECT, BUSINESS], token_budget=12_000),
}
```

意图文本构造（检索的 query 来源，不用自由对话）：

- link_identify：需求条款标题路径 + anchor 拼接；
- point_write：已确认 story.title + summary + 相关 clause 原文摘要；
- case_generate：point.title + angle + story.title。

### 8.2 算子签名与数据流

```python
async def retrieve_pipeline(
    ctx: TaskContext, cfg: RetrievalConfig, intent: str, *,
    batch_id: str | None = None,
) -> RetrievalOutcome:
    """节点内唯一入口。产出注入块 + 留痕（trace/snapshot）。"""

class RetrievalOutcome:
    items: list[InjectedItem]            # 已按锚点排序、已截断
    injected: list[str]                  # entry_id 顺序
    degraded: list[DegradedStep]
    latencies: dict[str, int]
    token_est: int
    truncated: bool

# ---- 六算子（函数式，便于单测与 eval 复用）----
async def multi_query(llm, intent, n) -> list[QueryVariant]:
    """raw 路恒在（index 0）；其余 n-1 路由 LLM 生成。LLM 失败 → 仅 raw，记 degraded。"""

async def parallel_recall(reader, queries, topk, types) -> list[Candidate]:
    """asyncio.gather 各路（信号量限并发）；按 (entry_id, entry_version) 并集，
    保留最高分与 source_channel 列表；单路失败填 error 候选不影响他路。"""

async def meta_filter(cands, types, scope) -> list[Candidate]:
    """scope=当前确认的 link/story ID 集合。命中过滤规则被剔除的候选 kept=false,
    drop_reason='filtered_type'/'filtered_scope'。能力不可用走 §8.4 镜像降级。"""

async def rerank(llm, cands, intent, limit) -> list[Candidate]:
    """>30 条分桶打分（§7.4④）；LLM 失败→规则分（标题/意图 token 重叠 *类型权重）。
    超出 limit：drop_reason='rerank_cutoff'。"""

async def passage_extract(cands, reader, form) -> list[InjectedItem]:
    """index_line：只取 title+summary，不拉正文。
    passage：结构化切分（按条目内标题/段落），选与意图重叠最高的 1~2 段；
    无结构信息→固定窗口（前 800 字）。LLM 抽取仅作 S5 后可选增强。"""

async def assemble(items, cfg) -> RetrievalOutcome:
    """token 估算（中英混合：len(cjk_chars)+len(ascii_words)*1.3）→
    超 token_budget 或 inject_limit 截断（drop_reason='budget_cut'）→
    锚点排序（§8.5）→ 去重（同 entry_id+version 只留最高分位置）。"""
```

### 8.3 trace / snapshot 写入时机

- 每算子结束写**内存 trace builder**（候选全集、各步计数、延迟）；管线结束一次性 `TraceDAO.append`（一行 = 一个批次一次管线调用，query_variant 存各路文本，candidates 存最终带 kept/drop_reason 的全集）。
- snapshot 在 assemble 后、LLM 生成调用前写：
  - meta：SnapshotDAO.put（items 含 tokens/position；无文件）；
  - full：先 SnapshotWriter 逐行 append 得到 offset/length 回填 items，再 put；
  - off：不写。
- 生成结束后回填该行 `referenced_ids`（产物引用白名单解析结果，§8.6）——trace 行采用"先 append 拿 id、结束 update 回填"的两次写。
- usage：管线自身辅助调用（multi_query/rerank）累加为 `usage.aux.retrieval = {calls, prompt_tokens, completion_tokens}`，生成主调用为 usage 主体。

### 8.4 ReMe 能力位与降级判定

`ReMeReader.capabilities()` 在连接测试与任务启动时探测并缓存：

```python
class ReMeCaps(BaseModel):
    metadata_filter: bool       # search 是否支持 types/scope 参数
    entry_version:  bool        # 是否返回 updated_at/内容 hash
    passage_api:    bool        # 是否支持段落级检索；False 则全量拉回本地切
```

降级决策（每次管线构造时读 caps，不写死全局）：

| 能力缺失 | 行为 |
|---|---|
| metadata_filter=False | 用 `IndexMirror`（启动刷新 + 每 60min TTL 的只读索引树缓存）在本地过滤；镜像空则跳过过滤全量进 rerank，degraded 记录 |
| entry_version=False | entry_version 退化为本地计算的 `h-{contenthash前12位}`（拉回内容时算），面板提示"版本由本地计算" |
| passage_api=False | recall 后批量 get_entry 拉正文，本地结构化切分；token 成本计入 aux |

### 8.5 缓存与注入排序

- `RetrievalCache`（进程内 LRU，容量 512 条，key 见下，value 带 entry_version 校验）：
  - recall：`sha1(ws|kb_id|query.text|topk|types)` → 候选列表；
  - rerank/extract：`entry_id|entry_version|intent_hash` → 分数/段落。
  缓存只在**同一 graph_run_id 内**复用（run 开始即清空该 task 分区），避免跨 run 陈旧结果。
- 排序：`sorted by score desc` 后执行锚点放置——第 1 名首位、第 2 名末位、第 3 名次位、第 4 名次末位……其余按序填中间；同一条目不重复。排序策略标识 `anchor-v1` 记入 snapshot（latencies 旁加 `ordering_strategy` 字段，随 002 迁移或直接并入 latencies JSON）。

### 8.6 引用闭环校验（产物侧）

```python
def close_loop(generated_texts, white_list: set[str]) -> ClosedLoop:
    found = parse_ids(generated_texts)               # 正则 [ID:xxx]
    hallucinated = found - white_list                # → warning，不计 referenced
    referenced  = found & white_list
    # 疑似贴标签：对每个 referenced 条目，取其注入 passage 与产物对应 case 文本，
    # 算字符级 bigram Jaccard；<0.08 标 weak_reference（仅标记）
    return ClosedLoop(referenced, injected_not_used=white_list - referenced,
                      hallucinated, weak_refs)
```

归因指标查询（调试面板与 eval 共用 SQL 口径）：

- 未召回：目标条目不在任意 trace.candidates[].entry_id；
- 被裁：candidates 中 kept=false，按 drop_reason 分组计数；
- 未用上：injected_ids − referenced_ids（weak_refs 单列，不改变主口径）。

### 8.7 ad-hoc playground

`POST /workspaces/{id}/retrieval/playground` 直接调 `retrieve_pipeline`，区别仅在于：用临时 TaskContext（task_id=`playground-{uuid}`、不落任何业务表），trace 结果**直接在 HTTP 响应返回**（漏斗各步计数 + 候选 + 注入 + degraded），snapshot_level 强制 off。供调优者快速试验 query/配置（R17）。

---

## 9. 接入层契约（server/adapters/）

### 9.1 ReMeReader（只读）

```python
class ReMeReader(Protocol):
    caps: ReMeCaps
    async def search(self, query: str, *, top_k: int,
                     types: list[str] | None = None,
                     scope: dict | None = None) -> list[Entry]: ...
    async def get_entry(self, entry_id: str) -> Entry: ...
    async def list_index_tree(self) -> IndexTree:
        """链路→故事两级树（title/一句话/entry_id/version/归属），索引镜像与 /kb/tree 共用。"""

class Entry(BaseModel):
    entry_id: str
    entry_version: str
    title: str
    content: str
    entry_type: EntryType
    link_id: str | None
    story_id: str | None
    updated_at: str | None
    raw: dict                      # ReMe 原始字段，适配层外不消费

class ReMeReaderFactory:
    async def for_workspace(self, kb_config: dict) -> ReMeReader:
        """按 (target, kb_id) 缓存实例；SDK/HTTP 两种实现实现同一 Protocol。"""
```

SDK 适配（S1 前为唯一优先实现）放 `adapters/reme_sdk.py`；若 S1 确认只能走服务模式，新增 `reme_http.py`，工厂按 kb_config.mode 选择。**Reader 不持有任何写方法**，从类型上消灭图内写库路径。

### 9.2 ReMeWriter（只被 api/kb.py import）

```python
class ReMeWriter(Protocol):
    async def write_proposal(self, token: OneTimeToken, proposal: KbPayload) -> WriteResult: ...

class WriteResult(BaseModel):
    ok: bool
    remote_ref: str | None         # 写入后的条目 ID/版本
    verified: bool                 # 是否经过写入后回查（S6）
    error_code: str | None
```

一次性令牌：proposal 创建时生成随机 32 字节 token，仅其 sha256 入库（confirm_token_hash），明文只在创建响应中返回一次；confirm 必须携带明文，服务端 hash 比对、状态须为 pending、未过 expires_at；成功/失败均终态（成功带幂等键重放返回首次 WriteResult）。

### 9.3 LLMClient

```python
class LLMClient(Protocol):
    async def chat(self, messages: list[Msg], *, model: str | None = None,
                   temperature: float | None = None,
                   json_schema: dict | None = None,
                   timeout: float | None = None,
                   stream_writer=None) -> LLMResult: ...

class LLMResult(BaseModel):
    content: str
    usage: dict                     # {prompt_tokens, completion_tokens, total_tokens}
    model: str
    finish_reason: str
    retries: int
    latency_ms: int
```

实现（`adapters/llm.py`，langchain-openai 兼容模式指向 DeepSeek）：

| 机制 | 细节 |
|---|---|
| 超时 | 连接 10s；非流式读 120s；流式两次 chunk 间隔 60s（可配） |
| 重试 | 429（尊重 Retry-After）、5xx、超时、连接错误 → 指数退避 1s/2s/4s ± 抖动，最多 3 次；4xx 不重试 |
| 限流 | 全局信号量（runtime_config.llm_concurrency，默认 4），排队等待不计超时 |
| JSON 模式 | json_schema 非空：优先 response_format=json_object + 在 user 消息尾部贴 schema；响应先用 json-repair 解析再 Pydantic 校验；失败携带 `ValidationError.errors()` 文本重请 1 次 |
| 流式 | 节点生成用 stream：chunk 经 LangGraph stream_writer → `llm_token` 事件；usage 从流尾 message 取（DeepSeek 流式返回 usage） |
| 错误映射 | 重试耗尽 → LLM_UPSTREAM（5xx/网络，retryable=true）/ LLM_TIMEOUT（retryable=true）/ RATE_LIMITED（retryable=true）/ LLM_BAD_REQUEST（4xx，false） |
| 不重试的取消 | asyncio 取消在当前 chunk 间生效；批次边界仍由 Runner 控制 |

### 9.4 ExportService

```python
class ExportService:
    async def export_md_zip(self, task_id, *, case_ids: list[str] | None = None) -> ExportOut:
        """筛选：status=active 且 review_status in (adopted, edited_adopted)；
           打包前逐文件 hash 校验；不一致文件列入 skipped 不进 zip。
           小批量（≤200 条且总字节 ≤20MB）同步返回；否则创建导出任务（一期实现：
           后台 asyncio task + 结果落 task_event/临时目录，句柄轮询 GET /tasks/{id}/export）。"""
```

zip 结构（**Q2 一期定稿**）：根目录 `INDEX.md`（序号/标题/优先级/评审状态/溯源条款）+ `v{stage_version}/{point_id}-{case标题slug}.md`（同 point 重名追加 `-2/-3`）。Excel 汇总与用例管理系统对接 → 二期预留。

---

## 10. API 详细设计

### 10.1 通用信封与状态码

成功直接返回资源对象或 `Page` 信封；分页响应统一：

```json
{ "items": [ ... ], "next_cursor": "eyJsYXN0..." }
```

错误响应（所有 4xx/5xx）：

```json
{ "error": {
    "code": "TASK_STATE_CONFLICT",
    "message": "任务正在运行，无法重复启动",
    "retryable": false,
    "details": {"current_status": "running"} } }
```

| HTTP | 使用场景 |
|---|---|
| 200 | 成功 |
| 201 | POST 创建成功（返回新对象，含 Location 头） |
| 400 | VALIDATION_*（body/参数错误） |
| 404 | NOT_FOUND（含工作区隔离不命中——不暴露存在性差异） |
| 409 | TASK_STATE_CONFLICT / VERSION_CONFLICT / 幂等请求体不一致 / 任务运行中 |
| 422 | 语义校验失败（如修订契约非法、评审状态转换非法）→ VALIDATION_REVIEW_TRANSITION 等 |
| 424 | KB_*（ReMe 依赖失败）、LLM_UPSTREAM/TIMEOUT 出现在同步探活类接口 |
| 500 | INTERNAL |

通用请求头：`Idempotency-Key`（POST 可选/必需见 §5.0 tech-design）、`If-Match`（PUT case）、`Last-Event-ID`（SSE）。

### 10.2 核心请求/响应模型（仅列关键字段，其余同 tech-design §5）

```python
# ---- workspace ----
class CreateWorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    kb_config: KbConfigIn
class KbConfigIn(BaseModel):
    mode: Literal["sdk", "service"]
    target: str                    # SDK 路径 / service base_url
    kb_id: str
    options: dict = {}
class KbTestOut(BaseModel):
    ok: bool; latency_ms: int
    capabilities: ReMeCaps | None
    error_code: str | None

# ---- conversation / message ----
class CreateConversationIn(BaseModel):
    workspace_id: str; title: str | None = None
class SendMessageIn(BaseModel):
    content: str
    kind: Literal["chat", "change_request"] = "chat"
    context: dict | None = None    # change_request 时可携带目标 stage 提示
class MessageOut(BaseModel): ...   # 对齐 message 表字段 + author 展示名
class SendMessageOut(BaseModel):
    user: MessageOut
    assistant: MessageOut | None = None  # kind=chat 时跑 tool_agent；含 payload.tool_trace
# chat：落 user → tool_agent_graph → 落 assistant；change_request：仅落 user。
# 工具实现见 tools/（sandbox、bash_persistent、str_replace_editor、registry）与
# graph/tool_agent.py / tool_gather.py；产线默认 enable_tools_stages=[]。

# ---- task ----
class CreateTaskIn(BaseModel):
    conversation_id: str
    requirement_md: str = Field(min_length=1)
    snapshot_level: Literal["off","meta","full"] | None = None
class TaskOut(BaseModel):
    id: str; workspace_id: str; conversation_id: str
    status: TaskStatus; current_stage: str
    active_artifacts: dict[str, ArtifactOut]   # stage -> active 摘要
    progress: dict[str, list[BatchResult]]     # 节点 -> 批次进度
    error_info: dict | None
    stale: bool = False                        # waiting_* 超 7 天
    created_at: str; updated_at: str

class RollbackIn(BaseModel):
    target_stage: Literal["link_identify", "point_write"]
    artifact_id: str
    expected_version: int
    revised_artifact: dict | None = None       # LinkPlan/PointPlan 修订版
class RollbackOut(BaseModel):
    graph_run_id: str
    impact: ImpactAnalysis                     # §11.2

# ---- confirm / answer / cancel ----
class ConfirmIn(BaseModel):
    gate_kind: Literal["plan_confirm", "review_decision"] = "plan_confirm"
    artifact_id: str
    action: Literal["confirm", "modify", "reject_rerun"] = "confirm"
    # 遗留路径：stage + expected_version 同时给出时走五阶段 confirm
    expected_version: int | None = None
    stage: str | None = None               # link_identify | point_write | …
    payload: dict | None = None            # modify 修订体 / ReviewProposal
class AnswerIn(BaseModel):
    answers: list[dict]                        # [{question_id, answer}]

# ---- plan / subtasks / review ----
# GET /tasks/{id}/plan → AgentPlan（或 404）
# GET /tasks/{id}/subtasks → list[SubtaskOut]
# GET /tasks/{id}/review-proposals/{artifact_id} → ReviewProposal

# ---- case ----
class CaseUpdateIn(BaseModel):
    markdown: str
class CaseDetailOut(BaseModel):
    id: str; point_id: str; stage_version: int
    lineage: Lineage; review_status: ReviewStatus
    markdown: str; content_hash: str
    trace_refs: TraceRefs; error_info: dict | None
class ReviewIn(BaseModel):
    items: list[ReviewItem]
    class ReviewItem(BaseModel):
        case_id: str
        action: Literal["adopt", "reject", "edited_adopted"]
class RegenerateIn(BaseModel):
    case_ids: list[str] = Field(min_length=1)
    instruction: str
    keep_original: bool = True                 # 旧行保留或置 obsolete
class ExportOut(BaseModel):
    status: Literal["ready", "running"]
    download_url: str | None
    skipped: list[dict]                        # hash 不一致等跳过项
    job_id: str | None

# ---- retrieval debug ----
class PlaygroundIn(BaseModel):
    query: str
    stage: str
    overrides: dict | None = None              # top_k/query_paths/types 临时覆盖
class PlaygroundOut(BaseModel):
    funnel: dict                               # 各步计数
    candidates: list[Candidate]
    injected: list[InjectedItem]
    degraded: list[DegradedStep]
    latencies: dict

# ---- snapshot ----
class SnapshotItemOut(BaseModel): ...           # context_snapshot 行（items 含偏移）
class SnapshotLineOut(BaseModel):
    position: int; entry_id: str; entry_version: str; title: str; content: str

# ---- config / kb proposal ----
class ModelConfigIn(BaseModel):
    base_url: str; api_key: str; model: str
    temperature: float = 0.2; top_p: float = 1.0; timeout: int = 120
class ModelTestOut(BaseModel):
    ok: bool; latency_ms: int; model: str | None; error_code: str | None
class ProposalIn(BaseModel):
    workspace_id: str; task_id: str | None; payload: dict
class ProposalCreatedOut(BaseModel):
    id: str; confirm_token: str; expires_at: str   # 明文令牌仅此一次
class ProposalConfirmIn(BaseModel):
    confirm_token: str
```

### 10.3 端点行为补充（表见 tech-design §5.1~5.6，此处只补易歧义点）

1. `POST /tasks`：创建即写 requirement.md（先文件）并落 task 行，初始 status 复用 `waiting_input`（语义为"已建未启动"，前端创建成功后立即自动调 `/run`，该态在任务列表中展示为"待启动"而非"澄清中"）；Reaper 不触碰该态，用户可稍后手动启动。启动后 running。
2. `POST /tasks/{id}/run`：200 返回 `{events_url, resume_from:{node, batch_id, done, total}}`；409 返回 TASK_STATE_CONFLICT。
3. `GET /tasks/{id}/events`：任务不存在/跨工作区返回 404（握手阶段），建流后错误走 task_error 帧。
4. `PUT /cases/{id}`：body.markdown 解析 → 渲染规范化 → 若 front-matter 被用户误改，以服务端重算的元数据为准（仅正文 hash 参与 If-Match）；成功置 edited_adopted 并写 review_record(edit, detail=diff 统计)。
5. `POST /cases/review`：逐条按 §3.2④ 转换表校验，任一非法整体 422（不部分成功），返回每项校验结果。
6. `POST /kb/proposals/{id}/confirm`：缺/错令牌返回 400 KB_TOKEN_INVALID（提案本身在列表中可见，无需 404 遮蔽）；过期 409 PROPOSAL_EXPIRED；成功 200 返回 WriteResult。
7. 所有列表 GET 支持 `?limit=&cursor=`，traces/cases 额外支持 `?stage=&version=&review=&status=`。

### 10.4 SSE 事件 payload（与 tech-design §5.7 一致，此处定型字段）

```python
EVENT_SCHEMAS = {
 "node_start":         {"node": str, "stage_version": int|None, "batch_id": str|None},
 "node_end":           {"node": str, "latency_ms": int},
 "batch_progress":     {"node": str, "batch_id": str, "done": bool, "total": int},
 "retrieval_summary":  {"stage": str, "batch_id": str|None, "query_count": int,
                        "candidate_count": int, "injected_count": int, "degraded": list},
 "context_assembled":  {"node": str, "batch_id": str|None, "item_count": int,
                        "tokens_est": int, "budget": int, "truncated": bool},
 "budget_warning":     {"node": str, "tokens_est": int, "budget": int},
 "llm_token":          {"node": str, "batch_id": str|None, "chunk": str},
 "checkpoint_waiting": {"stage": str, "artifact_id": str, "stage_version": int},
 "human_gate_waiting": {"stage": str, "artifact_id": str, "stage_version": int,
                        "gate_kind": "plan_confirm"|"review_decision",
                        "step_id": str|None},
 "plan_updated":       {"plan_id": str, "version": int, "status": str},
 "step_started":       {"step_id": str, "kind": str},
 "step_finished":      {"step_id": str, "kind": str, "output_ref": str|None},
 "subtask_started":    {"subtask_id": str, "kind": str},
 "subtask_finished":   {"subtask_id": str, "status": str, "output_ref": str|None},
 "reflection_result":  {"step_id": str, "decision": "pass"|"repair"|"replan"},
 "review_proposal_ready": {"artifact_id": str, "scope": str},
 "clarification_needed": {"questions": list},
 "case_generated":     {"case_id": str, "title": str, "file_path": str, "batch_id": str},
 "coverage_ready":     {"matrix_summary": dict, "warnings": list},
 "task_done":          {"status": "completed"},
 "task_error":         {"code": str, "message": str, "retryable": bool, "node": str|None},
}
```

客户端（§12）必须容忍未知事件类型与未知字段（前后端独立迭代）。

---

## 11. 关键一致性协议伪代码

### 11.1 用例批次提交（先文件后 DB）

```python
async def commit_case_batch(ctx, batch_id, points, generated, version):
    rows, idem_ids = [], []
    for point, cases in zip(points, generated["by_point"].items()):
        for seq, case in enumerate(cases):
            case_id = uuid5(ID_NS, f"{ctx.task.id}|{version}|{batch_id}|{point.id}|{seq}").hex
            idem_ids.append(case_id)
            content = CaseFileContent(case_id=case_id, point_id=point.id,
                                      stage_version=version, **case)
            written = await ctx.files.write_case(ctx.ws, ctx.task.id, version, content)  # 原子
            rows.append(CaseRow(id=case_id, task_id=ctx.task.id, point_id=point.id,
                                stage_version=version,
                                lineage=Lineage(root_case_id=case_id),
                                status=ACTIVE, review_status=PENDING,
                                file_path=written.file_path,
                                content_hash=written.content_hash,
                                title=content.title, trace_refs=content.trace_refs))
    async with ctx.db.immediate_tx():                    # BEGIN IMMEDIATE 短事务
        await ctx.daos.testcase.put_batch(rows)          # INSERT OR IGNORE
        await ctx.daos.testcase.sweep_stale_idem(        # 非确定性防护 §7.3
            ctx.task.id, version, batch_id, keep=idem_ids)
        await ctx.daos.artifact.attach_case_ids(ctx.task.id, version, batch_id, idem_ids)
```

崩溃矩阵：

| 崩溃点 | 现象 | 恢复 |
|---|---|---|
| tmp 写盘中 | 仅留 .tmp 文件 | 下次维护清理 .tmp；无 DB 影响 |
| rename 后、DB 事务前 | 孤儿 MD 文件 | put_batch 重放（INSERT OR IGNORE 补行）；或 Reconciler 登记孤儿 |
| DB 事务中 | 事务回滚，文件已成 | 重跑批次时同 hash 写文件无副作用，行重新插入 |
| DB 提交后 | 一致 | — |

### 11.2 回退协议（影响面 + 落账 + checkpoint 切换）

```python
async def rollback(ctx, task_id, body: RollbackIn) -> ImpactAnalysis:
    async with ctx.db.immediate_tx():                    # 全程一个事务
        task = await ctx.daos.task.get_for_update(task_id)
        assert_transition(task.status, allow={"waiting_confirm","waiting_input","completed"})
        target = await ctx.daos.artifact.get(body.artifact_id)
        assert target.id == body.artifact_id and target.stage_version == body.expected_version

        old_plan = target.payload
        new_plan = body.revised_artifact or old_plan
        impact = analyze_impact(target.stage, old_plan, new_plan)
        # impact = {
        #   "stories": {"added":[...], "removed":[...], "changed":[id...]},
        #   "downstream": [ {stage, affected_ids:[...], unaffected_ids:[...]} ] }

        new_run = uuid4().hex
        new_thread = f"{task.langgraph_thread_id}::run{run_seq(task)}"
        await ctx.daos.artifact.superseded_and_obsolete(
            task_id, from_stage=target.stage,
            affected=impact.downstream, new_run=new_run)   # unaffected 行改挂 new_run 保 active
        # 写入用户修订版本（若带修订）：目标阶段新版本 v+1，origin=user_revised
        entry_plan = old_plan
        if body.revised_artifact is not None:
            v = await ctx.daos.artifact.next_version(task_id, target.stage)
            await ctx.daos.artifact.supersede(target.id)
            await ctx.daos.artifact.put(ArtifactRow(
                id=uuid4().hex, task_id=task_id, stage=target.stage,
                graph_run_id=new_run, stage_version=v, origin=USER_REVISED,
                status=ACTIVE, payload=new_plan, confirmed_by="user"))
            entry_plan = new_plan
        # case 版本：受影响 point 所属 case 置 obsolete；unaffected 继承
        await ctx.daos.testcase.mark_obsolete_by_versions_or_points(
            task_id, affected_point_ids=impact.affected_points())
        await ctx.daos.task.start_new_run(task_id, graph_run_id=new_run,
                                          thread_id=new_thread, stage=target.stage)
        await ctx.daos.message.put(SystemRollbackMessage(task_id, impact))  # 留痕

    # 事务提交后：初始化新 run 的入口 state（文件/DB 已一致）
    await ctx.app.graphs.start_run_from_plan(new_thread, entry_state=entry_state)
    await ctx.bus.emit(task_id, "node_start", {"node": target.stage})
    return impact
```

影响面判定规则（结构化 diff，不靠 LLM）：

- link_identify 回退：story 删除/link 变更 → 其下 points 全部 affected；story 仅 summary 文案改动 → points 标记 unaffected（用例依据为条款与知识而非摘要），但 UI 提示文案已变；新增 story → 仅新增，不影响存量。
- point_write 回退：point 删除/其 clause_ids 集合变化 → 关联 case affected；仅 priority/title 微调 → unaffected。
- unaffected 产物：`graph_run_id` 改挂新 run、status 保持 active、review_status 原样保留；继承映射写 review_record 之外的一张轻量留痕（stage_artifact.payload 中 `inherited_from_run`）。

### 11.3 Reconciler 对账

```python
async def reconcile_workspace(ws):
    for task in list_tasks(ws):
        db_cases = testcase_dao.list_all_including_obsolete(task.id)
        files = set(files.list_case_files(ws, task.id))
        by_path = {c.file_path: c for c in db_cases if c.error_info is None}

        for c in db_cases:
            if c.file_path not in files:
                testcase_dao.mark_error(c.id, "file_missing")            # UI 标红
        for f in files - set(by_path):
            maintenance_quarantine(f)                                     # 登记不挂接
        for path, c in by_path.items():
            h = files.hash_of(path)
            if h != c.content_hash:
                testcase_dao.mark_error(c.id, "hash_conflict")            # 用户二选一处理
```

触发时机：进程启动（全量，但每个工作区只在首次被访问时惰性执行——避免工作区多时启动慢）；任务进入 review_export 前对该任务快速校验一次；用户在 UI 手动点"检查文件一致性"。hash_conflict 的消解动作（以文件为准 / 覆盖文件）各自走普通 update API，不特殊后门。

### 11.4 知识库写入两阶段（时序）

```
客户端                API/proposals         API/confirm            ReMeWriter
  │ 提交建议 ────────►│ 生成 token(32B)
  │                  │ 存 hash+pending+expires
  │◄─ token(明文一次) │
  │                  │                                                     │
  │ confirm+token+幂等键 ─►│ 校验状态/期限/hash（事务）                     │
  │                       │ 标记 confirming ───────────────────────────► │ write
  │                       │◄─ WriteResult(ok, remote_ref, verified) ──────│
  │                       │ 置 confirmed + write_result（终态）
  │◄─ 200 WriteResult    │
```

失败语义约定：ReMe 写失败（网络/拒绝）时提案保持 pending 并记录失败次数（允许用户在令牌有效期内带同 token 重试，幂等不重复写）；WriteResult.verified=False（写入后无法回查确认）时置 confirmed 但 write_result 标 `needs_manual_check`（S6 预案，UI 提示人工核对）。

---

## 12. 前端详细设计（web/）

### 12.1 技术选型与目录

React 18 + Vite + TypeScript + TanStack Query（服务端状态）+ Zustand（会话/流式本地状态）+ react-markdown（渲染）+ CodeMirror 6（MD 编辑）。路由 react-router，history 模式（后端 SPA fallback）。

```
web/src/
├── api/
│   ├── client.ts          # fetch 封装：错误信封解包、游标分页、Idempotency-Key 注入
│   ├── sse.ts             # EventSource：Last-Event-ID、重连退避、未知事件忽略
│   ├── domain.ts          # 含 GateKind / ReviewProposal / CheckpointWaiting
│   └── endpoints.ts       # 类型化端点（含 confirmTask.gate_kind、getReviewProposal）
├── stores/
│   ├── taskStream.ts      # SSE → phase/checkpoint/clarification；归一 human_gate_waiting
│   └── session.ts
├── pages/
│   ├── SessionPage.tsx         # 会话优先壳（现 re-export ChatPage）
│   ├── ChatPage.tsx            # 需求发起、澄清、内联门禁/评审卡、change_request
│   ├── StageConfirmPage.tsx    # 完整 CP 编辑（可选；会话内 GateConfirm 为主）
│   ├── WorkbenchPage.tsx
│   ├── RetrievalDebugPage.tsx
│   ├── WorkspacesPage.tsx
│   └── SettingsPage.tsx
├── components/
│   ├── session/（GateConfirmCard、ReviewProposalCard）
│   ├── chat/（MessageList、RequirementInput、ClarificationCard）
│   ├── confirm/（LinkPlanEditor、PointPlanEditor、ImpactPreview）
│   ├── case/（CaseList、MdViewer、MdEditor、ReviewBar、…）
│   ├── debug/（…）
│   └── common/（ErrorBanner、ReconnectBanner、…）
└── main.tsx / router.tsx      # 主路由 `/` → SessionPage
```

### 12.2 流式状态管理（taskStream store）

- 打开任务即建 SSE；`checkpoint_waiting` / `human_gate_waiting` 写入 `checkpoint`（含可选 `gate_kind`/`step_id`）。
- `gate_kind=review_decision` → Session 渲染 `ReviewProposalCard`（拉 `GET .../review-proposals/{id}`，可改条目 action 后 modify/confirm/reject_rerun）；否则 `GateConfirmCard`（plan_confirm）。
- `clarification_needed` → 内联 ClarificationCard，不强制跳转。
- 其余：`batch_progress` / `budget_warning` / `task_error` / `task_done` 同前。

### 12.3 关键页面交互要点

| 页面 | 要点 |
|---|---|
| SessionPage / ChatPage | 需求框建 task→run→订阅；澄清内联；**门禁/评审卡内联在状态区**；change_request `@阶段` → ImpactPreview → rollback；完整确认页链接仅作可选 |
| StageConfirmPage | 遗留 CP1/CP2 大编辑面；confirm 带 `gate_kind=plan_confirm` + expected_version |
| WorkbenchPage | 三栏列表/渲染/编辑；If-Match；file_missing/hash_conflict 消解；采纳率仅 active |
| RetrievalDebugPage | 阶段 tab + 漏斗 + 快照 + Playground |
| SettingsPage | 模型/KB 探活；能力位只读勾选 |

### 12.4 前端容错总则

- 所有 fetch 走统一解包：网络错误（无法连接）给"服务不可用，检查后端进程"；错误码到中文文案的映射表内置，未知 code 显示 message 原文。
- 所有用户动作按钮在 pending 态禁用；SSE 断线时页面顶部黄条"连接中断，重连中…"，不打断已加载内容。

---

## 13. 配置项清单

### 13.1 环境变量（server/main.py 读取）

| 变量 | 默认 | 说明 |
|---|---|---|
| TESTER_AGENT_DATA_DIR | `./data` | 数据根目录（DB + 文件） |
| TESTER_AGENT_HOST | `127.0.0.1` | 监听地址（一期默认回环） |
| TESTER_AGENT_PORT | `8080` | |
| TESTER_AGENT_LOG_LEVEL | `INFO` | |
| TESTER_AGENT_SINGLE_WORKER | `1` | 非 1 时启动拒绝（写死约束，避免误配多 worker） |

### 13.2 config.runtime_config（可在设置页"高级"中改，改后对新 run 生效）

```json
{
  "llm_concurrency": 4,
  "llm_timeout_connect_sec": 10,
  "llm_timeout_read_sec": 120,
  "batch_size": 5,
  "index_inject_hard_limit": 200,
  "retrieval_token_budgets": {"link_identify": 12000, "point_write": 16000, "case_generate": 12000},
  "heartbeat_interval_sec": 10,
  "heartbeat_stale_sec": 120,
  "suspend_stale_days": 7,
  "retention": {"events_days": 7, "snapshots_days": 30, "obsolete_cases_days": 30, "proposals_days": 14},
  "index_mirror_ttl_min": 60,
  "coverage_max_rounds": 2,
  "export_sync_limits": {"cases": 200, "bytes": 20971520}
}
```

agent.config（内置用例智能体种子配置，可编辑）：

```json
{
  "snapshot_level_default": "meta",
  "ambiguity_check": true,
  "prompts_dir": "server/prompts",
  "retrieval_overrides": {}
}
```

---

## 14. 错误码目录

| code | HTTP | retryable | 触发点 | 用户侧含义/处理 |
|---|---|---|---|---|
| VALIDATION_BODY | 400 | false | Pydantic 入参 | 表单校验提示 |
| VALIDATION_ARTIFACT_REVISION | 422 | false | CP 修改契约校验 | 按字段错误修正 |
| VALIDATION_REVIEW_TRANSITION | 422 | false | 评审状态机 | 提示当前状态不允许该动作 |
| NOT_FOUND | 404 | false | 资源不存在/跨工作区 | 刷新列表 |
| TASK_STATE_CONFLICT | 409 | false | 状态迁移、重复 run/confirm | 刷新任务态后重试 |
| VERSION_CONFLICT | 409 | false | If-Match/expected_version 不符 | 提示"内容已变更"，展示差异后重提 |
| TASK_BUSY | 409 | true | Registry 锁占用且心跳新鲜 | 稍后重试（前端不自动） |
| TASK_CANCELLED | — | false | 取消落终态 | 正常提示 |
| INTERRUPTED_BY_RESTART | — | true | Reaper 改判 | 显示"异常中断"，点重试从批次恢复 |
| LLM_UPSTREAM | 424/事件 | true | 5xx/网络，重试耗尽 | 稍后重试/检查网络 |
| LLM_TIMEOUT | 424/事件 | true | 超时耗尽 | 可缩小批次后重试（设置提示） |
| RATE_LIMITED | 424/事件 | true | 429 耗尽 | 等待后重试 |
| LLM_BAD_REQUEST | 400/事件 | false | 4xx（key 错/参数错） | 设置页检查配置 |
| LLM_BAD_OUTPUT | 事件 | true | 结构化两次失败 | 重试；持续出现报缺陷（附 snapshot） |
| KB_UNREACHABLE | 424 | true | ReMe 连接失败 | 工作区页测试连接 |
| KB_CAPABILITY | 200(degraded) | — | 能力缺失已降级 | 面板黄色"降级"标记，非阻断 |
| KB_TOKEN_INVALID | 400 | false | 令牌错 | 重新发起提案 |
| PROPOSAL_EXPIRED | 409 | false | 超期 | 重新发起 |
| FILE_MISSING | 200(标记) | false | 对账 | 红条：找回/作废 |
| FILE_CONFLICT | 409 | false | 编辑时 hash 不符 | 拉取最新内容，diff 后再保存 |
| INTERNAL | 500 | false | 未预期异常 | 附 trace_id 报缺陷 |

所有 5xx 与 task_error 在服务端日志带统一 `trace_id`（事件 payload 不强制带，error_info.details 带），便于从用户反馈定位日志。

---

## 15. 测试策略

### 15.1 测试金字塔

| 层 | 范围 | 工具/替身 |
|---|---|---|
| 单元 | 六检索算子、clause 切分、影响面 diff、hash/渲染、状态机表、游标分页、close_loop | pytest + fakes（FakeLLM/FakeReader，按脚本返回） |
| 组件 | 批次执行器（崩溃/重放/取消）、提交协议、回退协议、Reconciler、提案两阶段 | pytest + 真实 SQLite（tmp_path）+ FakeLLM |
| API | 全部端点 200/4xx 路径、分页/幂等/乐观锁/SSE 补发 | httpx.AsyncClient + ASGI transport |
| 图集成 | 五节点全链路（假 LLM 脚本驱动）、CP 中断恢复、跨"重启"（清内存锁、模 Reaper） | FakeReader 内置小型知识库夹具 |
| 前端 | 关键组件（MdEditor 冲突、Funnel、SSE 重连） | Vitest + MSW |
| E2E | 主场景：需求→CP1→CP2→评审→导出；回退继承；崩溃恢复 | Playwright（手动触发，不入 PR 必跑） |

### 15.2 必测场景清单（高风险逻辑的验收用例）

1. **批次幂等**：case_generate 跑到 b2 中途 kill（直接抛异常模拟）→ 重新 run → b0/b1 无重复文件与重复行，b2 重做成功。
2. **提交崩溃矩阵**：在 §11.1 四个崩溃点分别注入故障，库/文件最终一致。
3. **回退继承**：CP1 修改一个 story 名（仅摘要）→ 下游 point/case 全部继承、评审状态保留；删除一个 story → 对应产物 obsolete 且其余不动；回退后新 graph_run_id 的 trace 与旧 run 可分别查看。
4. **重复 run/confirm**：运行中再 run → 409；迟到的旧版本 confirm（expected_version 落后）→ 409。
5. **取消**：运行中 cancel → 节点在当前批结束处 aborted；waiting_confirm 时 cancel 立即生效。
6. **重启 Reaper**：heartbeat 置旧 → 重启 → running 改 failed(INTERRUPTED_BY_RESTART) → run 从游标恢复。
7. **SSE 补发**：run 中途断开（kill EventSource），3 个事件后用 Last-Event-ID 重连，无重复无遗漏。
8. **降级链**：FakeReader caps 关 metadata_filter → 走镜像过滤且 trace.degraded 有记录；rerank FakeLLM 抛 5xx → 规则分兜底不中断。
9. **引用闭环**：产物引用白名单外 ID → hallucinated 计数；未引用注入条目 → injected_not_used。
10. **对账**：外部删文件/改文件/放孤儿文件 → 三种标红/隔离正确，消解动作后恢复。
11. **编辑乐观锁**：A/B 两请求基于同一 hash 编辑，后者 409 且返回新 hash。
12. **知识库门禁**：图运行代码路径 import ReMeWriter 失败（import-linter 测试）；confirm 端点幂等重放返回同一结果。

### 15.3 eval 冒烟集与回归（D16）

- `tests/fixtures/knowledge/` 放小型假知识库（5 类各若干条）+ `fixtures/requirements/` 放 3~5 组构造需求与期望产物（黄金集到位前，S7）。
- `server/eval/run.py --preset smoke --config-diff '{"query_paths":4}'`：跑指定阶段，输出指标对比表：

  | 指标 | 口径 |
  |---|---|
  | recall_hit | 期望条目出现在 candidates 的比例 |
  | inject_hit | 期望条目出现在 injected 的比例 |
  | reference_rate | injected∩referenced / injected |
  | clause_coverage | 覆盖矩阵 covered 条款 / 全部条款 |
  | cost | aux+主调用 tokens、墙钟耗时、降级步数 |

- eval 与单测共享 FakeReader（eval 离线、确定性），prompt 模板/Pydantic 契约改动在 CI 跑 smoke，指标下降阈值（暂定 inject_hit −5pt）阻断合并，阈值随黄金集积累校准。

### 15.4 日志与可排障

- 结构化日志（JSON 行）：每次 LLM/检索调用记 task_id/run_id/node/batch_id/duration/retry/degraded；日志不含 api_key 与需求全文正文（只记 hash 与长度）。
- 任务失败时 task_error 事件 + 日志 trace_id 对齐；调试面板的 snapshot/trace 即业务侧排障主入口，不要求用户翻日志。

---

## 16. 实施顺序建议（落地切片）

| 切片 | 内容 | 出口标准 |
|---|---|---|
| C0 地基 | store(DDL/DAO/迁移) + FileStore + config + 健康检查 | DAO/原子写/对账单测全绿 |
| C1 适配器 | FakeReader 先行 + LLMClient（含韧性）+ ReMe SDK 适配（S1） | caps 探测与降级可演示；S1 结论归档 |
| C2 检索子图 | 六算子 + trace + 快照三档 + playground | 单测 + smoke eval 出数 |
| C3 图主干 | 五节点 + CP 中断 + 批次执行器（不接 UI，API 驱动） | 场景 1~6 集成测试全绿 |
| C4 运行时 | Runner/Registry/EventBus/Reaper + SSE | 场景 7 + 重启恢复测试 |
| C5 前端主流程 | Chat/Confirm/Workbench/Debug 四页 | E2E 主场景走通 |
| C6 评审与收尾 | 评审/导出/提案两阶段/保留期清理 | 场景 10~12；文档与 Q1/Q2 结论回灌 |

每个切片完成后更新本文版本；C1 的 S1 结论若触发预案（镜像过滤为正式方案），需同步回改 tech-design §4.3 与本文 §8.4。

---

## 17. 异常体系

### 17.1 类层次（server/errors.py 唯一来源）

```python
class AppError(Exception):                    # 所有可预期异常的基类
    code: str = "INTERNAL"
    http_status: int = 500
    retryable: bool = False
    def __init__(self, message: str = "", *, details: dict | None = None): ...

# ---- 客户端类（不重试）----
class ValidationError(AppError):
    code, http_status = "VALIDATION_BODY", 400
class ArtifactRevisionError(AppError):
    code, http_status = "VALIDATION_ARTIFACT_REVISION", 422
class ReviewTransitionError(AppError):
    code, http_status = "VALIDATION_REVIEW_TRANSITION", 422
class NotFoundError(AppError):
    code, http_status = "NOT_FOUND", 404
class TaskStateConflict(AppError):
    code, http_status = "TASK_STATE_CONFLICT", 409
class VersionConflict(AppError):
    code, http_status = "VERSION_CONFLICT", 409
class FileConflict(AppError):
    code, http_status = "FILE_CONFLICT", 409
class KbTokenInvalid(AppError):
    code, http_status = "KB_TOKEN_INVALID", 400
class ProposalExpired(AppError):
    code, http_status = "PROPOSAL_EXPIRED", 409

# ---- 可重试/依赖类 ----
class TaskBusyError(AppError):
    code, http_status, retryable = "TASK_BUSY", 409, True
class LLMUpstreamError(AppError):
    code, http_status, retryable = "LLM_UPSTREAM", 502, True
class LLMTimeoutError(AppError):
    code, http_status, retryable = "LLM_TIMEOUT", 504, True
class RateLimitedError(AppError):
    code, http_status, retryable = "RATE_LIMITED", 429, True
class LLMBadRequest(AppError):
    code, http_status = "LLM_BAD_REQUEST", 400
class LLMBadOutput(AppError):
    code, http_status, retryable = "LLM_BAD_OUTPUT", 502, True
class KbUnreachable(AppError):
    code, http_status, retryable = "KB_UNREACHABLE", 502, True

# ---- 内部控制流（不映射 HTTP，不出 Runner 边界）----
class TaskCancelled(Exception): ...          # 批次边界捕获 → aborted
# GraphInterrupt / interrupt() 直接使用 langgraph.types 原生类型，不另包装
class PathEscapeError(Exception): ...         # 编程错误：wrap() 兜底按 INTERNAL 处理
```

### 17.2 抛出与捕获边界

| 层 | 职责 |
|---|---|
| store/adapters | 抛**贴近根因**的异常：`sqlite3.IntegrityError` 由 DAO 翻译成 `VersionConflict/NotFoundError`；LLM 客户端把 openai 异常翻译成 LLM* 系列（§9.3）；ReMe 连接异常翻成 KbUnreachable |
| graph 节点 | 不吞异常；除 TaskCancelled 与中断外一律上抛。节点只允许抛 AppError 子类（wrap() 兜底把未知异常包成 INTERNAL 并打 trace_id 日志） |
| runtime Runner | 唯一收口：TaskCancelled→aborted；GraphInterrupt→waiting_*；AppError→failed 并写 ErrorInfo（retryable 透传）；其余→failed(INTERNAL)。终态后发 task_error 事件（§6.4） |
| api | FastAPI exception_handler 把 AppError → §10.1 错误信封（HTTP 状态取类属性）；SSE 流内不抛 HTTP，错误以 task_error 帧送达 |

翻译示例（LLMClient）：

```python
try:
    resp = await self._client.chat.completions.create(**kwargs)
except (APITimeout, APITimeoutError) as e:
    raise LLMTimeoutError(str(e)) from e
except RateLimitError as e:
    raise RateLimitedError(e.message, details={"retry_after": e.response.headers.get("retry-after")}) from e
except APIStatusError as e:
    if e.status_code >= 500: raise LLMUpstreamError(str(e)) from e
    raise LLMBadRequest(str(e)) from e
```

### 17.3 trace_id 约定

- 每个 HTTP 请求进入中间件时生成 `trace_id`（uuid 短码），写日志 contextvar；
- 图运行内部每次 Runner._run 生成 run 级 trace_id 并贯穿该 run 所有日志；task_error 的 details.trace_id 用它；
- 错误信封不强制返回 trace_id（5xx 必须返回，4xx 可选）。

---

## 18. 端到端时序

### 18.1 主场景（建任务 → CP1 → CP2 → 评审导出）

```
前端          API(tasks/run)   Runner      Graph/节点       Retrieval     DAO/FileStore     EventBus/SSE
 │ 建任务          │             │             │               │             │                  │
 │ POST /tasks ──►│ 写requirement.md+task行(waiting_input)                                       │
 │ POST /run ────►│ acquire锁 ─►│ _run        │               │             │                  │
 │ GET /events ══════════════════════════════════════════════ 先回放task_event再挂总线 ════════►│
 │                             │ ainvoke ───►│ intake:条款切分│             │ task.clauses      │
 │                             │             │ link_identify►│ 六算子留痕   │ artifact(v1)      │
 │◄══════════ checkpoint_waiting{artifact_id,v1} ════════════════════════════════════════════│
 │ （状态 waiting_confirm，Runner 已释放图、持锁结束）                                            │
 │ 审核/修改 LinkPlan                                                                          │
 │ confirm(modify,payload,expected_version=1) ─►│ 校验→superseded v1→put v2(user_revised)      │
 │                                             │ resume(update_state 新 plan) ─►│ point_write 批次
 │◄══ batch_progress(b0 done…) / retrieval_summary / context_assembled … ════════════════════│
 │◄══════════ checkpoint_waiting(point_write,v1) ═══════════════════════════════════════════│
 │ confirm ───────────────────────────────────►│ case_generate 批次 → 先写MD后写行/快照/trace   │
 │◄══ case_generated ×N（乐观插入，随即 GET /cases 校准）════════════════════════════════════│
 │                             │             │ coverage_check（≤2 轮补充）  │ coverage artifact │
 │◄══════════ coverage_ready / task_done ════════════════════════════════════════════════════│
 │ 工作台评审（review/编辑带If-Match/regenerate）→ POST /export（hash 校验）→ zip 下载         │
```

要点核对：checkpoint 中断时 Runner 随图中断退出并释放锁（waiting_* 期间无在飞执行体）；事件先落 task_event 后广播，故 GET /events 晚于 /run 发出也不丢帧。

### 18.2 批次中途崩溃与恢复

```
case_generate b0(done) b1(started: MD已rename、DB事务未提交) ✗ 进程被杀
  启动 → Reaper: running+心跳陈旧 → failed(INTERRUPTED_BY_RESTART)
用户点重试 → run（新 Runner，同 thread_id、同 graph_run_id、同 idempotency_nonce）
  读 progress：b0=done 跳过；b1=started 视为未完成整批重做
    → 同 case_id（uuid5 确定性）：文件同名 rename 覆盖(hash 同则无副作用)
      INSERT OR IGNORE 补行；sweep_stale_idem 清掉上一轮多余行
  b2…继续；产物与一次成功运行等价
```

### 18.3 回退（CP1 改一条 story 摘要）与影响面继承

```
用户在 StageConfirm 改 story S2 的 summary → rollback(target=link_identify, v=1, revised)
  BEGIN IMMEDIATE:
    diff: S2.fields_changed=[summary] → 下游判定 unaffected；新增/删除为空
    v1→superseded；put v2(user_revised)；point_write/case_generate 的 active 行
      graph_run_id 改挂新 run、status 保持 active（评审状态原样）
    task 切新 run/派生 thread
  COMMIT → start_run_from_plan(entry_state with AgentPlan)
  link_identify 重跑；point_write 因全部 unaffected：
    执行器读 active PointPlan 直接继承（不重调 LLM），仅当存在 added story 时对新增单元跑批次
  case_generate 同理只补 affected/新增点；UI 可见绝大多数既有评审成果保留
```

> 注：unaffected 直接继承的判定在节点入口做（`get_active_chain` + impact 标记），继承的产物写 `inherited_from_run` 留痕，不产生新 LLM/检索调用与费用。

### 18.4 知识库写入两阶段（见 §11.4 时序图）补充异常路径

- confirm 时 ReMe 超时：提案留 pending + fail_count+1，同 token 可重试；幂等键保证不会对 ReMe 重复提交（重试前先按客户端幂等键查本地是否已有成功 WriteResult）。
- confirmed 但 needs_manual_check：工作区页提案列表显示黄色"待人工核对"，提供"我已核对"按钮（仅置本地位，不回调 ReMe）。

---

## 19. 依赖、构建与部署

### 19.1 后端依赖（server/pyproject.toml，版本下限）

| 包 | 用途 | 备注 |
|---|---|---|
| fastapi >=0.115 / uvicorn[standard] | HTTP/SSE/静态托管 | uvicorn 单 worker |
| langgraph >=0.2 | 图编排 | 版本以 S2 验证结果锁定 |
| langgraph-checkpoint-sqlite | 断点续跑 | 独立 checkpoints.db |
| langchain-openai | DeepSeek 兼容接入 | 仅用 chat model |
| openai | langchain 传递依赖，异常类型翻译用 | 版本随 langchain |
| pydantic >=2.7 | 全部契约 | |
| mistune | 用例 MD 解析 | 纯 Python 无原生依赖 |
| json-repair | LLM JSON 容错 | |
| pyyaml | front-matter | |
| httpx | ReMe service 模式/export 测试 | |
| pytest / pytest-asyncio / anyio | 测试 | |
| import-linter | 门禁：graph→不得 import reme_writer | CI 契约 |

### 19.2 前端依赖

`react / react-dom / react-router-dom / @tanstack/react-query / zustand / react-markdown / remark-gfm / codemirror @codemirror/lang-markdown / vite / typescript / vitest / msw`。Node ≥20，构建 `web/dist` 由后端 StaticFiles 挂载（含 `/assets/*` 长缓存与 SPA fallback 到 index.html）。

### 19.3 一键脚本 scripts/dev.sh

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m venv .venv && source .venv/bin/activate
pip install -e ./server                               # 含 server/pyproject 依赖
[ -f server/.env ] || cp server/.env.example server/.env
python -m tester_agent.cli init-db                    # 迁移+内置 agent 种子+config 单行
(cd web && npm install && npm run build)              # 首次/前端变更时
exec uvicorn tester_agent.main:app --host 127.0.0.1 --port 8080 --workers 1
```

- 提供 `server/tester_agent/cli.py`：`init-db`（迁移+种子）、`reap`（手动触发 Reaper/对账，排障用）、`check`（DB/目录/依赖体检）。
- `.env.example` 列 §13.1 全部变量；不写任何真实 key。
- 生产/日常使用形态即"跑脚本 → 浏览器开 http://127.0.0.1:8080"；无 nginx、无外部数据库。

### 19.4 初始化种子（init-db 幂等）

1. 执行未应用的迁移（§3.2）；
2. config 单行不存在则插入默认 model_config（api_key 空，首启引导页要求填写）与 §13.2 默认 runtime_config；
3. 内置 agent 不存在则插入：id 固定 `builtin-case-designer`，agent_type=`case_designer`，builtin=1，config 用 §13.2 种子；
4. 不创建默认工作区（知识库配置必须用户显式填写，符合 PRD US7.1）。

### 19.5 数据备份（一期最简，WP-X2 已接线）

- 全部状态在 data/ 目录；备份 = 冷拷贝目录（建议先停服务，或使用 sqlite `.backup` 命令对两个 db 做热备份后连同 workspaces/ 打包）；
- `python -m tester_agent.cli backup <out_dir>`：对 app.db/checkpoints.db 执行 SQLite 在线备份（backup API）再连同 workspaces/ 复制；checkpoints.db / workspaces 缺失可跳过，app.db 缺失返回非 0；不做自动调度（见根 README）。

---

## 20. Prompt 模板 v1（server/prompts/）

每个模板文件头：

```yaml
---
version: "2026-09-26.1"
node: link_identify
---
```

### 20.1 system.shared.md（所有生成类节点拼接）

```
你是资深测试设计助手，服务于测试用例生成任务。
铁律：
1. 只能依据 <requirement> 与 <knowledge> 中给出的信息作答，不得引入未提供的业务事实；
2. 引用知识时只能使用 <knowledge> 中条目标注的 ID（形如 [ID:ent_xxx]），严禁编造 ID；
3. 信息不足以判断时，在输出的 clarifications 中提出具体问题，不要自行假设；
4. 输出且仅输出符合约定 JSON Schema 的 JSON，不输出解释性文字、Markdown 代码块标记。
```

### 20.2 intake.ambiguity.md（可配置关闭）

```
输入：<requirement_clauses> 条款标题与摘要（不含全文细节）。
任务：判断需求是否缺少生成测试用例所必需的信息：用户角色/触发入口/前置数据/
关键业务规则/异常处理约定是否存在明确描述。
输出：{"clarifications":[{"question":"","options":["…可选"]}]}；
没有实质缺失时输出空数组；不得为了提问而提问（措辞、风格类问题不提）。
```

### 20.3 link_identify.main.md

```
输入：
<requirement_clauses> 需求条款（标题路径+摘要）
<knowledge> 知识库中全部相关测试链路/用户故事索引摘要（每条带 [ID]、类型与归属链路）。
任务：
1. 判断需求涉及哪些已有链路与用户故事（hit=true，entry_id 必须取自 <knowledge> 的 ID）；
2. 知识库没有对应项但需求明显需要的，输出到 new_suggestions（仅建议，不写库）；
3. 给出每条 story 与需求关联的 rationale，并在 related_clause_ids 引用条款。
置信度规则：标题与摘要均明确对应≥0.8；主题相关但需推断 0.4~0.7；低于 0.4 不要输出为命中。
输出 schema：（贴 §7.4① 的 JSON Schema）
```

### 20.4 point_write.main.md（按故事批次）

```
输入：本批 <stories>（已用户确认）+ 其关联 <requirement_clauses> 原文
+ <knowledge> 业务规则/主流程用例/历史缺陷段落（带 [ID]）。
任务：为每个 story 设计测试点，覆盖正常/异常/边界/权限，历史缺陷须体现为回归测试点；
每个测试点必须能在 clause_ids 与 source_entry_ids 中指出依据（白名单 ID）。
一条 story 的测试点数量 3~8 个，按业务优先级排序，P0 仅用于主流程关键路径。
输出 schema：（贴 §7.4②）
```

### 20.5 case_generate.main.md（按测试点批次）

```
输入：本批 <points> + 每点相关 <requirement_clauses> 原文
+ <knowledge> 接口/DB/缺陷回归/相关规则段落（带 [ID]）。
任务：将每个测试点展开为可执行测试用例：
- 步骤具体到操作动作与数据，预期可判定、与步骤对应；
- 接口/DB 类知识要落到步骤或测试数据中，不得只在标题体现；
- trace_refs.entry_ids 只填 <knowledge> 白名单 ID；clause_ids 填实际覆盖条款。
每个 point 默认产出 1~3 条用例，按 <point id="…"> 分段输出。
输出 schema：（贴 §7.4③）
```

### 20.6 retrieve/multi_query.md 与 retrieve/rerank.md

```
[multi_query]
输入：检索意图（某阶段的 story/point 描述）。
输出 n-1 条不同表述的检索词：1 条关键词式（去修饰、保留业务名词），
其余为同义改写（换术语/换句式，不得改变意图）。
{"queries":[{"channel":"keyword","text":""},{"channel":"rewrite","text":""}]}

[rerank]
输入：意图 + 候选条目（id/标题/摘要，批量）。
按"对当前阶段测试设计的有用程度"打分 0~1：
- 直接支撑本阶段产物所需事实≥0.7；背景相关 0.3~0.6；仅主题沾边<0.3。
输出 [{"entry_id":"","score":0.0,"reason":"≤20字"}]；id 必须来自输入。
```

### 20.7 模板管理规则

- 模板加载顺序：agent.config.prompts_dir（自定义目录）→ 内置目录；同名文件自定义覆盖内置；
- 每次 LLM 调用快照记 prompt_template_ver（文件头 version）；模板变更必须升 version；
- eval smoke（§15.3）对模板版本敏感：改模板即跑回归，指标对比按 version 展示。

---

## 21. 开工前检查清单与文档索引

### 21.1 编码开工前必须关闭项

| 项 | 内容 | 责任切片 |
|---|---|---|
| S1 | ReMe SDK 三能力探测结论（metadata_filter / entry_version / passage），决定 §8.4 是否走镜像方案 | C1 之前 |
| S2 | interrupt()+Command(resume) 与派生 thread 在当前 langgraph 版本的验证；不通过则启用 run_from_stage 编排器 | C3 之前 |
| DeepSeek 账号 | base_url/model 名、流式 usage 返回确认、JSON mode 支持确认 | C1 |
| Q1 用例模板 | **closed（WP-X2）**：§4.2 v1 即为一期定稿 | C3 / C6 |
| 目录初始化 | TESTER_AGENT_DATA_DIR 落点、127.0.0.1 绑定确认 | C0 |

### 21.2 可并行、不阻塞编码项

S3/S4/S5 真实环境标定（一期 deferred/partial，见 tech-design §8）、Q8 合规结论、Q9 耗时上限；S6/S7 已按预案关闭。

### 21.3 文档索引与维护约定

| 文档 | 职责 |
|---|---|
| [PRD.md](file:///Users/test/Documents/python_project/testerAgent/docs/PRD.md) | 需求与开放问题（Q1~Q12） |
| [tech-design.md](file:///Users/test/Documents/python_project/testerAgent/docs/tech-design.md) | 架构决策（D1~D16）、评审记录（R1~R39）、spike（S1~S7） |
| detailed-design.md（本文） | 实现级契约；编码过程中与代码不符时**先改本文再改码** |

维护规则：

1. 本文任何接口/DDL/错误码/Schema 变更，必须同步改对应章节并升文档版本（头表），变更摘要写在头表下方；
2. 新增错误码先登记 §14 再写代码；新增 SSE 事件先登记 §10.4；
3. C0~C6 每切片完成在头表追加一行完成记录（切片、日期、偏离设计项）；
4. 代码中的接口签名以本文为准绳，code review 发现偏差按"设计错改设计、代码错改代码"处理，不允许静默分歧。

