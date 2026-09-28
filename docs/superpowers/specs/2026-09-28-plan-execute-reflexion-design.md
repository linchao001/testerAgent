# 用例智能体运行范式：动态 Plan-Execute + Reflexion + 工具/子任务

| 项 | 内容 |
|---|---|
| 日期 | 2026-09-28 |
| 状态 | 设计已定稿（待实现计划） |
| 对应 | [PRD](../../PRD.md) / [tech-design](../../tech-design.md) / [detailed-design](../../detailed-design.md) |
| 相关 | [builtin-tools](./2026-09-28-builtin-tools-design.md)（工具运行时保留并扩展） |
| 取代 | 固定五阶段主图拓扑（`intake → link → CP1 → point → CP2 → case → coverage`）作为编排主干 |

## 0. 决策摘要

| 决策点 | 选择 |
|---|---|
| 主图形态 | **整图替换**为自由 Plan-Execute；阶段/检查点不再硬编码为 StateGraph 边 |
| 人机确认 | **可配置**；一期默认开启链路确认 + 测试点确认 + 评审确认（`human_gate_*=true`） |
| 评审 | **全覆盖子任务**：覆盖 / 质量 / 采纳建议 → 人确认后落账 |
| 子任务 | 同任务内嵌子图；独立 `thread_id` 后缀；**串行**；一期**禁止嵌套派发** |
| 契约 | **演进**：引入 `AgentPlan` / `SubtaskResult` / `ReviewProposal` 等新主实体 |
| 前端 | **对话为主**的会话交互；独立确认页/工作台降为侧栏或深链 |
| 实现路径 | **控制面主图 + 能力工具化**（非纯 ReAct、非双 Agent 总线） |

## 1. 目标与选型依据

### 1.1 目标

将用例智能体运行范式重构为：

> **动态 Plan-Execute 为主干 + 多轮 Reflexion 为增强层**，同时支持工具调用与子任务；评审通过子任务完成。

### 1.2 任务本质（选型依据）

1. **全局覆盖优先**：须先有完整覆盖维度规划，再逐个生成；走一步看一步必漏点。
2. **强方法论约束**：等价类 / 边界 / 判定表 / 场景法等，非开放式自由发挥。
3. **多阶段递进**：需求解析 → 测试点 → 用例 → 覆盖校验 → 有效性校验 → 优化补全，边界清晰。
4. **强反馈校验**：首轮产物必有遗漏/冗余/逻辑问题，须至少一轮自查修正。

### 1.3 原则

- Plan-Execute 负责全局覆盖与步骤边界；Reflexion 负责步后校验与修订，不替代规划。
- 人机门禁由配置开关控制，开启时不可被 Planner 静默跳过（Reflect 强制 replan）。
- 工具与子任务共享沙箱与 Runner 约束（单机单用户、单进程单 worker）。
- 大正文不进 LangGraph checkpoint（需求/知识文件化；Plan 与产物元数据进 state）。
- ReMe 读写分离不变；工具层不可达 `ReMeWriter`。
- 一期不做旧任务热迁移；旧 run 只读或需重建。

## 2. 架构总览

```
SessionPage (对话为主)
        │
        ▼
API: conversations + tasks (confirm/plan/subtasks) + SSE
        │
        ▼
┌─────────────────────────────────────────────────────┐
│  Control Graph (替换原 main_graph 拓扑)              │
│  plan → dispatch → execute_step ↔ tools/subtask     │
│       → (await_human?) → reflect → replan|next|end  │
└─────────────────────────────────────────────────────┘
        │                         │
        ▼                         ▼
  Capability tools          Subtask graphs
  (旧节点函数能力化)         (review_* 等；串行)
        │                         │
        ▼                         ▼
  artifact / testcase DB + workspace files
```

旧 `intake` / `link_identify` / `point_write` / `case_generate` / `coverage_check` **节点函数降为能力实现**，由 execute / 工具调用；不再是主图拓扑节点。

## 3. 运行时控制环与状态模型

### 3.1 控制环

```text
START → plan → dispatch_next_step
              ├─ no pending & reflect ok → END
              └─ next step → execute_step ↔ (tools | spawn_subtask)
                            → human_gate? → await_human (interrupt)
                            → reflect → pass → dispatch
                                      → repair → execute_step
                                      → replan → plan
```

| 节点 | 职责 |
|---|---|
| `plan` | 按需求 + 方法论模板生成/修订 `AgentPlan`（覆盖维度先于用例细节） |
| `execute_step` | 执行当前 `pending` step；可调工具或 `spawn_subtask` |
| `await_human` | step 标记 `requires_confirm` 且对应 gate 开启时 `interrupt` |
| `reflect` | 校验刚完成 step；输出 `pass \| repair \| replan` |

上限（`runtime_config`）：

- `reflect_max_per_step` 默认 2
- `replan_max` 默认 3
- 子任务串行；不可再派子任务

### 3.2 TaskState（新）

| 字段 | 含义 |
|---|---|
| `agent_plan` | 当前 `AgentPlan`（versioned） |
| `plan_cursor` | 当前 `step_id` |
| `artifacts` | `{artifact_id → kind, version, uri/payload_ref}` |
| `subtask` | 当前/最近子任务摘要 |
| `reflection_log` | 最近 N 条反思结论（无大正文） |
| `clarification_questions` | 保留现有澄清协议 |
| `human_gates` | `{link, point, review}` 自 config 快照 |

保留：`task_id` / `graph_run_id` / `workspace_id`。移除对 `link_plan`/`point_plan`/`case_batch`/`coverage` 作为编排主键的依赖（可经 `artifacts` 引用）。

### 3.3 PlanStep.kind（一期封闭枚举）

| kind | 职责 | 默认可人确认 |
|---|---|---|
| `intake_parse` | 需求条款化 | 否 |
| `coverage_design` | 覆盖维度 / 链路骨架（映射原 CP1） | 可 |
| `point_design` | 测试点拆解（映射原 CP2） | 可 |
| `case_generate` | 按点/批生成用例 | 否 |
| `review_coverage` | 覆盖评审子任务 | 是（提案采纳） |
| `review_quality` | 质量/有效性评审子任务 | 是 |
| `review_adoption` | 用例采纳建议子任务 | 是 |
| `repair` | Reflexion 触发的定点修补 | 否 |
| `await_human` | 显式等待（也可由 flag 表达） | — |

若 `human_gates.link/point=true`，首次产出对应产物后必须存在未完成确认门，否则 Reflect → `replan`。

## 4. 子任务、工具与评审

### 4.1 工具分层

| 层 | 工具 | 可用性 |
|---|---|---|
| 沙箱 I/O | `bash`、`str_replace_editor` | 主 Agent + 子任务 |
| 能力工具 | `retrieve_kb`、`write_artifact`、`generate_cases_batch`、`build_coverage_matrix`、`update_plan` 等 | 主 Agent；评审子任务多为只读 |
| 编排工具 | `spawn_subtask`、`await_human_confirm` | **仅主 Agent** |

实现：继续 LangChain Tool + `tool_agent` 循环；能力工具内部复用旧节点纯函数与落库协议。

### 4.2 子任务 runtime

```text
spawn_subtask(kind, goal, input_refs)
  → subtask_id
  → thread_id = `{task_thread}::sub::{subtask_id}`
  → 按 kind 编译子图并 ainvoke
  → 写 SubtaskResult artifact
  → 回写 state.subtask，推进当前 step
```

硬约束：

1. 同 task 同时最多 1 个 running 子任务。
2. 子图 tool 列表不含 `spawn_subtask`。
3. 子任务未完成时主 `plan_cursor` 不前进（step 保持 `running`）。
4. `cancel_requested` 同时取消当前子图。
5. 子任务 fail → Reflect `repair|replan`；达上限才 fail 整 task。

### 4.3 评审子任务

| kind | 输入 | 输出 | 人确认后 |
|---|---|---|---|
| `review_coverage` | clauses + points + cases + 可选程序化矩阵 | 未覆盖、补点/补例建议、`degraded` | 接受则插入 `repair`/`case_generate` |
| `review_quality` | 用例批 + checklist | 逐条 issue + 修订建议 | 接受则生成 `repair` steps |
| `review_adoption` | active cases + 前序提案摘要 | 每条 `adopt/edit_adopt/reject` + rationale | 人可改判后批量落 `review_status` |

最终采纳权在人。程序化覆盖矩阵为只读工具；是否补生成由提案 + 人确认驱动（取消主图硬编码「≤2 轮自动补」）。

### 4.4 Reflexion vs 评审子任务

| | Reflexion | 评审子任务 |
|---|---|---|
| 时机 | 每 execute step 后自动 | Plan 中显式 step |
| 成本 | 轻量、有上限 | 可多轮、可工具、可人审 |
| 职责 | 结构合法、门禁、明显漏维 | 覆盖深度、质量、采纳建议 |
| 产物 | `reflection_log` | `ReviewProposal` |

### 4.5 SSE 事件

新增：`plan_updated`、`step_started`、`step_finished`、`subtask_started`、`subtask_finished`、`reflection_result`、`review_proposal_ready`、`human_gate_waiting`。

旧 `checkpoint_waiting` / `clarification_needed` 可映射或短期并存适配。

## 5. 数据契约与 API

### 5.1 领域模型

| 类型 | 要点 |
|---|---|
| `AgentPlan` | `plan_id`, `version`, `goal`, `steps[]`, `status`, `replan_count` |
| `PlanStep` | `step_id`, `kind`, `goal`, `input_refs[]`, `output_ref?`, `status`, `requires_confirm`, `max_reflect` |
| `ArtifactRef` | `kind` ∈ clauses / coverage_design / point_plan / case_set / coverage_matrix / review_proposal / reflection / … |
| `SubtaskResult` | `subtask_id`, `kind`, `thread_id`, `status`, `summary`, `output_ref` |
| `ReviewProposal` | `scope`, `items[]`, `matrix_ref?`, `degraded?` |
| `HumanDecision` | `gate_kind` ∈ plan_confirm \| review_decision；`action` ∈ confirm \| modify \| reject_rerun |

旧 `LinkPlan` / `PointPlan` / 用例 MD 可作为对应 artifact 的 payload 形态复用字段，降低生成逻辑重写；**主索引改为 Artifact + Plan**。

### 5.2 持久化

- `stage_artifact` 泛化为 `artifact`（`kind` 替换/并存 `stage`；保留 version/status/confirmed_by）。
- `AgentPlan` 存为 `artifact.kind=agent_plan`；`task` 表增加 `current_plan_artifact_id`（指向 active plan）。
- `subtask` 表：task_id、thread_id、kind、status、result_artifact_id。
- `testcase` + `review_status` / `review_record` 保留；采纳来源改为 `HumanDecision`。
- 人机等待统一复用任务态 `waiting_confirm`，用 confirm body 的 `gate_kind` 区分 plan 确认与 review 决策（不新增 `waiting_review` 态）。
- **无旧任务热迁移**。

### 5.3 API

| 能力 | 变化 |
|---|---|
| 会话消息 | 任务绑定会话为主通道；`kind` 扩展：`plan_revision`、`review_decision`、`gate_confirm`；chat 与编排合流 |
| 确认 | `POST /tasks/{id}/confirm` 按 `gate_kind` + artifact/proposal id |
| Plan | `GET /tasks/{id}/plan`；可选人触发 replan |
| 子任务 | `GET /tasks/{id}/subtasks` |
| 评审 | `GET .../review-proposals/{id}`；决策走 confirm |
| 回退 | 仍派生 run；入口为修订后 AgentPlan + 继承 artifacts；`@阶段` → `@step_kind` / 产物 kind |

### 5.4 配置

- `human_gate_link` / `human_gate_point` / `human_gate_review`（默认 true/true/true）
- `reflect_max_per_step`（2）、`replan_max`（3）
- `subtask_timeout_sec`
- 既有 `tool_agent_max_steps` 等

## 6. 前端：对话为主

**主界面**：`SessionPage`（由 ChatPage 演进）

- 时间线：用户话 / Agent 叙述 / Plan 卡片 / Step 进度 / 工具摘要 / 子任务块 / 澄清卡 / 确认卡 / 评审提案卡
- 内联确认：原 StageConfirm 编辑器嵌入卡片
- 内联评审：ReviewProposal 可改判后提交
- 侧栏：Plan 树、产物列表、用例抽屉（Workbench 精简为浏览/单条编辑）

**路由**：`/` 会话为主；`/confirm`、`/workbench` 降为深链或移除独立主入口；调试页可留。

人不离开对话即可完成：确认计划 → 看生成 → 审提案 → 采纳。

## 7. 错误处理

| 场景 | 行为 |
|---|---|
| 工具失败 | 同 step 重试至上限 → Reflect repair 或 step failed |
| 子任务失败/超时 | `SubtaskResult(failed)` → Reflect；达 `replan_max` → task failed |
| 跳过必选人机门 | Reflect 强制 replan |
| LLM 结构化失败 | 既有重试；不写半份 active plan |
| 取消 | 停主环与当前子图；已提交产物保留 |
| 进程重启 | 主/子 thread checkpointer 恢复；子任务 running 视为未完成并重入 |
| 覆盖无法补全 | `ReviewProposal.degraded=true`；人确认后可 completed（告警降级） |

## 8. 测试策略

1. 单测：Plan reducer、Reflect 决策表、gate 开关、spawn 串行与禁嵌套、ReviewProposal → review_status
2. 图测：控制环拓扑、interrupt/resume、子 thread checkpoint
3. API 测：confirm `gate_kind`、SSE 新事件、会话 kind
4. 前端测：内联确认/评审卡、Plan/step 进度、旧入口降级
5. 场景：默认两级确认；关 gate 全自动；评审驳回重跑；子任务中取消

一期不要求旧五阶段主图回归绿灯；相关测试改为能力函数测或删除并在 handoff 登记。

## 9. 非目标（一期）

- 子任务并行或嵌套派发
- 旧任务热迁移 / 双轨适配层长期并存
- 纯 ReAct 替换控制环
- Planner/Worker 多 Agent 消息总线
- 工具层写入 ReMe

## 10. 文档联动（实现期）

实现计划落地后需同步修订：

- `docs/tech-design.md`：主图决策（显式四阶段 → 控制面 Plan-Execute）
- `docs/detailed-design.md`：状态、节点、API、事件契约
- `docs/PRD.md`：交互从「阶段确认页 + 工作台」改为「对话为主」
- `docs/plan/handoff.md`：范式切换 WP 进度
- `docs/superpowers/specs/2026-09-28-builtin-tools-design.md`：删除「不替换用例主图」原则，改为「控制面主图 + 工具子图」

## 11. 成功标准

1. 新任务走控制环完成：计划 →（确认）→ 生成 → 评审子任务 → 人确认采纳。
2. 关闭人机 gate 时可全自动跑通并带 degraded 告警语义。
3. Reflexion 能挡住「跳过确认」与明显结构/覆盖失败，并在上限内 replan/repair。
4. SessionPage 内可完成确认与评审，无需依赖独立 `/confirm`、`/workbench` 主路径。
5. 单 worker 约束下子任务串行、可取消、可断点续跑。
