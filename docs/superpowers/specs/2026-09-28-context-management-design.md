# 上下文管理层设计：三分区模型与目标相关性淘汰（Context Management Layer）

| 项 | 内容 |
|---|---|
| 日期 | 2026-09-28 |
| 状态 | 需求细化 + 测试方案（4 个开放决策待用户裁决后定稿，见 §13） |
| 对应 | [tech-design](../../tech-design.md) / [detailed-design](../../detailed-design.md) |
| 相关 | [plan-execute-reflexion](./2026-09-28-plan-execute-reflexion-design.md)（控制环为消费面之一）/ [builtin-tools](./2026-09-28-builtin-tools-design.md)（工具层经 P2 摘要受益） |
| 定位 | 新增独立叶子层 `server/tester_agent/context/`（与 prompts 同级） |

## 0. 需求原始输入与细化映射

| 原始需求（用户） | 细化落点 |
|---|---|
| 基本信息（固定提示词指令规则）单独存放 | P0 静态区：prompts 包唯一来源，run 启动冻结，永不淘汰 |
| 从知识库查询到的业务知识单独存放 | P1 知识区：retrieve_pipeline outcome 按 step 窗口挂载，到期降级 |
| 任务过程产生信息单独存放，可删除、可废弃 | P2 任务区：active → demoted（tombstone）→ evicted 状态机 |
| 与任务目标无关的信息可删除 | 确定性打分淘汰（T1 生命周期 / T2 预算 / T3 目标漂移）+ journal 归因 |
| 保证每次调用大模型时上下文完整、精准 | 组装协议：钉住保完整、预算+淘汰保精准；场景 13 断言 |
| 作为架构中单独的一层 | `context/` 叶子层包 + import-linter 契约（§10.4） |

### 0.1 现状问题盘点（为什么要单独一层）

| 消费面 | 现状 | 问题 |
|---|---|---|
| 会话 chat（runtime/chat_agent.py） | 固定 system prompt + 「最近 40 条」全量历史 | 窗口大小与相关性无关（40 硬编码）；淘汰无归因 |
| 工具轮（graph/tool_agent.py） | max_steps 内消息无界累积，ToolMessage 全文驻留 | 长工具输出（如 bash 输出）永久占用后续每轮调用 |
| 控制环（graph/control/nodes.py） | artifacts payload 全额驻留 LangGraph state/checkpoint；reflection_log[-20:] 硬编码 | 产物随 step 线性累积；上一 step 的知识块后续仍全额驻留 |
| 能力节点（graph/nodes/* + retrieve_pipeline） | 单次调用内有 token_budget/inject_limit | 只管"这一次调用"，跨 step 的知识块无生命周期 |

可复用资产（不重复造轮子）：

| 资产 | 复用方式 |
|---|---|
| retrieve_pipeline 的 token 估算 / 预算裁剪 / 锚点排序（ops_b.assemble） | P1 组装语义对齐；estimator 上移共享（§11 WP-30） |
| 引用闭环 referenced/hallucinated（pipeline.close_loop） | 打分因子"被下游引用"的直接数据源 |
| artifact payload_ref 外置模式 | P2 降级（demote）复用：state 内 payload → digest + ref |
| snapshot 三档 + 偏移读 | P1 全文不落 DB 的既定口径延续；journal 同款"元数据入 DB" |
| 检索漏斗 drop_reason 恒留痕哲学 | 淘汰 journal（I4）同款哲学 |
| import-linter + AST 门禁（场景 12a） | 扩展为 context 层隔离契约 |

## 1. 核心理念与设计红线（不变量）

核心理念：**与任务目标无关的信息可删除**。但"删除"只作用于**未来组装窗口**，永不触碰审计数据。

| # | 不变量 | 说明 |
|---|---|---|
| I1 | 审计不灭 | DB（trace/snapshot/artifact/message/context_journal）与文件全文永远完整；淘汰仅影响"下一次组装读什么" |
| I2 | 可重放 | 同一 store 状态 + 同一 profile + 同一 policy_version → 字节级相同的组装结果 |
| I3 | 钉住优先 + tombstone | pinned 条目永不淘汰；任何淘汰必留 tombstone（摘要行 + 溯源指针） |
| I4 | 淘汰必归因 | 每次 append/demote/evict/pin 写 journal（对齐"检索漏斗恒留痕"硬约束） |
| I5 | 确定性淘汰 | 一期淘汰环内**零 LLM 调用**（规则打分），策略为纯函数可单测 |
| I6 | 层次隔离 | context 为叶子层：禁止 import runtime/graph/memory/adapters；graph/runtime/store 零 ReMeWriter 门禁不回退 |

## 2. 决策摘要

| 决策点 | 选择 |
|---|---|
| 分区模型 | P0 静态区 / P1 知识区 / P2 任务区，独立存放、独立预算 |
| 淘汰机制 | 确定性规则打分（recency / 引用 / goal 重叠 / 版本更替）+ 预算驱动裁剪 |
| LLM 压缩 | 一期不做，程序化 tombstone 摘要；二期留 policy hook（D1） |
| 淘汰持久化 | 新表 `context_journal`（005 迁移，仅元数据，恒落库，D2） |
| 与 snapshot 关系 | 互不影响；full 档可选扩展记录每次组装结果（D4） |
| 实现位置 | `server/tester_agent/context/`（store / policy / assembler / budget 四模块） |

## 3. 三分区模型

### 3.1 P0 静态区（基本信息）

| 维度 | 约定 |
|---|---|
| 内容 | 系统提示词、方法论规则（等价类/边界值/判定表/场景法）、输出格式契约、工具使用说明、风格与安全约束 |
| 来源 | prompts 包（PromptLoader，带 version）+ agent.config；**run 启动时冻结**（对齐 Runner"配置冻结"既有语义） |
| 生命周期 | 与 run/会话等长；永不淘汰、永不修改 |
| 组装位置 | 消息序列最前（稳定前缀，prompt cache 友好；可测试断言：同 run 两次组装 P0 段字节 hash 相同） |
| 版本戳 | 消费的全部模板 version 汇总为 `p0_version`，随 trace/journal 留痕（eval 对版本敏感的既有口径延续） |

### 3.2 P1 知识区（KB 业务知识）

| 维度 | 约定 |
|---|---|
| 内容 | retrieve_pipeline 注入块（entry_id / title / passage / anchor 序）+ 引用白名单 |
| 来源 | 各 step/批次的管线 outcome；管线自身预算（inject_limit/token_budget）仍是**单次调用内**的第一道闸，context 层只管跨调用生命周期 |
| 活跃窗口 | 挂载 step + 后续 `context.step_window`（默认 1）个 step；窗口到期 → demote |
| 淘汰 | 预算驱动裁剪 + 窗口到期；**知识仍在 KB，tombstone 附 retrieve_kb 重取提示** |
| 计量 | tokens_est 与管线 token_est 同源（共用 estimator，不二次估算） |

### 3.3 P2 任务区（任务过程信息，可删除可废弃）

| 维度 | 约定 |
|---|---|
| 内容 | 对话轮次、plan/steps、artifact 摘要与 payload、reflection 条目、tool trace 与工具输出、子任务摘要、人工决策记录 |
| 生命周期 | 三态：`active` → `demoted`（tombstone：digest + 溯源指针）→ `evicted`（窗口不再出现）；一切可从 DB 重建 |
| 淘汰 | T1 显式事件（版本更替/step 推进）+ T2 预算 + T3 目标漂移（§6） |
| 钉住（不可淘汰） | active AgentPlan 及其 pending/running step 的 input_refs 摘要；人工确认产物（confirmed_by=user）最新版本；当前 step 输入；待澄清 / waiting_confirm 相关条目；chat 最近 K 轮 |

### 3.4 与既有机制的边界（防止双重管理）

| 既有机制 | 关系 |
|---|---|
| RetrievalCache（run 分区 LRU） | 不变：cache 管"召回复用"，context 管"组装窗口"，解耦 |
| trace / snapshot / 漏斗计数 | 不变：淘汰不回写、不删审计（I1）；tombstone 引用 trace_id 溯源 |
| reflection_log[-20:] | 被 P2 策略接管后移除硬编码（行为对齐验证后删除） |
| artifact payload_ref | P2 demote 复用该模式；控制环 state 内大 payload → digest + ref |
| snapshot_level 三档 | 口径延续：P1 passage 全文不进 DB；journal 只存元数据 |

## 4. 架构与层次隔离

```
消费面（LLM 调用点）                  上下文管理层（新叶子层）
┌────────────────────────┐ assemble(profile) ┌────────────────────────┐
│ chat_agent（会话轮）     │ ────────────────▶ │ context/assembler.py   │
│ tool_agent（工具轮内）   │ ◀──────────────── │   ↑ 读取                │
│ control 环 plan/execute │  消息序列+         │ context/store.py       │
│ 能力节点（P1 挂载/降级） │  AssemblyReport   │ context/policy.py      │
└────────────────────────┘ ──挂载/降级/钉住──▶ │ context/budget.py      │
                                              └───────────┬────────────┘
                                                          │ journal（append-only）
                                                          ▼
                                              store/db.py（005 迁移）＋ workspace files（D4 full 档）
```

依赖方向（import-linter 契约）：

- 允许：`context → domain / prompts / utils`；`runtime / graph / tools → context`（消费面）
- 禁止：`context → runtime / graph / memory / adapters`（reader、LLM 等一律由调用方传参注入）

## 5. 组装协议（assemble profiles）

每个 LLM 调用点绑定一个 profile，声明三分区预算与选择规则：

| profile | 消费点 | P0/P1/P2 预算（tokens est） |
|---|---|---|
| chat | chat_agent.run_chat_turn | 1500 / 3000 / 8000（最近 K=6 轮完整） |
| plan | plan / replan 节点 | 3000 / 4000 / 6000 |
| execute | execute_step / capability | 3000 / 8000 / 6000 |
| reflect | reflect 节点 | 2000 / 2000 / 4000 |
| review | 评审子任务 | 2500 / 8000 / 8000 |

组装顺序（每次调用）：

1. **P0**：静态区全文 → SystemMessage 前缀（run 内字节稳定，I2/cache 友好）
2. **P1**：活跃知识块 entry_id 去重 → anchor-v1 排序 → 超预算按 score 裁剪（语义对齐 ops_b.assemble）
3. **P2**：pinned 全量 → 活跃条目按窗口与相关性 → demoted 条目降为 tombstone 行 → 预算裁剪
4. **产出**：消息序列 + `AssemblyReport{per_partition_tokens, evicted[], policy_version, p0_version, budget_override[]}`

tombstone 行格式（保证"完整"感知）：

```text
〔已归档 {entry_kind} #{entry_id}：{一行要点摘要}（全文：{payload_ref|trace_id|snapshot_id}；KB 知识可用 retrieve_kb 重取）〕
```

预算裁剪算法：总预算 = Σ 分区预算；单分区允许 10% 侵占余量；裁剪次序 = score 升序、同分按 `(created_at, entry_id)` 字典序——确定性 tiebreak（对齐冻结分页协议精神）；pinned 超预算时放行并记 `budget_override`（I3 优先于预算）。

## 6. 淘汰策略（policy，纯函数）

打分公式：

```text
score(e) = 0.35·recency + 0.30·referenced + 0.20·goal_overlap + 0.15·kind_weight − dup_penalty
```

| 因子 | 取值 |
|---|---|
| recency | 1/(1+step_distance)（chat 场景为轮距） |
| referenced | 条目被下游 active artifact 引用（引用闭环 referenced 集）→ 1，否则 0 |
| goal_overlap | 条目文本 × 当前 goal/step.goal 的字符 bigram Jaccard，< `goal_overlap_floor` 记 0（复用管线 bigram 实现） |
| kind_weight | plan/decision 0.9、artifact 0.7、tool_result 0.5、chat_old 0.3 |
| dup_penalty | 与窗口内已有条目 bigram Jaccard > 0.85 → 0.5 |

触发器：

| 触发器 | 时机 | 动作 |
|---|---|---|
| T1 生命周期事件 | step 边界（同步） | step 完成 → 该 step 的 P1 全部 demote；artifact 新版本 → 旧版本 demote；repair 完成 → 对应旧 reflection 条目归档 |
| T2 预算驱动 | assemble 时 | 超预算 → 低分先出（含跨分区余量侵占） |
| T3 目标漂移 | assemble 时 | 非 pinned 且 goal_overlap=0 且 step_distance > W → 进淘汰候选 |
| 条目边界 | 每条用例生成完成 | item scope 条目停止纳入后续窗口（组装过滤，非淘汰，无 tombstone）；batch 结束统一 evict（§15） |

安全规则：pinned 永不出（I3）；当前 step 输入与最近 K 轮不出（除非预算极端且已无更低分候选，此时记 budget_override）；每次淘汰写 journal（I4）；tombstone 必含溯源/重取指针。

## 7. 持久化与恢复

新迁移 `005_context_journal.sql`（只增不改协议）：

```sql
CREATE TABLE IF NOT EXISTS context_journal (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  scope TEXT NOT NULL,               -- 'task' | 'conversation'
  conversation_id TEXT,
  partition TEXT NOT NULL,           -- 'P0' | 'P1' | 'P2'
  entry_id TEXT NOT NULL,
  entry_kind TEXT NOT NULL,
  action TEXT NOT NULL,              -- 'append'|'demote'|'evict'|'pin'|'unpin'|'rebuild'
  reason TEXT NOT NULL,              -- 'step_window'|'budget_cut'|'superseded'|'goal_drift'|'source_missing'|...
  policy_version TEXT NOT NULL,
  tokens_est INTEGER NOT NULL DEFAULT 0,
  refs TEXT NOT NULL DEFAULT '{}',   -- JSON: {payload_ref, trace_id, snapshot_id}
  created_at TEXT NOT NULL           -- 仅插入时写入，永不更新
);
CREATE INDEX IF NOT EXISTS idx_context_journal_task ON context_journal(task_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_context_journal_ws ON context_journal(workspace_id, created_at, id);
```

写入规则（对齐项目硬约束）：INSERT OR IGNORE 幂等；workspace 强制过滤；`(created_at, id)` 降序 key-set 分页；WAL 下与业务读写并存。

重建协议（进程重启 / 任务重入）：从 artifact（最新版本）+ message + trace（injected_ids）重建三分区 → journal 追加 `action='rebuild'` 行；重复重建结果一致（幂等，对齐 Reconciler 消解哲学）；淘汰历史由 journal 重放恢复（demote/evict 状态不丢）。

## 8. API 与配置

| 端点 | 说明 |
|---|---|
| `GET /tasks/{id}/context` | 三分区视图（条目元数据 + tokens_est；P1 passage 全文走 snapshot 偏移读，不在此接口） |
| `GET /tasks/{id}/context/evictions` | journal 分页（key-set，(created_at,id) desc，对齐 WP-28 调试端点风格） |
| `GET /conversations/{id}/context` | 会话侧视图 |
| `POST /workspaces/{id}/context/playground` | 组装预览（persist=False，零业务表写入——对齐 dd §8.7 口径） |
| `POST /tasks/{id}/context/commands`（及 `/conversations/{id}/...`） | 运行时干预指令（§14；Idempotency-Key 幂等，复用 WP-29 IdempotencyStore） |

配置（runtime_config，agent.config 可覆盖）：

| 配置键 | 默认 | 说明 |
|---|---|---|
| context.enabled | true | 关闭时组装退化为现状行为（40 条历史 / 全额注入），保证可回退 |
| context.profiles.* | §5 表 | 各 profile 三分区预算 |
| context.chat_recent_turns | 6 | chat P2 完整保留轮数 |
| context.step_window | 1 | P1 知识块活跃后续 step 数 |
| context.goal_overlap_floor | 0.05 | bigram Jaccard 下限 |
| context.policy_version | "cp-v1" | 策略版本戳（journal/trace 记录） |
| context.intervention.enabled | true | 运行时干预通道总开关（API + 会话工具组，§14） |

## 9. 错误处理

| 场景 | 行为 |
|---|---|
| 预算/配置非法（负值、总量超模型窗口） | ValidationError（启动即拒，对齐 snapshot_level 校验风格） |
| journal 落库失败 | 内存态继续生效；degraded 事件 + 结构化日志；**不阻断 LLM 调用** |
| 重建时源数据缺失 | 跳过该条 + journal reason='source_missing'；不抛 |
| 跨 workspace 访问 | 404 不暴露存在性（对齐 API-A 口径） |
| pinned 超预算 | 放行 + budget_override 记录（I3 > 预算） |

## 10. 测试方案

约定：全量 `python -m pytest tests/ -W error` 全绿为硬验收；基线数字以开工日空跑为准并登记交接单；FakeLLM 脚本驱动（tests/fakes.py 惯例）；新增约 **80** 用例。

### 10.1 单测矩阵

| 测试文件（新增） | 对象 | 关键用例 |
|---|---|---|
| test_context_store.py (~12) | ContextStore | 三分区 append/get；entry_id 幂等去重；pin/unpin；active→demoted→evicted 状态机（非法迁移报错）；workspace 隔离；并发 append（asyncio.gather 无丢失）；按分区列表排序稳定 |
| test_context_policy.py (~14) | policy 纯函数 | 打分表驱动（recency/引用/goal 重叠/版本更替/重复惩罚）；pinned 永不进候选；裁剪次序=低分先出、同分 (created_at, entry_id) 确定性 tiebreak；budget_override 记录；overlap 阈值边界（floor 上下）；superseded 检测 |
| test_context_assembler.py (~12) | assembler | profile 预算分配；**P0 段字节稳定**（同 run 两次组装 P0 hash 相同）；P1 去重 + anchor 序；P2 窗口（K 轮完整 + tombstone 行）；**确定性**（同状态两次组装逐条相等）；tombstone 含溯源指针；policy_version 透传 |
| test_context_chat.py (~8) | chat 接线 | 历史窗收敛（>K 轮淘汰留 tombstone）；最近 K 轮完整；system prompt 冻结（P0）；既有 chat 回归（test_api_chat_tools 全绿）；淘汰后下一轮不含被淘汰内容 |
| test_context_tool_agent.py (~6) | 工具轮内 | 长 ToolMessage 在下轮模型调用前被摘要替换；tool_trace 不受影响；max_steps/recursion_limit 行为不变；final_text 不变 |
| test_context_control_graph.py (~10) | 控制环 | step 完成→上一步 P1 降级；superseded artifact 降级；confirmed artifact pinned；reflection_log 硬编码[-20:]被策略接管；中断恢复后 store 重建等价（同组装结果）；rebuild journal 幂等 |
| test_context_api.py (~8) | 四端点 | 200/404（跨 workspace 不暴露存在性）；evictions 分页无重复无遗漏；playground 零业务表写入（对齐 §8.7 断言方式）；journal 失败降级 degraded |
| test_migration_005_context.py (~4) | 005 迁移 | 全新库成功；重复执行幂等；索引存在；created_at 仅插入不更新 |
| test_context_intervention.py (~12) | 干预通道（§14） | selector 解析表驱动；执行器状态迁移；P0 拒绝 422；pinned forget 需 confirm；幂等键重放；journal reason=manual:*；跨 workspace 404；会话工具循环（FakeLLM 脚本发 context_forget → store 生效）；SSE 事件；模糊指代返回候选列表 |
| test_context_scoping.py (~10) | 条目级隔离（§15） | 组装第 N 条用例窗口无第 N-1 条 item 条目；scope 过滤先于淘汰与 tombstone；repair 同 item 保留；batch 结束统一 evict + journal；rebuild 幂等（已完成 item 不复活）；case_index_digest 开关默认关；100 条批次第 100 条组装 tokens ≈ 第 1 条（O(1) 断言）；**阶段隔离（§15.5）**：编写窗口无 design 过程条目、shared 白名单可见、大纲修订后 digest 跟随新版本 |

### 10.2 场景 13：长任务上下文收敛与可重放（对应 WP-32 验收）

构造：FakeLLM + 预置工作区；模拟 200 轮 chat + 12-step plan-execute 任务（含 replan 2 次、repair 3 次、评审子任务 2 个）。

| # | 断言 |
|---|---|
| 1 | token 有界：任意一次 LLM 调用组装 tokens ≤ profile 总预算 + pinned 容差 |
| 2 | 收敛性：随 step 推进 P2 活跃条目数不单调增长（有淘汰发生且 journal 可归因） |
| 3 | 钉住安全：确认过的 artifact 与 active plan 始终在窗口内 |
| 4 | 重放等价：按 journal + 审计数据重建，第 N 次调用的组装结果与首次运行字节一致（I2） |
| 5 | 审计完整：淘汰前后 trace/snapshot/artifact/message 行数与内容不变（I1） |
| 6 | 淘汰归因：每条淘汰有 reason 且可从 evictions 端点查询 |

### 10.3 质量护栏（eval 联动，对齐"离线评测管线"硬约束）

- eval fixtures 增加触发淘汰的长需求样本；对比 context.enabled on/off（config-diff）：
  - 引用闭环不劣化：hallucinated 不升；referenced 占比下降 ≤ 5%
  - 覆盖矩阵完整率不下降（淘汰不得导致下游丢维度）
- 指标随 eval smoke 5 项指标表输出（WP-13 既有管线扩展）。

### 10.4 门禁

- import-linter 新契约：context 层不得 import runtime/graph/memory/adapters（含传递依赖）
- 场景 12a AST 扫描扩展：context 包对 ReMeWriter 零引用
- 验收命令：`cd server && python -m pytest tests/ -W error`（全绿）+ `lint-imports`（若已配置）

## 11. WP 拆分建议（挂 α 线尾，编号接续 WP-29）

| WP | 内容 | 体量 | 验收 |
|---|---|---|---|
| WP-30 | context 纯函数层：store/policy/assembler/budget + scope 作用域过滤（§15）+ tokens 估算上移共享（ops_b 一次性小重构，登记交接单） | M | 10.1 前三个文件全绿；确定性/预算断言 |
| WP-31 | 消费面接线：chat_agent 历史窗 + tool_agent 轮内摘要 + case_item profile（条目级隔离） | M | test_context_chat/tool_agent；既有 chat 回归全绿 |
| WP-32 | 控制环接线 + 005 迁移 + journal + 调试 API + 场景 13 + eval 联动 + 门禁扩展 | M | 场景 13 全绿；import-linter 通过；eval 指标不劣化 |
| WP-33 | 运行时干预：intervention 执行器 + 指令 API + context_* 工具组 + SSE 事件 | S | test_context_intervention 全绿；幂等/权限/P0 拒绝断言 |

依赖：WP-30 无依赖可立即开工；WP-31/32 依赖 30。均遵守"契约冻结、测试即记忆、收尾写交接单"协议。

## 12. 非目标（一期）

- LLM 参与压缩/总结（仅程序化 tombstone；二期 policy hook）
- 跨任务/跨会话上下文共享与迁移
- 多 worker 分布式一致性（维持单机单用户单 worker）
- 旧任务热迁移（对齐范式切换约定）
- 前端上下文调试页（可挂 γ 线后续包）

## 13. 开放决策（needs-design，待用户裁决）

| # | 决策 | 本方案建议 | 影响 |
|---|---|---|---|
| D1 | P2 一期采用程序化摘要（截断+要点行），不做 LLM 压缩 | 是（确定性可测，I5） | 摘要质量上限；二期可换 |
| D2 | journal 恒落库（轻量元数据，对齐"检索漏斗恒留痕"） | 是 | 005 迁移 + 轻量写放大 |
| D3 | 各 profile 预算与 chat_recent_turns=6 默认值 | 按 §5 表 | 可先按建议值上线，eval 数据回灌后调 |
| D4 | snapshot full 档扩展记录每次组装结果（JSONL 增量） | 仅 full 档启用，默认 meta 不落 | full 档文件量增长；调试/重放能力最强 |

## 14. 增补（v0.2）：运行时干预通道

需求：运行中可通过指令直接干预上下文（钉住/遗忘/重激活/改目标/调预算）。

### 14.1 双通道、单执行器

| 通道 | 入口 | 解析方式 |
|---|---|---|
| API 指令 | `POST /tasks/{id}/context/commands`、`POST /conversations/{id}/context/commands` | 结构化 ContextCommand（前端调试页/脚本用） |
| 会话指令 | 对话中的自然语言 / 斜杠命令 | 注册为 `context_*` 工具组，走既有 tool_agent 循环——**解释权在 LLM，执行权在确定性执行器**（不破坏 I5） |

两通道汇入唯一执行器 `context/intervention.py: execute(store, cmd)`；全部动作记 journal（`action='manual:{sub}'`，operator=user/api），支持 Idempotency-Key（复用 WP-29）。

### 14.2 指令集

| 指令 | 语义 | 约束 |
|---|---|---|
| pin / unpin {selector} | 钉住/解钉（进/出淘汰豁免） | P0 拒绝 422 |
| forget {selector, confirm?} | 立即降级或淘汰 | 人工确认产物默认 pinned，需 confirm=true 二次确认；审计数据不动（I1） |
| refresh {selector} | demoted/evicted 重新激活，content 从审计数据回填（KB 块走 retrieve_kb 重取） | 源缺失 → source_missing 不抛 |
| set_goal {text} | 更新任务目标（影响 T3 打分） | journal + goal 变更事件 |
| budget {profile} {p0,p1,p2} | 运行时调预算 | 不得超模型窗口（校验同 §9） |
| show [selector] | 查看窗口内容 | 只读 |
| recall {query} | 触发一次重检索并挂载 P1 | 走 retrieve_pipeline 既有语义 |
| freeze / unfreeze | 冻结/解冻当前组装窗口（调试重放） | 冻结期间组装结果恒定并记告警 |

selector 确定性语法：`id:{entry_id}` | `kind:{entry_kind}` | `step:{step_id}` | `recent:{n}` | `all`——正则解析；一期不做自然语言 selector。

```python
class ContextCommand(BaseModel):
    action: ContextAction      # StrEnum: pin/unpin/forget/refresh/set_goal/budget/show/recall/freeze
    selector: str
    arg: str | None = None
    confirm: bool = False
    reason: str | None = None  # journal reason=manual:{reason}
```

### 14.3 安全规则

- I1 红线：任何干预不删改 message/artifact/trace/snapshot
- P0 不可干预；pinned 条目的 forget 需 confirm=true
- 并发：store 操作在 asyncio 锁内原子执行；任务终态（failed/cancelled）拒绝 409
- 权限：workspace 归属校验，404 不暴露存在性
- 幂等：Idempotency-Key 重放返回首次结果
- 会话通道误伤防护：未匹配斜杠命令的消息按普通对话处理；LLM 发起 forget 时 selector 必须精确命中，模糊指代时工具返回候选列表要求二次选择
- SSE：`context_command_executed {action, selector, affected[]}` 事件，前端卡片反馈

### 14.4 会话指令示例

```text
用户：把"只覆盖支付模块"设为当前目标
  → LLM 调 set_goal(text="只覆盖支付模块") → store.goal 更新，journal: manual:set_goal
用户：忘掉最近 3 轮里关于登录的讨论
  → LLM 先 show recent:3 拿候选 → 返回 selector 列表 → 用户选定 → forget id:chat_turn_xxx
用户：/context pin kind:decision
  → 斜杠命令直通执行器（不经 LLM）
```

## 15. 增补（v0.3）：条目级作用域隔离（批次用例生成）

需求：逐条生成用例时，每条的上下文不包含之前条目产生的过程上下文——批次重复任务的过程信息对单条用例设计是噪声。

### 15.1 三级作用域

ContextEntry 增加 `scope` 字段（P2 专属维度；P0/P1 恒为 task）：

| scope | scope_key | 内容 | 窗口可见性 |
|---|---|---|---|
| task | — | 任务目标、active plan、确认产物摘要、人工决策 | 所有调用 |
| batch | batch_id | 批次公共信息：当前测试点全文、批次级知识块（P1 挂载即 batch scope） | 同批次所有条目 |
| item | `{batch_id}:{point_id/序号}` | 单条用例生成过程：工具调用、草稿、条目级反思 | **仅该条目自己的调用** |

每条用例组装公式：`P0 + P1(当前点, batch) + P2(task) + P2(item=当前)`。

### 15.2 隔离机制：组装过滤，而非事后淘汰

- assembler 对 case_item profile 先施加 scope 过滤器（task ∪ 当前 batch ∪ 当前 item），**前序 item 条目从未进入后续窗口**——因此无 tombstone（I3 不破：tombstone 服务于同窗口完整性感知，跨条目不需要）
- 条目随 batch 结束统一 evict（journal reason='batch_closed'）；item 完成不触发写操作，只改变过滤器参数（零写放大）
- repair 重做同一 item 时 scope_key 不变，过程条目保留直至该 item done

### 15.3 与既有机制的对齐

| 机制 | 对齐 |
|---|---|
| 跨条目防重复 | 不靠上下文：数据层已有 content_hash 对拍、覆盖矩阵、uuid5 确定性 ID；可选开关 context.case_index_digest（默认关）注入一行"已生成用例标题索引"（数据层摘要，非过程上下文） |
| 评审子任务 | 评审读取 artifact/testcase 产物数据，不依赖过程上下文，隔离不影响评审 |
| checkpoint | item 过程不进 LangGraph state/checkpoint（对齐"大正文不进 checkpoint"既有原则），崩溃恢复由文件/DB 重建 |
| token 收益 | P2 窗口从 O(条数) 降为 O(1)；场景 13 增加断言（100 条批次，第 100 条组装 tokens ≈ 第 1 条） |

### 15.4 干预联动

- selector 语法扩展：`item:{key}`、`batch:{batch_id}`（§14）
- `forget batch:{id}` → 终态批次整批过程条目清理

### 15.5 阶段间隔离：测试设计 vs 用例编写（v0.4）

两部分任务——测试设计（用户故事/知识点组成测试大纲）与逐条编写用例——的上下文互相独立。scope 模型扩展 phase 可见性：

| scope | phase 可见性 | 内容 |
|---|---|---|
| task.shared | 两阶段可见 | 任务目标、澄清结论、人工决策、**确认大纲摘要条目**（digest + payload_ref，非全文） |
| task.design | 仅设计阶段 | 大纲草稿、覆盖分析、设计期工具调用与反思 |
| task.write | 仅编写阶段 | 编写期任务级过程 |
| batch / item | 仅编写阶段 | 原 §15.1 语义 |

组装公式：

- 测试设计调用：`P0(design profile) + P1(设计期检索, phase=design) + P2(task.shared ∪ task.design)`
- 用例编写调用：`P0(case_item) + P1(编写期检索, phase=write) + P2(task.shared) + P2(batch) + P2(item=当前)`——**task.design 与 task.write 过程互不可见**

规则：

1. 跨阶段唯一合法通道是 **artifact 数据**：大纲 confirm 后以"摘要条目（digest + 版本 + payload_ref）"进入 task.shared，编写阶段引用数据而非重放设计过程
2. P1 知识块挂载时带 phase 标记，设计期检索块（服务于大纲）不进编写窗口，反之亦然
3. 大纲修订/回退（WP-24）→ 新版本 digest 条目 append + 旧 digest superseded；编写阶段 window 自动跟随最新确认版本，重建（rebuild）同样只从产物数据恢复，不恢复跨阶段过程
4. 例外即 shared 白名单：设计阶段的**人工确认/澄清结论**必须进编写窗口（否则用例违背已确认约束），此类条目显式标记 phase=shared，其余默认 phase=private

### 15.6 运行时强制机制：四道防线（v0.5）

隔离不靠约定，由四道机制强制保证：

| 防线 | 机制 | 说明 |
|---|---|---|
| 1 写入定性 | wrap() 注入 scope 标签 | 节点执行时由 wrap()（WP-15 既有机制）注入当前 phase/step/batch/item 标签；store.append 未显式指定时自动继承——条目产生时即定性，不存在全局可变标签，跨阶段串标代码写不出来 |
| 2 组装过滤 | assemble 唯一入口 | 所有 LLM 调用的消息序列必须经 assembler.assemble(profile)；case_item profile 的 scope 过滤器（§15.2）物理排除 design 条目——数据在 store 里存在，但进不了窗口 |
| 3 状态不带过程 | state 只传引用 | LangGraph state/checkpoint 只含 agent_plan 与 artifact 引用（output_ref→input_refs 数据流）；过程上下文仅存于 store，不进 state，中断恢复/重放也不会把设计期过程带回编写窗口 |
| 4 验证闭环 | 测试 + 门禁 | test_context_scoping 断言编写窗口零 design 条目；场景 13 集成断言；FakeLLM 断言所有消费点收到的消息均出自 assembler（防绕过直拼 messages）；import-linter 门禁（§10.4） |

跨阶段唯一合法数据流：设计 step 的 output_ref（大纲 artifact）→ 人确认 → shared digest 条目 → 编写 step 的 input_refs / 数据层按需读取。除此之外无任何通道。

## 16. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-09-28 | 初稿：需求细化 + 测试方案（待 D1~D4 裁决） |
| v0.2 | 2026-09-28 | 增补 §14 运行时干预通道：双通道单执行器 + 指令集 + 安全规则 + WP-33 |
| v0.3 | 2026-09-28 | 增补 §15 条目级作用域隔离：task/batch/item 三级 scope + 组装过滤 + 测试/WP 联动 |
| v0.4 | 2026-09-28 | §15.5 阶段间隔离：task.shared/design/write 可见性 + 产物数据唯一跨阶段通道 + shared 白名单 |
| v0.5 | 2026-09-28 | §15.6 运行时强制机制：写入定性/组装唯一入口/state 只传引用/验证闭环 四道防线 |
