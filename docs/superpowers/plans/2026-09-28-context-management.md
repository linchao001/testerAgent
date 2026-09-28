# 上下文管理层技术方案：三分区 / 作用域隔离 / 确定性组装（WP-30 ~ WP-33）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在智能体中新增独立的上下文管理层，把"上下文存放"与"LLM 调用窗口组装"分离：三分区（P0 静态 / P1 知识 / P2 任务）独立存放与预算，确定性打分淘汰只作用于未来组装窗口、永不触碰审计数据；条目级 scope（task/batch/item）× phase（shared/design/write）实现批次重复任务与测试设计/用例编写两阶段的强制隔离；支持运行时指令干预。

**Architecture:** 新增叶子包 `server/tester_agent/context/`（models / store / policy / budget / assembler / scopes / journal / rebuild / intervention）。所有 LLM 调用点经唯一入口 `assembler.assemble(profile)` 获取消息序列；ContextStore 以 run/conversation 为键内存驻留，动作恒写 `context_journal`（005 迁移，仅元数据 + digest），重启从审计数据 + journal 重放重建。写入定性由 contextvars 作用域栈在 `wrap()` / batch runner 自动继承。

**Tech Stack:** Python 3.11+, Pydantic v2（域模型）, langchain_core.messages（仅消息类型，外部库）, SQLite（005 迁移，WAL）, FastAPI（调试/干预 API）, 现有 FakeLLM + pytest 体系。

**Spec:** [docs/superpowers/specs/2026-09-28-context-management-design.md](../specs/2026-09-28-context-management-design.md)（v0.5，下文引 §N 即该文档章节）

## Global Constraints

- 六条不变量 I1~I6（spec §1）为硬约束：审计不灭 / 可重放 / 钉住+tombstone / 淘汰必归因 / 确定性零 LLM / 层次隔离。
- context 层为**叶子层**：只允许 import `domain`、`prompts`、`utils/errors/logging`、第三方库（pydantic、langchain_core.messages）；**禁止** import `runtime` / `graph` / `memory` / `adapters` / `store`（DAO 经构造函数注入，不直接 import store 包的具体类；类型注解用 TYPE_CHECKING 或 Protocol）。
- 迁移只增不改：新增 `005_context_journal.sql`，不修改 001~004。
- DAO 遵循既有协议：workspace 强制过滤、`INSERT OR IGNORE` 幂等、`(created_at, id)` 降序 key-set 分页、created_at 仅插入时写入、WAL 并存。
- 命名规避：既有 [runtime/context.py](../../server/tester_agent/runtime/context.py) 持有 `AppContext`/`TaskContext`。新包名为 `tester_agent.context`（与模块 `tester_agent.runtime.context` 不冲突），但在两者同时出现的文件中强制写法：
  `from ..runtime.context import AppContext, TaskContext`（不改名）与 `from ..context.store import ContextStore`；禁止 `from ..runtime import context as ctx_mod` 之类别名。
- 特性开关 `context.enabled=true`（默认开），关闭时所有消费点退回现状行为（40 条历史 / 全额注入），保证可回退。
- 单机单进程单 worker；ContextStore 内存态不做跨进程一致性。
- TDD per task：failing test → implement → pass；全量验收 `cd server && python -m pytest tests/ -W error`。
- 不做旧任务热迁移：005 表对历史任务为空，rebuild 对历史任务产出空 store（等同关闭），新 run 完整生效。

**Execution waves（顺序执行）：**

| Wave | WP | Tasks | Testable outcome |
|---|---|---|---|
| A 纯函数层 | WP-30 | 1–7 | store/policy/budget/assembler/scopes/journal 单测全绿；零项目内反向依赖 |
| B 消费面接线 | WP-31 | 8–11 | chat 历史窗 + tool_agent 轮内摘要 + case_item 隔离；既有 chat 回归全绿 |
| C 控制环/持久化 | WP-32 | 12–16 | 005 迁移 + T1 钩子 + rebuild + 调试 API + 场景 13 + 门禁 |
| D 运行时干预 | WP-33 | 17–19 | 指令执行器 + API + context_* 工具组 + SSE |

---

## File map

| Path | 动作 | Responsibility |
|---|---|---|
| `server/tester_agent/context/__init__.py` | 新增 | 包出口：ContextStore / assemble / profiles / ExecScope 公共 API |
| `server/tester_agent/context/models.py` | 新增 | ContextPartition / EntryStatus / EntryKind / ProfileName / ContextEntry / AssemblyReport / ContextView 等 Pydantic 模型 |
| `server/tester_agent/context/tokens.py` | 新增 | `estimate_tokens`（自 ops_b 平移）；ops_b 改为 re-export，签名不变 |
| `server/tester_agent/context/budget.py` | 新增 | ProfileBudget / ProfileName 预算表、校验、10% 跨区余量 |
| `server/tester_agent/context/scopes.py` | 新增 | ExecScope（phase/step/batch/item）+ contextvars 栈 + `scope()` async context manager + 过滤器谓词 |
| `server/tester_agent/context/policy.py` | 新增 | 纯函数打分（recency/referenced/goal_overlap/kind/dup）、裁剪次序、T3 候选、tombstone 文本 |
| `server/tester_agent/context/store.py` | 新增 | ContextStore（内存三分区、状态机、pin、append 幂等、asyncio 锁）；JournalSink Protocol 注入 |
| `server/tester_agent/context/assembler.py` | 新增 | `assemble(store, profile, *, goal, ...)` 唯一组装入口：scope 过滤→分区装配→预算裁剪→消息序列+Report |
| `server/tester_agent/context/p0.py` | 新增 | P0 引导：PromptLoader 加载模板集→冻结 SystemMessage 段 + `p0_version` |
| `server/tester_agent/context/journal.py` | 新增 | JournalRow / ContextJournalDAO（put_batch / list key-set 分页 / list_actions 重放） |
| `server/tester_agent/context/rebuild.py` | 新增 | rebuild_store(owner, daos, journal)：审计数据 + journal 重放恢复 store |
| `server/tester_agent/context/intervention.py` | 新增 | ContextCommand / selector 解析 / execute(store, cmd) 确定性执行器 |
| `server/tester_agent/context/registry.py` | 新增 | ContextRegistry：进程级 owner→store 字典（AppContext 持有），start/get/evict |
| `server/tester_agent/store/migrations/005_context_journal.sql` | 新增 | context_journal 表 + 索引 |
| `server/tester_agent/store/db.py` | 修改 | DEFAULT_RUNTIME_CONFIG 增 context.* 键 |
| `server/tester_agent/store/models.py` | 修改 | 新增 ContextJournalDAO（DAO 组追加字段，默认 None 兼容旧夹具） |
| `server/tester_agent/runtime/context.py` | 修改 | AppContext 增 `context_registry`；TaskContext 增 `context_store`（默认 None）；DAOs 增 journal |
| `server/tester_agent/graph/retrieval/ops_b.py` | 修改 | estimate_tokens 改为从 context.tokens re-export（交接单登记） |
| `server/tester_agent/graph/tool_agent.py` | 修改 | 增可选 `before_model_hook`（消息压缩回调）；默认 None 行为不变 |
| `server/tester_agent/runtime/chat_agent.py` | 修改 | 40 条历史 → chat profile 组装；turn append；开关关闭走旧路径 |
| `server/tester_agent/graph/wrap.py` | 修改 | 节点执行前按 state 推入 ExecScope（phase/step_id）；结束弹出 |
| `server/tester_agent/graph/control/nodes.py` | 修改 | T1 钩子（step 完成 P1 降级、superseded、repair 归档）；移除 reflection_log[-20:] 硬编码 |
| `server/tester_agent/graph/batch.py` | 修改 | run_in_batches 推入 batch_id/item_key 作用域；batch 结束 evict |
| `server/tester_agent/graph/nodes/case_generate.py` | 修改 | 参考接线：case_item profile 组装 + item 过程 append（其余节点同构） |
| `server/tester_agent/api/context.py` | 新增 | 视图 / evictions 分页 / playground / commands 四组端点 |
| `server/tester_agent/main.py` | 修改 | 挂载 context 路由 |
| `server/tester_agent/tools/registry.py` | 修改 | ToolBuildContext 增 context_store 可选字段；注册 context_* 工具组（WP-33） |
| 测试（10 个新文件，~96 用例） | 新增 | 见各 Task |

---

## 1. 域模型设计（context/models.py）

所有模型 Pydantic v2，`model_config = ConfigDict(extra="forbid")`，与 domain.py 同款 StrEnum 风格。

```python
class ContextPartition(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"

class EntryStatus(StrEnum):
    ACTIVE = "active"
    DEMOTED = "demoted"
    EVICTED = "evicted"

class EntryKind(StrEnum):
    METHODOLOGY = "methodology"     # P0
    KB_BLOCK = "kb_block"           # P1
    PLAN = "plan"                   # P2
    DECISION = "decision"           # P2 人工决策（高 kind_weight）
    ARTIFACT_DIGEST = "artifact"    # P2 产物摘要
    TOOL_RESULT = "tool_result"     # P2
    CHAT_TURN = "chat_turn"         # P2
    REFLECTION = "reflection"       # P2
    GOAL = "goal"                   # P2 当前目标
    OUTLINE_DIGEST = "outline"      # P2 shared：确认大纲摘要

class ScopeLevel(StrEnum):
    TASK = "task"
    BATCH = "batch"
    ITEM = "item"

class Phase(StrEnum):
    SHARED = "shared"
    DESIGN = "design"
    WRITE = "write"

class ProfileName(StrEnum):
    CHAT = "chat"
    PLAN = "plan"
    EXECUTE = "execute"
    REFLECT = "reflect"
    REVIEW = "review"
    CASE_ITEM = "case_item"

class EntryRefs(BaseModel):
    payload_ref: str | None = None
    trace_id: str | None = None
    snapshot_id: str | None = None
    message_id: str | None = None

class ContextEntry(BaseModel):
    entry_id: str                       # 幂等键（调用方确定性生成，见 §3.1）
    partition: ContextPartition
    entry_kind: EntryKind
    status: EntryStatus = EntryStatus.ACTIVE
    content: str                        # 正文；demote 后由 store 替换为 tombstone 文本
    digest: str                         # 一行要点（append 时程序化生成，§4.4）
    tokens_est: int = 0
    pinned: bool = False
    refs: EntryRefs = EntryRefs()
    # 作用域维度
    scope_level: ScopeLevel = ScopeLevel.TASK
    phase: Phase = Phase.SHARED         # P0/P1/task.shared 用 SHARED；过程条目 DESIGN/WRITE
    step_id: str | None = None
    step_seq: int = 0                   # 计划内序号（recency 距离用，避免字符串比较）
    batch_id: str | None = None
    item_key: str | None = None
    turn_seq: int = 0                   # chat 场景轮次序号
    # 生命周期
    created_at: str                     # utcnow_iso，仅插入
    supersedes: str | None = None       # 版本更替：指向被替代 entry_id
    source: str = "runtime"             # runtime | rebuild | manual

class AssemblyReport(BaseModel):
    profile: ProfileName
    policy_version: str
    p0_version: str
    per_partition_tokens: dict[str, int]
    included: list[str]                 # entry_id 顺序
    demoted_shown: list[str]            # 以 tombstone 形式出现的 entry_id
    evicted_in_assembly: list[dict]     # [{entry_id, reason}] 本次组装新出窗
    budget_overrides: list[str]         # pinned 超预算放行的 entry_id
    frozen: bool = False

class ContextView(BaseModel):            # GET 视图响应
    owner: dict                          # {scope, owner_id}
    goal: str | None
    entries: list[ContextEntry]          # 元数据为主，P1 全文不在此接口
    totals: dict[str, int]               # 按 partition/status 计数与 tokens
```

**约束：**
- P0/P1 条目 `scope_level` 恒为 TASK（P1 批次级例外见 §6.2：挂载到 case 批次的 KB 块允许 `scope_level=BATCH`，但 `partition=P1` 不变）。
- `phase` 与 spec §15.5 对齐：P0=SHARED；P1 按挂载时 ExecScope.phase（design/write）；P2 task 级条目可为 SHARED/DESIGN/WRITE，batch/item 恒 WRITE。
- entry 不可变字段：entry_id/partition/created_at/scope_level/phase/batch_id/item_key（状态机动作只改 status/content/pinned）。

## 2. 执行作用域（context/scopes.py）——防线 1 的实现

```python
class ExecScope(BaseModel):
    phase: Phase = Phase.SHARED
    step_id: str | None = None
    step_seq: int = 0
    batch_id: str | None = None
    item_key: str | None = None
    turn_seq: int = 0

_current_scope: contextvars.ContextVar[ExecScope | None]
# contextvars 保证 asyncio 任务间隔离：节点/批次各自 push，不串标（spec §15.6 防线 1）

@asynccontextmanager
async def scope(**kwargs):
    """压栈：未传字段从父 scope 继承（如 item 继承 batch 的 batch_id）。"""

def current_scope() -> ExecScope: ...    # 无栈时返回默认 ExecScope()
```

- `store.append(...)` 未显式传 scope 字段时，自动取 `current_scope()` 快照写入条目——**条目产生即定性，之后不可改**。
- 推入点（§7 接线详述）：`wrap()` 在节点执行前按 state 推 phase+step_id+step_seq；`run_in_batches` 推 batch_id；逐条用例处理推 item_key；`run_chat_turn` 推 turn_seq。
- 过滤器谓词（纯函数，assembler 与单测共用）：

```python
def visible_in_window(e: ContextEntry, *, profile: ProfileName, s: ExecScope,
                      current_phase: Phase) -> bool:
    if e.partition == P0: return True
    if e.status == EVICTED: return False
    if profile == CASE_ITEM:
        # task.shared 恒见；task.design/write 对侧不见；只认当前 batch/item
        if e.scope_level == ITEM: return e.item_key == s.item_key
        if e.scope_level == BATCH: return e.batch_id == s.batch_id
        return e.phase == SHARED or e.phase == WRITE and current_phase == WRITE
    # plan/execute/reflect/review：task 级按 phase 可见性；batch/item 仅在同键调用可见
    ...
```

## 3. ContextStore（context/store.py）

### 3.1 关键签名（冻结，后续 WP 只增不改）

```python
class JournalSink(Protocol):
    async def record(self, rows: list[JournalRow]) -> None: ...   # put_batch 语义

class ContextStore:
    def __init__(self, *, owner_type: Literal["task","conversation"], owner_id: str,
                 workspace_id: str, policy_version: str = "cp-v1",
                 journal: JournalSink | None = None): ...

    # ---- 写（均在 per-owner asyncio.Lock 内；均写 journal；幂等）----
    async def append(self, entry: ContextEntry, *, scope: ExecScope | None = None) -> bool
        # entry_id 已存在（任意状态）→ INSERT OR IGNORE 语义：忽略返回 False
    async def demote(self, entry_id: str, reason: str) -> None
        # active→demoted；content 替换为 tombstone（policy.make_tombstone）；非法迁移报错
    async def evict(self, entry_id: str, reason: str) -> None
        # active/demoted→evicted
    async def pin(self, entry_id: str, *, reason: str) -> None
    async def unpin(self, entry_id: str, *, reason: str) -> None
    async def set_goal(self, text: str, *, reason: str) -> None   # append kind=GOAL + 旧 goal demote(superseded)
    async def close_batch(self, batch_id: str) -> int             # evict 批次全部条目，返回条数
    async def bulk_demote(self, predicate, reason: str) -> list[str]

    # ---- 读（纯内存，同步函数；确定性）----
    def entries(self, partition: ContextPartition | None = None,
                status: EntryStatus | None = None) -> list[ContextEntry]
    def get(self, entry_id: str) -> ContextEntry
    def goal_text(self) -> str | None
    def snapshot_state(self) -> dict                            # rebuild 对比用（条目 id:status 映射）
```

- 状态机合法迁移：ACTIVE→{DEMOTED,EVICTED}、DEMOTED→{ACTIVE(refresh 专用),EVICTED}、EVICTED 终态；对 EVICTED pin / 任意非法迁移抛 `errors.ValidationError`。
- `append` 幂等：同 entry_id 直接返回 False（不更新，对齐 put_batch INSERT OR IGNORE 哲学）；entry_id 由调用方按确定性规则生成：`f"{kind}:{source_id}:v{version}"` / `f"chat:{message_id}"` / `f"tool:{tool_call_id}"` / P1 复用 `entry_key(...)` 同构键。
- journal 落库失败：内存操作仍生效，记 degraded 日志 + store 内 `degraded_journal: list[JournalRow]`（下次成功补写），**不阻断调用方**（spec §9）。

### 3.2 并发

- 单 owner 单锁（asyncio.Lock），所有写串行；读为无锁快照（dict 值引用不可变——状态机动作以替换 ContextEntry 对象实现，不就地改字段，保证 assemble 读半状态）。
- 注册表 `ContextRegistry`（挂 AppContext）：`start_owner(...) -> ContextStore`、`get(owner)`、`await restore(owner, daos...)`（rebuild）、任务终态 `evict_owner`（仅摘内存，journal 保留）。

## 4. 策略与组装（policy.py / budget.py / assembler.py / p0.py）

### 4.1 预算（budget.py）

```python
class ProfileBudget(BaseModel):
    p0: int; p1: int; p2: int
    def total(self) -> int: return self.p0 + self.p1 + self.p2
    SPILL_RATE: ClassVar[float] = 0.10   # 单分区最多侵占其他区余量 10%

PROFILES: dict[ProfileName, ProfileBudget] = {   # spec §5，D3 建议值
    CHAT:      (1500, 3000, 8000),
    PLAN:      (3000, 4000, 6000),
    EXECUTE:   (3000, 8000, 6000),
    REFLECT:   (2000, 2000, 4000),
    REVIEW:    (2500, 8000, 8000),
    CASE_ITEM: (2000, 6000, 4000),
}
```

- 校验：任一值 ≤0 或 `total > model_context_window * 0.8`（model 窗口由 ConfigDAO model_dict 提供，校验在组装期惰性做，context 层不 import settings）→ ValidationError。
- 运行时 budget 指令产出 `ProfileBudget` 覆盖实例，存 store（不落 DB，进程内生效，登记偏离：与 WP-27 内存作业表同口径）。

### 4.2 P0 引导（p0.py）

```python
def bootstrap_p0(*, loader: PromptLoader, template_names: list[str],
                 extra_static: str = "") -> tuple[list[BaseMessage], str, int]:
    """返回 (P0 消息段, p0_version, tokens)。
    p0_version = sha256("|".join(f"{name}:{tpl.version}" 排序) + extra_static)[:12]
    模板内容按 template_names 固定顺序拼接——同 run 两次调用字节一致（I2）。"""
```

模板集（WP-31 接线时冻结）：`system.shared` + 方法论模板（新增 `prompts/methodology.md`，等价类/边界值/判定表/场景法规则，WP-31 Task 9 随接线新增，带 YAML version 头）。chat 场景的 _SYSTEM 文案并入该模板或经 extra_static 注入（二选一，实施时登记交接单）。

### 4.3 打分（policy.py，纯函数）

```python
@dataclass(frozen=True)
class ScoreInput:
    entry: ContextEntry
    current_step_seq: int
    current_turn_seq: int
    goal: str
    referenced_ids: frozenset[str]
    window_contents: list[str]      # 已在窗口条目正文，用于 dup

def score(x: ScoreInput) -> float:
    return round(
        0.35 * recency(x) + 0.30 * referenced(x) +
        0.20 * goal_overlap(x) + 0.15 * kind_weight(x.entry.entry_kind)
        - dup_penalty(x), 6)
```

| 因子 | 实现 |
|---|---|
| recency | step 场景 `1/(1+current_step_seq - entry.step_seq)`；chat 用 turn_seq |
| referenced | entry_id ∈ referenced_ids → 1.0（数据来自 retrieve_pipeline.close_loop 的 referenced 集，由接线点传入） |
| goal_overlap | 复用 pipeline._bigram_jaccard 同算法（在 policy 内重实现为纯函数，禁止 import graph）：`< floor(0.05)` 记 0 |
| kind_weight | plan/decision/outline/goal=0.9，artifact=0.7，reflection=0.6，tool_result=0.5，chat_turn=0.3，kb_block 不在 P2 打分 |
| dup_penalty | 与窗口内任一条目 bigram Jaccard > 0.85 → 0.5 |

裁剪次序（确定性 tiebreak）：score 升序；同分按 `(created_at ASC, entry_id ASC)`（对齐冻结分页协议精神）；pinned 不进候选，超预算放行并入 `budget_overrides`。

T3 目标漂移候选：`not pinned and goal_overlap==0 and step_distance > W(默认 5)`。

### 4.4 tombstone 文本

```python
def make_tombstone(e: ContextEntry) -> str:
    where = e.refs.payload_ref or e.refs.trace_id or e.refs.snapshot_id or e.refs.message_id or "journal"
    re_fetch = "；KB 知识可用 retrieve_kb 重取" if e.partition == P1 else ""
    return f"〔已归档 {e.entry_kind.value} #{e.entry_id}：{e.digest}（全文：{where}{re_fetch}）〕"
```

`digest` 程序化生成（D1，零 LLM）：正文首行/首句截断 80 字；ToolResult 用既有 `tools/trace.py:args_digest` 同构摘要；Artifact/Plan digest 由接线点显式传入（标题+版本+条目数，质量高于截断）。

### 4.5 组装主流程（assembler.py）

```python
async def assemble(
    store: ContextStore, profile: ProfileName, *,
    p0_messages: list[BaseMessage],            # 由 p0.bootstrap_p0 在 run/会话启动时冻结一次
    model_window: int,
    scope: ExecScope | None = None,            # 缺省 current_scope()
    referenced_ids: frozenset[str] = frozenset(),
    persist_evictions: bool = True,            # playground=False 时不写 journal
) -> AssemblyResult:                          # (messages: list[BaseMessage], report: AssemblyReport)
```

步骤（对应 spec §5 与图示四步）：

1. **scope 过滤**：按 §2 `visible_in_window` 过滤；ITEM/BATCH 不匹配条目在此物理排除（不 demote、不 journal、无 tombstone——spec §15.2）。
2. **分区装配**：
   - P0：`p0_messages` 原样前置（冻结段，不重算）。
   - P1：ACTIVE，按挂载 step_seq 降序、同 (entry_id, version) 去重（与 ops_b 去重语义一致）。
   - P2：pinned ACTIVE 全量 → 其余 ACTIVE（chat profile 最近 K=6 轮 CHAT_TURN 整体保留）→ DEMOTED 条目只取 tombstone 文本按 created_at 合并为单条 SystemMessage（最多 20 行，超出仅保留计数行）。
3. **预算裁剪**：分区各自计 tokens（`context.tokens.estimate_tokens`）；超预算先在区内按 §4.3 出窗（evict_in_assembly：demote + journal reason='budget_cut'，T2）；区内裁完仍超 → 跨区 10% 余量；仍超 → pinned 放行 budget_override。T3 候选在裁剪前并入候选集（reason='goal_drift'）。
4. **输出**：消息序列（SystemMessage 分区，CHAT_TURN 转 Human/AIMessage，其余 SystemMessage 段）+ AssemblyReport；store.frozen 时跳过步骤 3 并在 report 标 frozen。

确定性（I2）：同 store 快照 + 同 profile + 同 scope + 同 policy_version ⇒ 消息序列逐条 content 相等（单测用两次 assemble 的 `[m.content for m in ...]` 全量对比 + 全序列 sha256）。

## 5. 持久化：005 迁移与 DAO

### 5.1 DDL（spec §7 的实现细化）

相对 spec §7 的增补列：作用域四列 + digest（重建 tombstone 必需）+ owner 泛化（会话可无 task）。

```sql
-- 005_context_journal.sql
CREATE TABLE IF NOT EXISTS context_journal (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  owner_type TEXT NOT NULL,             -- 'task' | 'conversation'
  owner_id TEXT NOT NULL,
  task_id TEXT,                         -- owner_type='task' 时 = owner_id；否则可空
  conversation_id TEXT,
  partition TEXT NOT NULL,             -- 'P0' | 'P1' | 'P2'
  entry_id TEXT NOT NULL,
  entry_kind TEXT NOT NULL,
  action TEXT NOT NULL,                -- append|demote|evict|pin|unpin|rebuild|goal|batch_close
  reason TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  tokens_est INTEGER NOT NULL DEFAULT 0,
  digest TEXT NOT NULL DEFAULT '',
  refs TEXT NOT NULL DEFAULT '{}',
  scope_level TEXT NOT NULL DEFAULT 'task',
  phase TEXT NOT NULL DEFAULT 'shared',
  step_id TEXT,
  batch_id TEXT,
  item_key TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ctx_journal_owner ON context_journal(owner_type, owner_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_ctx_journal_ws ON context_journal(workspace_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_ctx_journal_entry ON context_journal(owner_type, owner_id, entry_id, created_at);
```

- P0 不写 journal（无状态变化）；其余 append/demote/evict/pin/unpin 每动作一行；`goal`/`batch_close` 为复合动作标记行。
- 写入：`ContextJournalDAO.put_batch(rows)`，`INSERT OR IGNORE`（id=uuid4 hex 由 store 生成；重放幂等靠 action 行的业务唯一性由 rebuild 侧保证，见 §5.3）。
- 分页：`list_evictions(owner, cursor, limit, partition=None)` key-set `(created_at DESC, id DESC)`，与 WP-28 调试端点同款。

### 5.2 ContextJournalDAO（store/models.py）

```python
class ContextJournalRow(BaseModel): ...          # 列同名
class ContextJournalDAO:
    async def put_batch(self, rows: list[ContextJournalRow]) -> None
    async def list_by_owner(self, *, workspace_id, owner_type, owner_id,
                            cursor: str | None, limit: int,
                            partition: str | None = None) -> Page[ContextJournalRow]
    async def list_actions(self, *, workspace_id, owner_type, owner_id) -> list[ContextJournalRow]
        # rebuild 重放用：按 (created_at, id) 升序全量；量级=单任务动作数，内存可容
```

workspace 过滤强制（与既有 DAO 一致）；DAOs 组追加 `journal: ContextJournalDAO | None = None`。

### 5.3 rebuild（context/rebuild.py）

```python
async def rebuild_store(*, owner_type, owner_id, workspace_id, daos, journal_sink,
                        current_step_seq: int = 0) -> ContextStore
```

协议（spec §7）：
1. 新构造空 store（source='rebuild' 路径）。
2. **P2**：task  owner → ArtifactDAO 取 active 链（agent_plan 最新版 → PLAN pinned；最新 confirmed 产物 ARTIFACT_DIGEST；旧版本条目标 superseded 关系，状态由 journal 最终裁决）；conversation owner → MessageDAO 历史转 CHAT_TURN（turn_seq 按 created_at 排名）。
3. **P1**：TraceDAO 取该 owner 各 step 的 injected 集（trace 行含 injected_ids/candidates），按 current_step_seq 与 step_window 判 ACTIVE/DEMOTED；正文不重建（DEMOTED 只留 tombstone，ACTIVE 内容从 snapshot 偏移读或重新挂载——ACTIVE P1 只在紧邻 step 存在，重建场景由该 step 节点重新 retrieve，故 rebuild 对 P1 统一建 DEMOTED 占位，journal 有更近 append 者除外）。
4. **journal 重放**：按 list_actions 升序回放 action（demote/evict/pin/unpin/goal/batch_close），以 journal 为状态最终裁决；append 行用于补审计数据中不存在的条目（如 reflection/tool_result，content 用 digest 重建为 tombstone 态）。
5. 源数据缺失（引用的 artifact/message 已不存在）：跳过 + journal `reason='source_missing'`（rebuild 行），不抛。
6. 幂等：rebuild 结果只取决于审计数据 + journal 全量；重复 rebuild 的 `snapshot_state()` 相等。rebuild 本身只追加一行 action='rebuild' 标记，不重复 append 条目行（append 行已在首次运行写入）。

## 6. 消费面接线契约（WP-31/32）

### 6.1 统一调用形态

所有 LLM 调用点改造为同一形态（以能力节点为例）：

```python
# 旧：messages = [SystemMessage(...), *payload]；await llm.chat(messages)
# 新：
store = ctx.context_store  # TaskContext 注入；None 时走旧路径（开关/旧夹具兼容）
if store is None or not runtime_config.get("context.enabled", True):
    messages = _legacy_messages(...)
else:
    messages, report = await assemble(store, ProfileName.CASE_ITEM,
                                      p0_messages=store.p0_segment(),
                                      model_window=model_window,
                                      referenced_ids=frozenset(referenced))
```

`store.p0_segment()`：P0 段在 owner start 时 bootstrap 一次并缓存于 store（p0_version 随 Report）。

FakeLLM 测试约束：新增一个公共断言夹具 `assert_messages_from_assembler(monkeypatched_assemble_calls, ...)`，场景测试中核对所有消费点调用记录都经过 assemble（防线 4，spec §15.6）。

### 6.2 接线点清单

| 消费点 | 文件 | profile | append 时机 | T1/作用域钩子 |
|---|---|---|---|---|
| 会话轮 | runtime/chat_agent.py | CHAT | 用户消息 append(CHAT_TURN) 在锁内先于调用；assistant 落库后 append | 最近 K 轮 pinned 等价（整体保留）；turn_seq 递增 |
| 工具轮内 | graph/tool_agent.py | 跟随上游 | ToolMessage 产生后由 hook append(TOOL_RESULT) | `before_model_hook`：下一次 call_model 前对超阈 ToolMessage 调 demote(step_window)，以 tombstone SystemMessage 替换其窗口位；tool_trace 与 message 审计不动 |
| plan/replan | control/nodes.py | PLAN | agent_plan artifact 落库后 append(PLAN, pinned=True)；旧版 demote(superseded) | wrap 推 phase=DESIGN/WRITE 按 step.kind 映射（见下） |
| execute_step | control/nodes.py | EXECUTE | step 结束：该 step_id 的 P1 `bulk_demote(step_window)`；repair 完成旧 reflection demote(superseded) | wrap 推 step_id/step_seq |
| reflect | control/nodes.py | REFLECT | append(REFLECTION)；**删除 reflection_log[-20:] 硬编码**，窗口由策略接管（state 只存全量计数/最新 id，正文不进 checkpoint） | — |
| review 子任务 | graph/subtasks/* | REVIEW | 子任务结束 append(ARTIFACT_DIGEST) | 子任务线程内 scope 独立（owner 仍为 task，phase=WRITE） |
| 检索挂载 | retrieve_pipeline 各消费节点 | 跟随 | outcome.items → 每 item append(P1/KB_BLOCK, refs={trace_id,snapshot_id}, tokens 取 item.tokens_est，phase/ batch_id 从 current_scope) | 管线 persist=False（playground）时不 append |
| 批次用例 | graph/batch.py + nodes/case_generate.py | CASE_ITEM | batch 开始推 scope(batch_id)；每条推 scope(item_key=f"{batch_id}:{point_id}")，条目过程 append（ITEM） | 批次终态 `store.close_batch(batch_id)`（journal batch_closed）；repair 同 item_key 不换键 |
| confirm API | api/tasks.py（WP-32 小改） | — | 确认成功后对对应 ARTIFACT_DIGEST pin(reason='human_confirmed')；大纲确认另 append(OUTLINE_DIGEST, phase=SHARED, supersedes=旧 digest) | spec §15.5 跨阶段唯一通道 |

step.kind → phase 映射（wrap 内纯函数映射表）：
`intake_parse / coverage_design / point_design → DESIGN`；`case_generate / repair / review_* → WRITE`；`await_human →` 不覆盖父 phase。

### 6.3 tool_agent hook 接口（默认 None，行为零变化）

```python
async def run_tool_agent(..., before_model_hook: Callable[[list[BaseMessage], int], Awaitable[list[BaseMessage]]] | None = None):
# call_model 内：msgs = state["messages"];
#   if before_model_hook: msgs = await before_model_hook(msgs, step)
```

hook 实现放在接线侧（chat_agent 或 graph 新薄模块 `graph/context_hooks.py`——允许 graph→context），保证 context 包不感知 LangGraph。

## 7. wrap 与状态隔离（防线 1/3）

- `wrap._wrapped` 在 `node_fn(ctx, state)` 前后增加：
  - 前：从 state 取 current plan（plan artifact 经 state 的 plan 指针/序列化 plan）解析 running step → `async with scope(phase=map_phase(step.kind), step_id=step.step_id, step_seq=idx)`；取不到 plan 时 scope() 空栈（默认 SHARED，行为等同现状）。
  - 后：contextmanager 自动弹栈（异常也弹）。
- state（TaskState）**不新增**上下文字段；reflection_log 保留但不再截断消费（WP-32 行为对齐后，节点从 store 读窗口；state 中 reflection_log 仅留作 checkpoint 兼容，标注 deprecated，后续包删除）。
- 大正文不进 checkpoint 原则不变：item 过程条目只存内存 store + journal 元数据，崩溃后 rebuild 对 item 过程不复活（spec §15.3：已完成 item 不复活）。

## 8. API 层（api/context.py）

沿用 api/common.py 的分页/错误/Idempotency-Key 辅助与 workspace 404 口径。

| 方法/路径 | 请求 | 响应/行为 |
|---|---|---|
| GET `/tasks/{id}/context` | — | ContextView（P1 不含 passage 全文，附 snapshot_id） |
| GET `/conversations/{id}/context` | — | 同上（owner_type=conversation） |
| GET `/tasks/{id}/context/evictions` | `?cursor&limit&partition` | key-set 分页 journal（action∈demote/evict/goal/batch_close/pin） |
| POST `/workspaces/{id}/context/playground` | `{profile, goal?, p1_items?, p2_entries?, scope?}` | 临时 store 组装预览，**persist=False 零业务表写入**（对齐 WP-28 retrieve playground 断言方式：前后各表行数快照） |
| POST `/tasks/{id}/context/commands` | ContextCommand + `Idempotency-Key` | 干预执行（§9） |
| POST `/conversations/{id}/context/commands` | 同上 | 同上 |

任务终态（failed/cancelled/completed 后，completed 允许只读与 refresh）写指令 → 409 STATE_CONFLICT；跨 workspace → 404。

SSE：bus.emit(`context_command_executed`, {action, selector, affected[], idem_key})；budget/goal 变更额外发 `context_policy_changed`。

## 9. 运行时干预（intervention.py，WP-33）

```python
class ContextAction(StrEnum):
    PIN="pin"; UNPIN="unpin"; FORGET="forget"; REFRESH="refresh"
    SET_GOAL="set_goal"; BUDGET="budget"; SHOW="show"; RECALL="recall"
    FREEZE="freeze"; UNFREEZE="unfreeze"

class ContextCommand(BaseModel):
    action: ContextAction
    selector: str = ""           # id: | kind: | step: | recent: | item: | batch: | all
    arg: str | None = None
    confirm: bool = False
    reason: str | None = None

async def execute(store, cmd: ContextCommand, *, operator: str, idem_store=None) -> CommandResult
```

- selector 正则解析为谓词（表驱动单测）；解析失败/命中 0 条 → 422（附候选近匹配用于 kind: 拼写错误）。
- 安全矩阵：P0 任何写指令 422；FORGET pinned 条目需 confirm=true 否则 422 CONFIRM_REQUIRED（返回命中条目）；FORGET/REFRESH 触碰审计数据零删改（只改 store 状态 + journal）；REFRESH 源缺失 → 200 但 affected 标 `source_missing`（不抛）。
- 全部动作 journal `reason=manual:{cmd.reason or action}`；幂等经 WP-29 IdempotencyStore（key 维度 owner+body 哈希，重放返回首次结果）。
- 会话通道：`tools/context_tools.py`（WP-33 新增，经 registry 按开关注册）：context_pin/unpin/forget/show/set_goal/budget 为 LangChain 工具，ToolBuildContext 增 `context_store` 字段注入；工具内部只做参数转换后调 `execute()`——**解释权在 LLM，执行权在执行器**（I5/I6）。forget 工具要求 selector 精确（id:/单条），多命中时返回候选列表（工具结果文本），不执行。
- 斜杠命令 `/context <verb> <selector>`：chat_agent 在进入 tool_agent 前做前缀识别，命中则直接执行器（不经 LLM），未命中按普通对话处理（spec §14.3）。

## 10. 配置（store/db.py DEFAULT_RUNTIME_CONFIG 增补）

```python
"context.enabled": True,
"context.policy_version": "cp-v1",
"context.step_window": 1,
"context.chat_recent_turns": 6,
"context.goal_overlap_floor": 0.05,
"context.goal_drift_window": 5,
"context.case_index_digest": False,   # spec §15.3，默认关
"context.intervention.enabled": True,
"context.profiles": {p.value: {"p0":..., "p1":..., "p2":...} for p in PROFILES},
```

启动期不硬校验 profile（config 先于 model 信息）；首次 assemble 时校验并缓存结果，非法 → ValidationError 一次。

---

## Wave A — WP-30：context 纯函数层

### Task 1: tokens 平移与域模型

**Files:** 新增 `context/__init__.py`、`context/tokens.py`、`context/models.py`；修改 `graph/retrieval/ops_b.py`；测试 `tests/test_context_models.py`、`tests/test_context_tokens.py`

- [ ] **Step 1（failing）**：`test_context_tokens.py`：estimate_tokens 中英混合用例（空串 0、纯中文按字、ascii 词×1.3 ceil）；`test_context_models.py`：枚举值、extra=forbid、非法 partition ValidationError、entry 不可变作用域字段（模型层允许构造但 store 不提供改口——在 Task 3 测）。
- [ ] **Step 2**：新建 tokens.py（实现搬自 ops_b，含 _CJK_CHAR/_ASCII_WORD）；ops_b 改 `from ...context.tokens import estimate_tokens`（保留 re-export，既有 import 路径全绿）。
- [ ] **Step 3**：models.py 按 §1 全量实现。
- [ ] **Step 4**：`pytest tests/test_context_models.py tests/test_context_tokens.py tests/test_retrieval*.py -W error` 全绿（回归检索套件）。

### Task 2: ProfileBudget + 校验

**Files:** `context/budget.py`；测试 `tests/test_context_budget.py`

- [ ] failing：六 profile 默认值与 spec §5 一致；负值/超窗 0.8 拒；SPILL 10% 计算表驱动。
- [ ] implement → pass。

### Task 3: ContextStore 状态机 + 幂等 + 锁

**Files:** `context/store.py`（JournalSink 用测试 fake，不接 DAO）；测试 `tests/test_context_store.py`（~12，spec §10.1）

- [ ] failing 用例：三分区 append/get；重复 entry_id 返回 False 且不覆盖；active→demoted→evicted 合法路径；evicted→pin / active→active 等非法迁移报错；pin/unpin；set_goal 旧目标 superseded；close_batch；并发 `asyncio.gather` 100 append 无丢失；状态替换不可变（append 后修改入参对象不影响 store）；journal fake 收到全部动作行；journal 抛错时操作仍成功且 degraded_journal 可补写。
- [ ] implement（per-owner asyncio.Lock；条目对象替换语义）→ pass。

### Task 4: policy 纯函数

**Files:** `context/policy.py`；测试 `tests/test_context_policy.py`（~14）

- [ ] failing：score 五子项表驱动（含 floor 边界 0.05 两侧）；kind_weight 全枚举；dup>0.85；裁剪低分先出 + (created_at,entry_id) tiebreak；pinned 不进候选；T3 候选三条件；make_tombstone 全 refs 分支（P1 含 retrieve_kb 提示）；digest 80 字截断/中文按字。
- [ ] implement → pass。

### Task 5: ExecScope 与可见性谓词

**Files:** `context/scopes.py`；测试 `tests/test_context_scoping.py` 前半（~6）

- [ ] failing：scope() 嵌套继承（batch 内推 item 自动带 batch_id）；contextvars asyncio 并发不串；CASE_ITEM profile 下：前序 item 不可见、同 item 可见、task.shared 可见、task.design 在 write 调用不可见、batch 条目跨 batch 不可见；PLAN profile 下 item 条目不可见。
- [ ] implement → pass。

### Task 6: assembler 四步管线

**Files:** `context/assembler.py`、`context/p0.py`（p0 可先用直构造文本，不接 PromptLoader 的部分在 Task 7）；测试 `tests/test_context_assembler.py`（~12）

- [ ] failing：P0 段字节稳定（两次 hash 相等）；P1 去重与 step 序；P2 pinned 全量 + K 轮 CHAT_TURN 完整 + demoted 合 tombstone 段；预算超支裁剪顺序与 report.evicted_in_assembly 归因一致；budget_override；确定性（两次全序列 sha256 相等）；CASE_ITEM 组装公式断言（无 design/前序 item）；frozen 时不裁剪。
- [ ] implement → pass。

### Task 7: P0 引导 + registry

**Files:** `context/p0.py` 完整版、`context/registry.py`；测试 `tests/test_context_p0.py`
- [ ] failing：PromptLoader fake 两模板版本 → p0_version 稳定摘要；顺序敏感（换序 version 变）；registry start/get/evict/重复 start 幂等。
- [ ] implement → pass。
- [ ] WP-30 验收：`pytest tests/test_context_*.py -W error`；新增临时脚本（不提交）验证 `grep -R "from ..runtime\|from ..graph\|from ..memory\|from ..adapters\|from ..store" tester_agent/context/` 零命中（Task 16 固化为门禁）。

## Wave B — WP-31：消费面接线

### Task 8: TaskContext/AppContext 接线位（store 可选注入）

**Files:** `runtime/context.py`、`store/models.py`（DAOs.journal）；测试 `tests/test_context_wiring.py`
- [ ] failing：构造 TaskContext 带 fake store，daos.journal 可取；默认 None 时旧路径无影响。
- [ ] implement（AppContext.context_registry 默认工厂；TaskContext.context_store=None）→ pass。

### Task 9: chat_agent 历史窗 + P0 模板

**Files:** `runtime/chat_agent.py`、新增 `prompts/methodology.md`（YAML version 头）；测试 `tests/test_context_chat.py`（~8）
- [ ] failing：>K 轮组装后旧轮以 tombstone 出现/不出现在正文；最近 6 轮完整 Human/AI 配对；开关 false 时仍走 40 条旧路径（行数与内容断言）；turn append journal；FakeLLM 收到的消息经 assemble（调用记录断言）。
- [ ] implement：owner=conversation，store 从 registry.get 或临时 start（WP-32 rebuild 落地前先 start 空 store + 从 MessageDAO 补 CHAT_TURN 的过渡逻辑，加 TODO 指向 WP-32 Task 13）。
- [ ] 既有 test_api_chat_tools 全绿。

### Task 10: tool_agent before_model_hook

**Files:** `graph/tool_agent.py`、`graph/context_hooks.py`（新薄模块）；测试 `tests/test_context_tool_agent.py`（~6）
- [ ] failing：FakeLLM 脚本制造长 ToolMessage，hook 后下次模型输入中旧 ToolMessage 变 tombstone；tool_trace 不变；max_steps/final_text 不变；hook=None 行为字节不变。
- [ ] implement → pass。

### Task 11: 批次 case_item 隔离接线

**Files:** `graph/batch.py`、`graph/nodes/case_generate.py`；测试扩充 `test_context_scoping.py`（至 ~10）
- [ ] failing：100 条批次，第 100 条组装 tokens 与第 1 条差 <10%（O(1) 断言）；第 N 条窗口无 N-1 item；close_batch 后条目 evicted + journal batch_close；repair 同 item_key 过程保留；case_index_digest 默认关（窗口无索引行），开关开时注入一行。
- [ ] implement → pass。

## Wave C — WP-32：控制环 / 持久化 / 验收

### Task 12: 005 迁移 + ContextJournalDAO

**Files:** `store/migrations/005_context_journal.sql`、`store/db.py`（config 键）、`store/models.py`；测试 `tests/test_migration_005_context.py`（~4）
- [ ] failing：全新库建表/索引；重复执行幂等；非法迁移命名仍被拒；created_at 插入后不被更新。
- [ ] implement：DAO put_batch（INSERT OR IGNORE）/list_by_owner（key-set 分页无重复无遗漏）/list_actions 升序；workspace 隔离（异 ws 查不到）。
- [ ] config 键默认值单测。

### Task 13: rebuild

**Files:** `context/journal.py`（JournalRow + DAO 适配 sink）、`context/rebuild.py`；测试 `tests/test_context_rebuild.py`（~8）
- [ ] failing：artifact 链→PLAN pinned/旧版 superseded；message→CHAT_TURN 与 turn_seq；journal 重放 demote/evict/pin 裁决；缺源 source_missing 不抛；两次 rebuild snapshot_state 相等；item 过程不复活；替换 Task 9 过渡逻辑为 registry.restore（rebuild 真路径）后 chat 测试仍绿。
- [ ] implement → pass。

### Task 14: 控制环 T1 钩子 + wrap 作用域

**Files:** `graph/wrap.py`、`graph/control/nodes.py`、`api/tasks.py`（confirm pin 一小钩）；测试 `tests/test_context_control_graph.py`（~10）
- [ ] failing：wrap phase 映射表（design/write）；step 结束 P1 bulk_demote(step_window)；replan 旧 PLAN superseded；repair 旧 reflection demote；confirm 后 artifact pinned；大纲 confirm 产生 SHARED OUTLINE_DIGEST 且编写窗口可见、设计草稿不可见；中断恢复（模拟重建 store 注入）组装等价；reflection_log 硬编码移除后既有 control 测试回归全绿。
- [ ] implement → pass。

### Task 15: 调试 API + playground + SSE

**Files:** `api/context.py`、`main.py`、`api/events.py`（事件名登记）；测试 `tests/test_context_api.py`（~8）
- [ ] failing：两 owner GET 视图 200；跨 ws 404；evictions 分页无重复无遗漏 + partition 过滤；playground 前后业务表行数快照一致（context_journal 也零写入）；journal 故障注入 → 端点 degraded 字段不 500；SSE 事件抓到（总线测试惯例）。
- [ ] implement → pass。

### Task 16: 场景 13 + 门禁 + eval 联动

**Files:** `tests/scenario/test_scenario_13_context.py`（新）、门禁脚本/配置（import-linter 契约或 AST 扫描扩展，沿用场景 12a 既有实现位置）、eval fixtures 扩展
- [ ] 场景：FakeLLM + 预置工作区，200 轮 chat + 12-step（replan×2、repair×3、review 子任务×2、100 条用例批次）。断言 spec §10.2 六项 + 编写窗口零 design 条目 + O(1) 批次 tokens。
- [ ] 门禁：context 包反向依赖零命中（含传递依赖检查按既有 AST 扫描方式扩展）；ReMeWriter 零引用不回退。
- [ ] eval：context on/off config-diff，referenced 占比下降 ≤5%、覆盖完整率不降；指标并入 eval smoke 输出。
- [ ] 全量 `cd server && python -m pytest tests/ -W error` 全绿；基线用例数登记交接单。

## Wave D — WP-33：运行时干预

### Task 17: 执行器 + selector

**Files:** `context/intervention.py`；测试 `tests/test_context_intervention.py` 前半（~7）
- [ ] failing：selector 六语法表驱动（含 item:/batch:）；pin/unpin/forget/refresh/set_goal/budget/freeze 状态结果；P0 → 422；pinned forget 无 confirm → CONFIRM_REQUIRED 带候选；refresh 源缺失不抛；manual:* journal 行；终态 409（执行器侧给 store.closed 判定）。
- [ ] implement → pass。

### Task 18: commands API + 幂等 + SSE

**Files:** `api/context.py` 扩展；测试 `tests/test_context_intervention.py` 中段（~3）
- [ ] failing：Idempotency-Key 重放首次结果；跨 ws 404；completed 任务只读允许/写 409；SSE context_command_executed/context_policy_changed。
- [ ] implement（复用 api/common.py 与 runtime/idempotency.py）→ pass。

### Task 19: context_* 工具组 + 斜杠命令

**Files:** `tools/context_tools.py`（新）、`tools/registry.py`、`runtime/chat_agent.py`；测试 `tests/test_context_intervention.py` 尾段（~2）
- [ ] failing：FakeLLM 脚本发 context_forget 工具调用 → store 生效 + journal；多命中返回候选列表不执行；`/context pin kind:decision` 不经 LLM 直通；非斜杠普通消息不被拦截；开关 false 时工具组不注册。
- [ ] implement → pass。全量验收 `pytest tests/ -W error`，更新 handoff。

---

## 12. 错误与降级矩阵（对齐 spec §9）

| 场景 | 行为 | journal/日志 |
|---|---|---|
| profile 预算非法 | assemble 首次 ValidationError（422 经 API 边界；节点侧 AppError INTERNAL 不新增码） | — |
| journal 落库失败 | 内存生效，degraded_journal 补写 | 结构化日志 + store degraded 计数 |
| rebuild 源缺失 | 跳过 | rebuild 行 reason=source_missing |
| refresh 源缺失 | 200，affected 标 source_missing | manual 行 |
| pinned 超预算 | 放行 | budget_overrides + 不 journal evict |
| 跨 workspace | 404 不暴露存在性 | — |
| 干预 selector 命中 0 条/多条 | 422 + 候选近匹配 | 不写 journal（未执行） |
| 任务终态写指令 | 409 STATE_CONFLICT | — |
| context.enabled=false | 全部接线走旧路径 | 不 start owner、不写 journal |

## 13. 验收清单（Definition of Done）

- [ ] 005 迁移幂等；全量 `cd server && python -m pytest tests/ -W error` 全绿，新增 ~96 用例。
- [ ] 场景 13 六断言 + 批次 O(1) + 阶段隔离零泄漏全通过。
- [ ] I2 重放：rebuild 后指定 step 的组装结果与首次 sha256 一致。
- [ ] I1：场景全程 trace/snapshot/artifact/message 表内容与文件 hash 在淘汰前后不变。
- [ ] 门禁：context 层反向依赖 AST 扫描零命中；ReMeWriter 门禁不回退。
- [ ] eval config-diff 护栏达标；开关关闭回归旧行为有测试锁定。
- [ ] 交接单：ops_b 估算平移、reflection_log 废弃、chat 过渡逻辑移除、预算内存态等偏离逐条登记；handoff 更新。

## 14. 非目标（一期，沿用 spec §12）

LLM 压缩（仅程序化 digest）；跨任务/跨会话共享；多 worker 一致性；旧任务热迁移；前端调试页（γ 线另包，本方案仅提供 API）。
