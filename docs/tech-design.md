# TesterAgent 技术设计文档

| 项 | 内容 |
|---|---|
| 版本 | v0.3 |
| 状态 | 一期候选发布 |
| 日期 | 2026-09-28 |
| 对应需求 | [PRD v0.7](file:///D:/code/github/testerAgent/docs/PRD.md) |

> v0.3 变更摘要（WP-X2）：① §8 S1~S7 全部写入一期结论；② §9 开放问题与 PRD v0.7 对齐（Q1/Q2/Q6/Q7/Q11/Q12 closed）；③ 备份 CLI / 导出·提案发布门禁见 detailed-design §19.5 与 `docs/plan/release-checklist.md`。
>
> **嵌入式 ReMe（同日修订）**：S1/D18 改为同进程嵌入；HTTP `service` 删除；目录见 §6 `memory/` + `adapters/reme_sdk.py`；细节 [reme-memory 设计](superpowers/specs/2026-09-28-reme-memory-module-design.md)。
>
> **实现级契约**：[detailed-design.md v0.4](file:///D:/code/github/testerAgent/docs/detailed-design.md) 已对齐 Plan-Execute（控制环 §7、ConfirmIn.gate_kind、SessionPage、迁移 004）。
>
> v0.2 变更摘要：针对 v0.1 两轮架构评审（共 39 条意见，处理记录见[附录 A](#附录-a评审意见处理记录)）修订，主要变化：
> ① 新增 §2.1/§4.5 执行模型（Runner、进程内事件总线、重启 reaper、任务互斥与取消、单 worker 约束、LLMClient 韧性）；
> ② §3.2/§3.3 补回退与 checkpoint 协调语义、回退影响面分析、DB↔文件原子写入与对账协议；
> ③ §4.2 节点内批次级断点、幂等键、挂起通道通用化、局部重生成纳入版本体系；
> ④ §4.3 检索管线补降级/缓存/去重/硬上限/知识条目版本/注入排序；
> ⑤ §3.1 新增 message / kb_proposal / task_event 三张表及索引规划；
> ⑥ §5 补通用约定（错误模型/分页/幂等/乐观锁/SSE 续传）与 snapshot、消息、取消、连接测试、ad-hoc 检索等缺失端点；
> ⑦ §8 spike 扩为 7 项（新增 ReMe 能力 fallback、检索成本标定、黄金集 eval 最小管线）。

---

## 1. 设计目标与原则

| 目标 | 对应 PRD |
|---|---|
| 支撑四阶段流水线 + 两级人工确认的长程任务运行、断点续跑与阶段回退 | PRD 4.2 / US8.2 / US8.3 |
| 将"上下文精准策略"（召得全/裁得准/用得上）落成可独立调试的工程组件 | PRD 6.1 |
| 用例以 MD 文件为唯一产物实体，SQLite 存元数据与运行轨迹 | PRD Q6（本文给出结论） |
| ReMe 读写分离：写路径物理上不可达，除非用户显式确认 | PRD 7 / US1.3 |
| Monorepo 一键部署，单机单用户 | PRD 7 |

原则：最小持久化、图编排显式化、每阶段产物结构化且可溯源、检索全程留痕可回放、**跨边界操作有显式一致性协议（DB/文件、应用层/checkpoint）**、**所有外部依赖调用可重试、可降级、可观测**。

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────┐
│ L1 交互层  web/ (React + Vite)                            │
│   会话页 / 阶段确认工作台 / 用例工作台(MD渲染+编辑)          │
│   / 检索调试面板 / 工作区与智能体配置 / 模型配置             │
├─────────────────────────────────────────────────────────┤
│ L2 服务层  server/api (FastAPI)                          │
│   REST 接口 + SSE 事件推送 + 确认令牌签发                  │
│   TaskRegistry：任务互斥锁 / 取消令牌 / 运行态注册表        │
├─────────────────────────────────────────────────────────┤
│ L3 编排层  server/graph + runtime (LangGraph)            │
│   控制环：plan→dispatch→execute_step→await_human→reflect │
│   能力：intake / link_identify / point_write / case_generate │
│   子管线：精准检索；对话 tool_agent（可挂 memory_search）   │
│   Runtime：Runner + EventBus                             │
├─────────────────────────────────────────────────────────┤
│ L4 记忆层  server/memory                                 │
│   WorkspaceMemoryPool / ReMeMemoryManager（一工作区一嵌入） │
│   个人记忆（daily/digest）+ 可选共享 KB 挂载；auto_memory  │
│   对话工具 memory_search；**不**暴露 save_to_knowledge     │
├─────────────────────────────────────────────────────────┤
│ L5 接入层  server/adapters                               │
│   SdkReMeReader（只读，供 L3）/ SdkReMeWriter（仅 L2 confirm）│
│   LLMClient（DeepSeek）/ ExportService                   │
├─────────────────────────────────────────────────────────┤
│ L6 存储层  server/store + data/                          │
│   SQLite（WAL：元数据/轨迹/配置/事件）                     │
│   文件：用例 MD / 快照 / reme vault（data/workspaces/…）   │
│   Reconciler：启动与定时对账（DB 行 ↔ 文件）               │
└─────────────────────────────────────────────────────────┘
```

分层口径：L1→L6 按**调用与信任边界**自上而下——交互、门禁 API、编排、记忆能力本体、协议适配、持久化。`memory/` 单独成 L4，是因为它负责嵌入生命周期与个人/共享记忆语义；`adapters/reme_sdk` 只把同一嵌入实例适配为冻结的 `ReMeReader`/`ReMeWriter` Protocol（L5），不承担记忆业务本身。

关键约束：**L3 编排层仅持有只读 `ReMeReader`，`ReMeWriter` 只暴露给 L2 的专用确认端点**；对话可挂 `memory_search`，不可挂 `save_to_knowledge`。从代码结构上保证 agent 链路无法触达知识库写入（PRD 7 硬性要求）。

### 2.1 部署与并发约束（一期显式约束，不提前做分布式）

| 约束 | 说明 |
|---|---|
| 单进程单 worker | 后端以 uvicorn 单 worker 运行；图执行、EventBus、TaskRegistry、**嵌入式 ReMe 实例池**均为进程内组件，不做跨进程协调。部署脚本与文档写死该约束 |
| 并发任务 | 允许多任务排队/交错挂起，但**同一时刻同一 task 只允许一个 Runner 持有执行权**（§4.5 互斥）；不同 task 可并发，LLM/检索并发受适配器信号量限制 |
| 重启语义 | 进程重启不保证节点执行中的内存状态；恢复依赖最近一次成功的批次级持久化（§4.2③），由 Reaper 改判无主任务（§4.5）；嵌入 ReMe 随进程启停，vault 在工作区目录持久化 |
| 无后台调度器 | 不引入 celery/APScheduler；挂起超时检查、对账在启动时与 API 请求触达时惰性执行；一期无 dream/daily_paper cron |

---

## 3. 数据模型

持久化结论（回应 PRD Q6）：**用例正文 = 文件系统 MD 文件（唯一事实源）；SQLite = 工作区/会话/任务/阶段产物/评审状态/检索轨迹/事件/配置的元数据**。用例评审状态变更只更新 SQLite，内容编辑只改文件，以 `content_hash` 对齐；两类存储的提交顺序与失败修复由 §3.3 一致性协议保证。

### 3.1 表结构

**workspace（工作区，业务隔离单元）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | uuid |
| name / description | TEXT | |
| kb_config | TEXT(JSON) | 本工作区嵌入式 ReMe 配置：`{kb_id, knowledge_dir?, create_knowledge_base?, options}`；vault=`data/workspaces/{id}/reme/`（已废除 `mode`/`target`/HTTP service） |
| created_at / deleted_at | TEXT | 软删除：删除工作区先置 deleted_at，文件清理走保留期任务（§3.4） |

**agent（智能体；一期仅内置一条"用例智能体"，表结构为多智能体预留）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | |
| name / agent_type | TEXT | `case_designer` 等 |
| config | TEXT(JSON) | 生成策略、提示词模板、snapshot_level 默认档位 |
| created_at | TEXT | |

**agent_workspace（绑定关系，多对多）**

| 字段 | 类型 | 说明 |
|---|---|---|
| agent_id / workspace_id | TEXT FK | 联合主键 |

**conversation（会话）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | uuid |
| workspace_id | TEXT FK | 会话归属的工作区，创建时指定、不可变更 |
| title | TEXT | 会话标题（首条消息摘要生成） |
| created_at / updated_at | TEXT | ISO8601 |

**message（会话消息，澄清问答/对话驱动调整/重生成指令的载体）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | uuid |
| conversation_id / task_id | TEXT FK | task_id 可空（任务发起前的自由对话）；任务关联消息必填 |
| role | TEXT | `user / assistant / system` |
| kind | TEXT | `chat / clarification_qa / checkpoint_revision / change_request / regen_instruction` |
| content | TEXT | 消息正文（MD） |
| ref_artifact_id | TEXT FK? | checkpoint_revision 关联的阶段产物；regen_instruction 关联用例集合（另存 payload） |
| payload | TEXT(JSON) | 结构化附带数据（如重生成的 case_ids、阶段修订差异） |
| created_at | TEXT | |

**task（长程任务，一会话可关联多任务）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | uuid |
| conversation_id | TEXT FK | |
| workspace_id | TEXT FK | 冗余自会话，便于按工作区直接过滤与生成文件路径 |
| status | TEXT | `running / waiting_confirm / waiting_input / cancelling / completed / aborted / failed` |
| current_stage | TEXT | 阶段标识为**字符串**（不建 SQL 枚举），内置取值见 4.2；为后续 agent_type 的不同图预留 |
| requirement_ref | TEXT(JSON) | `{path, content_hash, clause_count}`；需求原文落文件 `requirement.md`，**不入 state、不逐 checkpoint 复制**（见 4.1、S3） |
| clauses | TEXT(JSON) | intake 产出的条款索引 `[{clause_id, anchor, title}]`（不含正文），覆盖矩阵与溯源的稳定锚点（§4.2①） |
| langgraph_thread_id | TEXT | LangGraph checkpointer 的 thread_id，断点续跑凭据 |
| graph_run_id | TEXT | 当前应用层图运行的标识；回退重跑生成新 run（§3.2②） |
| runner_heartbeat | TEXT | Runner 持有执行权期间周期性续租时间戳；Reaper 据此判定无主任务（§4.5） |
| cancel_requested | INTEGER | 0/1；协作式取消标志，节点在批次边界检查 |
| error_info | TEXT(JSON) | failed 时的错误码、可重试标记、失败节点（§5.0 错误模型） |
| created_at / updated_at | TEXT | |

**stage_artifact（阶段产物，回退机制核心）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | uuid |
| task_id | TEXT FK | |
| stage | TEXT | 字符串标识；内置 `link_identify / point_write / case_generate / coverage_check` |
| graph_run_id | TEXT | 产出该版本的图运行；回退重跑后切换为新 run（§3.2②） |
| stage_version | INTEGER | 同一 (task_id, stage) 单调递增；回退重跑产生新版本 |
| origin | TEXT | `system / user_revised`；用户在检查点修改后放行的版本记 user_revised（R28） |
| status | TEXT | `active / superseded / obsolete`（见 3.2） |
| payload | TEXT(JSON) | 结构化产物：链路清单 / 测试点清单 / 覆盖矩阵 |
| confirmed_by | TEXT | `user / auto`，检查点产物为 user |
| created_at | TEXT | |

唯一索引：`(task_id, stage, stage_version)`。

**testcase（用例元数据，正文在文件系统）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | uuid |
| task_id | TEXT FK | |
| point_id | TEXT | 来源测试点 id（溯源） |
| stage_version | INTEGER | 所属 case_generate 阶段版本，回退后旧版本批量置 obsolete |
| lineage | TEXT(JSON) | 版本血缘：`{root_case_id, regenerated_from_case_id?}`；局部重生成不绝嗣（R8，§4.2④） |
| status | TEXT | `active / obsolete` |
| review_status | TEXT | `pending / adopted / edited_adopted / rejected`，转换规则见 3.2④ |
| file_path | TEXT | 相对 data/ 的 MD 文件路径 |
| content_hash | TEXT | 编辑检测、乐观锁与导出一致性校验 |
| title | TEXT | 列表展示用，冗余自 MD 标题/front-matter |
| trace_refs | TEXT(JSON) | 引用来源：需求条款/知识条目 ID 列表（US4.2） |
| updated_at | TEXT | |

**retrieval_trace（检索轨迹，调试面板数据源）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | uuid |
| task_id / stage / stage_version | | 归属定位 |
| graph_run_id / node / batch_id | | 精确定位某次执行与节点内批次（§4.2③） |
| query_variant | TEXT | 本路召回的 query 表述 |
| candidates | TEXT(JSON) | `[{entry_id, entry_version, title, score, source_channel, kept, drop_reason, latency_ms, error?}]`（R24/R39） |
| injected_ids | TEXT(JSON) | 最终注入上下文的条目 ID（含注入 position） |
| referenced_ids | TEXT(JSON) | 生成产物实际引用的条目 ID（闭环校验，校验规则见 §4.3⑥） |
| degraded | TEXT(JSON) | 本次管线的降级记录：`[{step, reason, fallback}]`（§4.3④） |
| created_at | TEXT | |

归因口径直接由此表算出：未召回（目标条目不在任何 candidates）、被裁掉（kept=false 的 drop_reason 分布）、未用上（injected − referenced）。

**context_snapshot（注入上下文快照，监控数据源，详见 4.4）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | |
| task_id / stage / stage_version / node | | 定位到具体图节点的某次执行 |
| graph_run_id / batch_id | | 批次级定位 |
| items | TEXT(JSON) | `[{entry_id, entry_version, title, tokens_est, position, char_offset, byte_length}]`；offset/length 支撑 JSONL 单行随机读（R38） |
| snapshot_path | TEXT | 快照全文文件路径（相对 data/，JSONL）；`snapshot_level` 非 `full` 时为空 |
| total_tokens_est | INTEGER | 注入总量估算 |
| budget / truncated | INTEGER / INTEGER | 该阶段预算上限 / 是否发生截断 |
| prompt_template_ver | TEXT | 提示词模板版本 |
| model_ref | TEXT(JSON) | 实际调用模型：`{provider, model, temperature, top_p}`——跨版本质量对比的前提（R21） |
| usage | TEXT(JSON) | LLM 返回的 prompt/completion tokens 实际值；检索管线自身的辅助 LLM 调用按 node 子项另计 |
| latencies | TEXT(JSON) | 各算子耗时毫秒：`{multi_query, recall, filter, rerank, extract, assemble, llm}`（R39） |
| created_at | TEXT | |

**task_event（SSE 事件留痕，断线续传与重启后回放的数据源）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | 单调递增，即 SSE 游标（Last-Event-ID） |
| task_id | TEXT FK | |
| type | TEXT | 事件类型同 §5.7 |
| payload | TEXT(JSON) | |
| created_at | TEXT | |

保留期默认 7 天（配置项），清理随惰性维护任务执行（§3.4）。

**review_record（评审操作留痕）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | |
| task_id / testcase_id | TEXT FK | |
| action | TEXT | `adopt / edit / reject / regenerate_request` |
| detail | TEXT(JSON) | 编辑字段差异、重生成指令等 |
| created_at | TEXT | |

**kb_proposal（知识库写入提案，两阶段确认的落处）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | uuid |
| workspace_id / task_id | TEXT FK | |
| payload | TEXT(JSON) | 建议新增/修改的链路、用户故事内容与来源 |
| status | TEXT | `pending / confirmed / rejected / expired` |
| confirm_token_hash | TEXT | 确认令牌哈希（一次性，§5.6） |
| confirmed_at / expires_at | TEXT | 令牌/提案有效期；过期由惰性任务置 expired |
| idempotency_key | TEXT | 确认请求幂等键（唯一索引），网络重试安全（R16） |
| created_at | TEXT | |

**config（平台级配置，单行 JSON）**

| 字段 | 说明 |
|---|---|
| model_config | DeepSeek API base/key（平台全局；本地单机，明文存本地即可） |
| runtime_config | 单任务 LLM 并发信号量、事件保留期、快照保留期、索引注入硬上限等运行参数 |

> 智能体配置（生成策略/提示词）在 `agent.config`，知识库挂载在 `workspace.kb_config`，不再放全局 config。

**索引规划（高频查询，随首版 DDL 一并建立）**

- task：`(workspace_id, status)`、`(conversation_id)`、`(runner_heartbeat)`
- stage_artifact：`(task_id, stage, status)`
- testcase：`(task_id, status, review_status)`、`(point_id)`
- retrieval_trace / context_snapshot：`(task_id, stage, stage_version)`
- message：`(conversation_id, created_at)`、`(task_id)`
- task_event：`(task_id, id)`
- kb_proposal：`(workspace_id, status)`

### 3.2 回退与 stage_version 状态机

```
阶段产物生命周期：

  生成 ──► active ──(用户回退到上游某阶段)──► obsolete
            │
            └──(本阶段被重跑)──► superseded
```

规则：

1. 每阶段每 run 只有一个 `active` 版本；新 active 产生时旧版本置 `superseded`。
2. **回退 = 一次带修订输入的新图运行（graph_run_id 切换）**，三步按固定顺序执行：
   ① **影响面分析**：比对目标阶段修订前后的结构化差异（链路/故事/测试点的增删改及 ID 映射），标记下游每个产物为 `affected / unaffected`；
   ② **状态落账**（一个 DB 事务内）：目标阶段旧 active 置 `superseded`（保留差异留痕），其下游 `affected` 产物置 `obsolete`、`unaffected` 产物**继承到新 run**（保持 active 与评审结论不丢，R7）；task 回拨 current_stage、写新 graph_run_id；
   ③ **checkpoint 协调**（R2）：旧 thread 的 checkpoint 标记为该 run 废弃（不删除，留档），新 run 使用**派生 thread_id**（`{old_thread_id}::run{N}`）并以修订后的结构化产物作为入口 state 初始化——不回放旧 checkpoint 内部状态，避免旧 link_plan/point_plan 残留；state 初始化与步骤②在同一临界区内完成。
3. 用例按 `stage_version` 批量作废：case_generate 重跑时受影响 version 的 testcase 行置 `obsolete`（unaffected 用例继承），文件保留在版本目录不物理删除，UI 默认只展示 active。
4. **review_status 转换规则（正交于 status）**：
   - pending → adopted / edited_adopted / rejected 均可；adopted/edited_adopted/rejected 之间允许互转（用户改判），每次转换写 review_record；
   - 用例被局部重生成时：旧行按用户选择保留（review_status 不变，status 随其版本）或置 obsolete；新行经 lineage 挂根用例；
   - **采纳率统计口径**：只统计 `status=active` 用例的最新评审动作，obsolete 行不计分子分母（对接 PRD 2.2/Q3，Q3 折算细则仍开放）。
5. `origin=user_revised` 的检查点版本同样占用 stage_version 序号，与系统版本在时间线上连续排列，保证"谁在何时改了什么"可回放。
6. `waiting_confirm / waiting_input` 状态下任务可被安全中断；重启后凭当前 run 的 thread_id 从 checkpointer 恢复。挂起超过配置阈值（默认 7 天）恢复时，前端提示"知识库/模型配置可能已变更"，由用户选择继续或先重跑检索（R30）。

### 3.3 数据目录与一致性协议

**目录约定**（原 workspace/ 更名 data/，避免与"工作区"概念冲突）：

```
data/
└── workspaces/
    └── {workspace_id}/            # 工作区级隔离
        ├── reme/                  # 嵌入式 ReMe vault（daily/digest/knowledge…）
        └── {task_id}/
            ├── requirement.md     # 需求原文副本
            ├── snapshots/         # 上下文快照全文（JSONL，见 4.4②）
            │   └── {stage}/v{stage_version}/{batch_id}-{node}-{snapshot_id}.jsonl
            └── cases/
                ├── v1/            # case_generate stage_version=1
                │   ├── {case_id}--{point_id}--{slug}.md
                │   └── ...
                └── v2/            # 回退重跑后的新版本
```

**文件命名（支持一个测试点多条用例，R27）**：以 `{case_id 短码}--{point_id}--{slug}.md` 命名，case_id 为主键先行；MD 顶部写 YAML front-matter（`case_id / point_id / stage_version / trace_refs / content_hash`），文件被外部工具搬动后仍可对账。

testcase.file_path、context_snapshot.snapshot_path 均存相对 `data/` 的路径。

**DB ↔ 文件系统一致性协议（R1）**：

1. **产物提交顺序固定为"先文件、后 DB"**：写文件一律 `同目录临时文件 → flush+fsync → 原子 rename 到目标路径`；rename 成功后才写 testcase/artifact 行。失败在任一步：临时文件可安全丢弃；DB 行缺失仅产生"孤儿文件"，不产生悬空引用。
2. **删除只做标记不删文件**：obsolete/软删除均不动文件系统，物理清理仅由保留期任务按 DB 状态批量执行。
3. **Reconciler 对账**（启动时全量、运行中按任务惰性触发）：
   - DB 有行、文件缺失 → 行置 `error_info=file_missing`，UI 标红并提供"标记作废/从 v 目录找回"；
   - 文件存在、DB 无行（孤儿文件）→ 登记隔离目录清单，不自动挂接；
   - 行 hash ≠ 文件 hash（外部改动）→ UI 提示冲突，由用户选择"以文件为准重新入库"或"以库/版本为准覆盖"，不静默覆盖。
4. **导出一致性**：打包前逐个校验 content_hash，不一致用例排除并在导出清单中列明。

**保留期与清理（R31）**：`full` 快照文件、obsolete 用例文件、task_event 默认保留 30 天（runtime_config 可配），由惰性维护任务（启动 + 工作区访问触发）清理；清理前确认无 active 行引用。

### 3.4 工作区与隔离

**定义**：工作区 = 一个业务/项目的隔离单元，绑定一份 ReMe 知识库连接（`kb_config`）。智能体与工作区为**多对多**：创建智能体时绑定一个或多个工作区（`agent_workspace`），一个工作区也可被多个智能体使用。

**隔离的三个层面**：

| 层面 | 机制 |
|---|---|
| 数据 | 会话/任务及其下游产物均挂 workspace_id（且强制 `workspace.deleted_at IS NULL` 过滤），DAO 层不提供无工作区参数的查询方法 |
| 检索 | 按工作区 `kb_config` 从 `WorkspaceMemoryPool` 取嵌入实例 → `SdkReMeReader`；任务运行期内只能访问本工作区知识库，不存在跨库检索路径 |
| 文件 | 用例 MD 与快照按 `data/workspaces/{workspace_id}/` 目录物理隔离 |

**删除级联**：DELETE 工作区 = 软删除 + 校验无 running/waiting 任务；DB 下游行随查询过滤逻辑隔离；文件目录在保留期后由清理任务物理删除，删除失败仅记日志并在下次维护重试（不阻塞 API）。

**一期 UI 简化**：智能体为内置的"用例智能体"（不做创建/市场 UI，PRD 3.3），工作区管理页可创建工作区、配置知识库连接、勾选绑定的智能体；模型配置保持平台全局。此设计同时回应 PRD Q7：平台对多智能体的预留落在**数据模型层 + 执行注册层**（agent 表 + 绑定关系 + 阶段字符串标识 + 图按 agent_type 注册，见 4.2），UI 层不提前建设。

---

## 4. LangGraph 图设计

> **2026-09-28 范式更新**：生产主图为控制环 `plan → dispatch → execute_step → await_human → reflect`（动态 Plan-Execute + Reflexion）。阶段能力（intake / link_identify / …）由 `execute_step` 经 `invoke_capability` 调度，不再作为 StateGraph 拓扑节点。完整契约见 [plan-execute-reflexion 设计](../superpowers/specs/2026-09-28-plan-execute-reflexion-design.md)。下文 §4.1–§4.2 描述的是**阶段能力语义**（由控制环 dispatch）。

### 4.1 主图状态（State）

```python
class TaskState(TypedDict):
    task_id: str
    graph_run_id: str
    workspace_id: str               # 节点据此取本工作区的 ReMeAdapter（检索隔离边界）
    clauses: list[ClauseRef]        # intake 产出的需求条款索引（正文在文件，不进 state）
    # 阶段产物（结构化，阶段间唯一传递物 —— 对应 PRD 6.1 "阶段隔离"）
    link_plan: LinkPlan | None        # 链路/用户故事清单
    point_plan: PointPlan | None      # 测试点清单
    case_batch: CaseBatch | None      # 本批生成结果引用（文件路径列表）
    coverage: CoverageMatrix | None
    # 控制字段
    clarification_questions: list[Clarification]  # 任意节点均可挂起提问（见 4.2②）
    current_stage_version: dict[str, int]
    batch_cursor: dict[str, int]     # 节点内批次进度游标（见 4.2③）
```

注意：state 中**不放需求正文、不放检索到的知识正文、不放对话历史**；知识只在各节点内部组装进 prompt，需求正文按 clause_id 从文件按需读取——这是"阶段隔离"的落地点，也避免长需求被逐 checkpoint 复制膨胀。

### 4.2 节点与流转

```
                 ┌─────────────┐
  START ────────►│ intake      │ 需求解析+条款切分(clause_id)；信息不足→挂起提问(waiting_input)
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐
                 │ link_identify│ 调检索子图(索引摘要档)→链路清单；歧义→挂起提问
                 └──────┬──────┘
                        ▼
                 ╔═════════════╗
                 ║ CHECKPOINT_1 ║ interrupt_before；等待用户确认/修改
                 ╚══════┬══════╝   写入 stage_artifact(origin=user_revised 视情况)
                        ▼
                 ┌─────────────┐
                 │ point_write  │ 按故事分批：逐批检索(段落级)→测试点（批次级断点）
                 └──────┬──────┘
                        ▼
                 ╔═════════════╗
                 ║ CHECKPOINT_2 ║ interrupt_before；等待用户确认/修改
                 ╚══════┬══════╝
                        ▼
                 ┌─────────────┐
                 │ case_generate│ 按测试点分批：检索+生成→写MD+testcase行（批次级断点）
                 └──────┬──────┘
                        ▼
                 ┌─────────────┐
                 │ coverage_check│ 条款×产物覆盖矩阵；未覆盖→补充生成(有上限)
                 └──────┬──────┘   或降级为告警写入产物
                        ▼
                  review_export（END，等待评审操作驱动后续）
```

实现要点：

- **① 需求条款化（R26）**：intake 对 requirement.md 做结构化切分，产出稳定 `clause_id`（规则：`h{标题层级}-{同级序号}`，锚定标题路径与原文 hash）；条款索引存 `task.clauses`，覆盖矩阵、trace_refs、point/case 溯源全部引用 clause_id。用户在对话中修订需求（US 变更场景）时，未变动条款保持原 ID，新增条款追加序号，被删条款标 deleted 而非复用 ID。
- **② 中断/恢复与通用挂起**：两个检查点用 LangGraph `interrupt_before` + SQLite checkpointer（D1）。**挂起提问不是 intake 专属**：任一节点发现信息不足，均可写 `clarification_questions` 并 interrupt 为 `waiting_input`；用户答复以 message(kind=clarification_qa) 落库后 resume（R32）。对话驱动的需求调整（PRD 4.2 变更场景）统一走 message(kind=change_request) → API 层判定为"续跑当前图"或"回退重跑"（R33）。
- **③ 节点内批次级断点（R3）**：point_write / case_generate 以"故事 / 测试点分批"为最小工作单元。每批开始前在 `stage_artifact.payload.progress`（或独立 progress 行）写 `{batch_id, unit_ids, status: started}`，批完成后写 `done` 并落本批产物；checkpoint 在批次边界推进。节点恢复时读游标：`done` 批次跳过（凭 idempotency_key 防重复文件/行），`started` 批次判定为未完成并重做（临时文件+原子 rename 保证重做不留残骸）。批次大小默认 5 个测试点（runtime_config 可调）。
- **④ 局部重生成纳入版本体系（R8）**：US5.3 由 API 层调用 case_generate 的**节点函数（同一套写入/留痕/快照代码路径）**，作为当前 case_generate stage_version 下的增补批次执行（batch_id 独立），retrieval_trace/context_snapshot 正常归属该 stage_version 与 batch_id；新用例行写 lineage，旧用例保留/废弃由用户选择。不产生游离的第二条产物链路。
- **⑤ 回退**：按 §3.2 规则 2 执行，派生 run、影响面继承、初始化新 checkpoint。
- **⑥ 图注册**：`main_graph.py` 以 agent_type → 编译后图的注册表组织，一期仅注册 case_designer 的主图；阶段标识全程字符串，不建枚举常量耦合（R37）。

### 4.3 检索子图（精准检索管线）

三个阶段节点复用同一个子图，仅配置不同：

```
retrieve_subgraph(config: RetrievalConfig)
  ┌─ multi_query      生成 N 路 query 表述（原文/关键词/同义改写）
  ├─ parallel_recall  并行调 ReMeAdapter.search，top-K 放宽，并集去重
  ├─ meta_filter      按知识类型/链路归属结构化过滤
  ├─ rerank           LLM 相关性打分，按预算 top-N 截断
  ├─ passage_extract  长文档段落级抽取
  └─ assemble         组装注入块（每条带来源 ID + entry_version），全程写 retrieval_trace
```

**阶段差异化配置**（对应 PRD 6.1 表格；预算值为初始值，由 S5 成本标定后固化）：

| 配置项 | link_identify | point_write | case_generate |
|---|---|---|---|
| 检索对象 | 链路/故事索引摘要 | 业务规则/主流程用例/缺陷 | 接口/DB/缺陷回归点 |
| query 路数 | 3 | 4 | 按测试点 2~3 |
| top-K（召回） | 50 | 40 | 30/测试点 |
| 注入上限（重排后） | **≤200 条硬上限（全量索引超限时截断+budget_warning，R22）** | ~20 条 | ~15 条/批 |
| 注入形态 | 标题+一句话 | 段落级 | 段落级 |

**降级链（R4/R9，每步降级写 trace.degraded）**：

| 环节 | 主路径失败/超限时 |
|---|---|
| multi_query LLM 失败 | 退化为单路原文 query，不阻断流程 |
| ReMe 元数据过滤不可用（S1 未通过） | 退化为本地"索引摘要镜像"过滤：适配器侧缓存链路/故事索引树（仅标题+一句话+类型+归属，随任务启动刷新），**不做对 ReMe 的二次开发写入**；若镜像也不可用则粗排后全量交 rerank |
| rerank LLM 失败/超时 | 退化为召回分排序 + 规则分数（关键词覆盖/类型权重），标记 degraded |
| passage_extract | 默认**结构化切分**（标题/段落边界），仅在切分质量不足时用 LLM 抽取；LLM 失败退化为固定窗口截取 |
| 单路检索失败 | 其余路结果继续，trace 标 error；全部失败则节点中断为 task_error（可重试） |

**缓存与去重（R9/R23）**：

- 进程内 LRU 缓存复用同一 run 内的 `query → 召回结果` 与 `entry_id+entry_version → rerank 分数/段落抽取结果`，跨批次不重复打分、不重复注入；缓存键含 entry_version，知识更新后自然失效；
- case_generate 设跨批**用例去重**：以（point_id 邻接组 + 标题规范化 + 关键步骤指纹）做生成后查重，命中则合并/提示，不重复出条；
- 候选中检出**互相矛盾条目**（同链路、同主题、更新时间差异或显式 supersedes 标记）时，两条都保留但在注入块中显式标注"可能冲突"并提示模型在产物中引用所采纳的版本，trace 留痕——不做自动裁决。

**闭环校验（用得上，R10）**：注入条目带统一来源 ID（白名单注入，提示词只允许引用白名单 ID）；生成后程序化解析产物引用，未在白名单出现的 ID 视为幻觉引用（记 warning 不计 referenced）；"注入未引用"差集照常落 trace。在此基础上对"引用了但正文无实质相关段落"的疑似贴标签行为，用轻量关键词重叠度做二次标记（仅标记供调优分析，不影响产物），降低假阳性对归因指标的污染。

**知识版本（R24）**：candidates 与注入 items 均记录 `entry_version`（ReMe 更新时间或内容 hash，S1 确认取哪个）；同一任务跨小时/跨天执行时，调试面板可提示"本条目自上次阶段后已更新"，保证归因能对齐"当时看到的内容"。

**注入排序**：高相关条目置于首尾锚点位、次相关居中，同一条目不重复占位；position 与排序策略版本一并入快照，使 position 数据既能解释也能改进 lost-in-the-middle（R25）。

**覆盖闭环**：生成节点输出后，程序化比对 injected_ids 与产物引用，差集写入 retrieval_trace；覆盖校验节点产出条款（clause_id）×产物覆盖矩阵写入 stage_artifact。

### 4.4 上下文监控（每个环节可观测）

监控对象分三层，全部落 SQLite，检索调试面板统一回放：

**① 检索漏斗 —— 哪条知识在哪一步被裁掉**

检索子图每个算子的输出计数、被裁条目原因、单路延迟与错误，随 `retrieval_trace.candidates`（`kept/drop_reason/latency_ms/error`）与 `context_snapshot.latencies` 留痕。面板按阶段渲染漏斗：召回数 → 过滤后 → 重排截断后 → 实际注入。

**② 组装快照 —— 模型当时到底看到了什么**

快照采集为**三级开关**（`snapshot_level`，可在 agent 配置全局设置、创建任务时单任务覆盖）：

| 级别 | 行为 | 适用 |
|---|---|---|
| `off` | 不产任何快照（检索漏斗的 retrieval_trace 仍始终留痕，属轻量必留） | 稳定运行期 |
| `meta` | 只写 DB 元数据行（items 清单、tokens、usage、model_ref、latencies），不落全文文件 | **默认** |
| `full` | meta + JSONL 全文落盘 | 调试期：需要还原模型看到的原文时开启 |

`full` 模式下的落盘方式：

- **全文写文件**：以 JSONL 流式逐条 append 至 `.../snapshots/{stage}/v{n}/{batch_id}-{node}-{snapshot_id}.jsonl`，每行一条知识（entry_id, entry_version, title, tokens_est, position, content），写入前记录该行字节偏移与长度到 items 的 `char_offset/byte_length`，回放可定位读取单行，不整文件载入；
- **DB 只存元数据**：items 清单（含偏移）与 snapshot_path，不存正文；
- **回放按需加载**：调试面板默认只读元数据列表，点开某条时按偏移读该行；无法定位偏移时退化为顺序扫描（兼容旧数据）。

两个明确取舍：

1. **开关只对开启后的新运行生效**——历史执行未存全文时无法补录，需开 `full` 后重跑该阶段（有回退/重跑机制支撑，成本低）；
2. 存全文的意义：快照独立于知识库可回放——即使后续知识库内容变更，仍能还原"第 N 次生成时模型看到的原文"。

**③ 用量与闭环 —— 花了多少、用没用上**

- `LLMClient` 统一封装，每次调用记录 usage 与 model_ref（写入 context_snapshot），按 task → stage → node → batch 聚合；multi_query/rerank 等检索辅助 LLM 调用计入对应节点子项，成本归因到阶段；
- 生成后 injected vs referenced 差集（§4.3⑥）；
- `total_tokens_est` 超预算或发生截断时，SSE 推送 `budget_warning`。

SSE 新增事件：

```
context_assembled   # {node, batch_id, item_count, tokens_est, budget, truncated}
budget_warning      # {node, tokens_est, budget}
```

不引入 LangSmith 等外部 tracing：自研留痕已覆盖需求，且避免需求/业务数据再出一份到第三方（见 D9）。

### 4.5 执行模型与并发控制

**Runner（图执行宿主）**

- `POST /tasks/{id}/run` 在 TaskRegistry 注册执行权：以 task_id 为键的进程内互斥锁（asyncio.Lock + owner token），**已持锁时重复 run 返回 409**（R6）；
- Runner 在后台 asyncio task 中执行图，周期续租 `task.runner_heartbeat`（默认每 10s）；图终态（completed/aborted/failed/waiting_*）落库后释放锁；
- **取消**：`cancel_requested=1` 为协作式信号，节点仅在批次边界检查（不杀正在进行的 LLM 调用）；检查到后置 `aborted` 并停在最近批次边界，已确认产物保留；
- **状态守卫**：confirm/answer/rollback/regenerate 仅在允许的源状态下接受（waiting_confirm / waiting_input / completed 评审期等），且必须携带期望的 artifact_id/version，否则 409（§5.0）。

**EventBus（进程内发布订阅）**

- 图节点通过统一 `emit(event)` 发事件：同步写 task_event 表（持久化）+ 发布到进程内 pub/sub；
- `GET /tasks/{id}/events` 订阅时：先按 `Last-Event-ID`（缺省取最近 N 条/订阅时刻）回放 task_event 表补齐，再挂总线订阅实时事件——解决"先 run 后订阅、多标签页、断线重连"（R12/R19）；
- 事件不做跨进程广播（单 worker 约束内足够）。

**Reaper（重启/故障改判）**

- 进程启动时扫描 `status=running 且 runner_heartbeat 早于阈值（默认 2 分钟）` 的任务：改判为 `failed`（error_info=interrupted_by_restart，可重试），等待用户/API 重新 run；重新 run 时从最近批次断点恢复（§4.2③）；
- running 任务调 run 同样先看心跳，避免僵尸持锁。

**SQLite 并发**：业务库与 checkpoint 库均开启 **WAL + busy_timeout（5s）**；所有多步写操作走短事务；checkpointer 库独立连接串配置。

**LLMClient 韧性契约（R20）**

| 项 | 策略 |
|---|---|
| 超时 | 连接 10s / 读取流式 120s（可配） |
| 重试 | 429/5xx/网络错误指数退避重试 3 次（抖动），4xx 不重试 |
| 限流 | 进程内信号量限制并发（runtime_config，默认 4），排队不报错 |
| 结构化输出 | JSON 解析/Pydantic 校验失败时携带校验错误自动重请 1 次；仍失败则该节点 task_error（retryable），不产出半成品 |
| 取消 | 流式调用随批次取消信号在边界中断 |
| 记录 | 每次调用记 model_ref、usage、耗时、重试次数到 context_snapshot |

**运行中配置变更（R21）**：model_config / agent.config 变更只对**之后启动的新 run 与新批次**生效；同一 run 内冻结配置快照（model_ref 可溯源）。变更模型配置后，挂起中任务恢复时前端提示配置已变化。

**离线评测（R5，内部基础设施，非用户功能）**：`server/eval/` 提供最小 eval 管线——导入黄金集（Q4：历史需求 + 已采纳用例，5~10 组），可对指定 prompt_template_ver / 预算配置 / 模型跑指定阶段，输出召回率（目标条目是否在 candidates）、注入率、引用率、条款覆盖率与 token/延迟成本，并支持两版配置 diff。该管线复用检索子图与节点函数，不另建一套逻辑；无黄金集前以冒烟集（少量手工构造样例）保证回归。

### 4.6 内置工具与 tool-agent 子图（D17）

用例智能体一期提供 `bash`（持久 shell）与 `str_replace_editor`（view/create/str_replace/insert）：

- **实现**：`server/tester_agent/tools/` + `graph/tool_agent.py`；`ChatOpenAI.bind_tools` ↔ `ToolNode` 循环直至无 tool_calls 或触顶 `tool_agent_max_steps`。
- **沙箱**：工作区根 `FileStore.root/workspaces/{workspace_id}/`；禁止写入 `**/snapshots/**`；owner 隔离（`conversation:{id}` / `task:{id}`）。
- **对话**：`POST /conversations/{id}/messages` 且 `kind=chat` → 跑子图 → 落库 assistant，`payload.tool_trace` 精简审计；`kind=change_request` 不进 tool loop。
- **产线**：`agent.config.enable_tools_stages`（默认 `[]`）列出的节点在 json_schema 生成前可选 `maybe_gather_with_tools`；`coverage_check` 不开。
- **Shell 后端**：`runtime_config.tool_shell_backend=auto` 时 Windows 探测 bash → pwsh；工具名仍为 `bash`。`cli check` 报告探测结果。
- **配置键**：`tool_bash_timeout_ms`、`tool_max_output_chars`、`tool_agent_max_steps`、`tool_shell_backend`。

---

## 5. API 契约

统一前缀 `/api/v1`。流式交互走 SSE。

### 5.0 通用约定

**错误模型**（所有非 2xx 统一）：

```json
{ "error": { "code": "TASK_STATE_CONFLICT", "message": "...",
             "retryable": false, "details": {} }
```

内置错误码分段：`AUTH_* / VALIDATION_* / NOT_FOUND / TASK_STATE_CONFLICT / VERSION_CONFLICT /
KB_* / LLM_UPSTREAM / LLM_TIMEOUT / RATE_LIMITED / FILE_CONFLICT / INTERNAL`。SSE 内致命错误使用同一 code 的 `task_error` 事件。

**分页**：列表接口统一 `?limit=&cursor=`（不透明游标，基于自增 id/创建时间），响应 `{items, next_cursor}`；默认 limit 50、最大 200。cases/traces/snapshots/messages/events/conversations 均遵循。

**幂等与并发控制**：

- 所有 POST 支持可选 `Idempotency-Key` 头，服务端按 (端点, key) 去重（kb confirm、regenerate、tasks/run 必须）；
- `PUT /cases/{id}` 须携带 `If-Match: {content_hash}`，不匹配返回 `VERSION_CONFLICT` 与当前文件 hash（乐观锁，R16）；
- confirm/rollback 须携带目标 `artifact_id` 与期望 `stage_version`，与当前 active 不符返回 `TASK_STATE_CONFLICT`。

**SSE 续传**：`GET /tasks/{id}/events` 支持 `Last-Event-ID` 头（或 `?after_event_id=`），服务端保证按 task_event.id 顺序补发后再接实时流。

### 5.1 工作区与智能体

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/workspaces` | 列表/创建工作区（POST body: name, description, kb_config） |
| GET/PUT/DELETE | `/workspaces/{id}` | 详情/更新/软删除（需确认无活跃任务，文件按保留期清理） |
| POST | `/workspaces/{id}/kb/test` | 测试 ReMe 连接（只读探活：list_tree 小样），返回可达性/延迟/能力位（是否支持元数据过滤，S1） |
| GET/POST | `/workspaces/{id}/agents` | 本工作区绑定的智能体列表 / 绑定 |
| GET/POST | `/agents` | 智能体列表 / 创建（一期 UI 不开放创建，接口预留） |
| GET/PUT/DELETE | `/agents/{id}` | 详情/更新/删除 |

### 5.2 会话与任务（均归属工作区）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/workspaces/{id}/conversations` | 某工作区的会话列表（分页） |
| POST | `/conversations` | 新建会话（body: workspace_id, title?） |
| GET | `/conversations/{id}` | 会话详情（含任务与分页消息） |
| POST | `/conversations/{id}/messages` | 发送自由消息（chat / change_request；系统决定续跑或回退，见 4.2②） |
| GET | `/conversations/{id}/messages` | 消息历史（分页） |
| POST | `/tasks` | 创建任务（body: conversation_id, requirement_md, snapshot_level?） |
| GET | `/tasks/{id}` | 任务详情：status/current_stage/各阶段 active 产物/批次进度/error_info |
| POST | `/tasks/{id}/rollback` | 回退（body: target_stage, artifact_id, expected_version, revised_artifact?），返回影响面分析结果 |

### 5.3 执行与确认

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tasks/{id}/run` | 启动/恢复图运行（幂等键；已在运行→409），返回 SSE 流地址与当前批次进度 |
| POST | `/tasks/{id}/cancel` | 协作式取消（批次边界生效） |
| GET | `/tasks/{id}/events` | SSE：支持 Last-Event-ID 补发；事件类型见 5.7 |
| POST | `/tasks/{id}/confirm` | 检查点放行（body: stage, artifact_id, expected_version, action=confirm/modify, payload=修订清单） |
| POST | `/tasks/{id}/answer` | 回答澄清问题，resume 图（消息同步落 message 表） |

### 5.4 用例

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tasks/{id}/cases` | 用例列表（分页；含 review_status，默认只含 active；可按版本/评审状态过滤） |
| GET | `/cases/{id}` | 用例详情（MD 正文 + trace_refs + lineage） |
| PUT | `/cases/{id}` | 编辑 MD 正文（If-Match 乐观锁；成功后更新文件+hash，review_status→edited_adopted） |
| POST | `/cases/review` | 批量评审（body: [{case_id, action}]，服务端按状态机校验转换合法性） |
| POST | `/cases/regenerate` | 局部重生成（幂等键；body: case_ids, instruction，走 4.2④ 同路径） |
| POST | `/tasks/{id}/export` | 导出：默认仅 active 且 adopted/edited_adopted；同步小批量直接返回 zip，超阈值转异步任务并返回任务查询句柄（导出前 hash 校验，R34） |

### 5.5 检索调试

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tasks/{id}/traces` | 按阶段查检索轨迹（分页，支持 stage/version 过滤） |
| GET | `/traces/{id}` | 单条轨迹详情（候选/裁剪原因/注入/引用闭环/降级记录） |
| GET | `/tasks/{id}/snapshots` | 快照元数据列表（node/batch/items 概要/tokens/usage/model_ref） |
| GET | `/snapshots/{id}` | 单快照详情（meta：items 含偏移、latencies、usage） |
| GET | `/snapshots/{id}/items/{position}` | full 模式按偏移读取某条注入知识全文（R13） |
| POST | `/workspaces/{id}/retrieval/playground` | ad-hoc 检索试验：给定 query/阶段配置/top-K，只走只读检索子图并回显漏斗，不落任务数据（Epic 6 调优，R17） |

### 5.6 配置与知识库

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | `/config/model` | 模型配置（平台全局；PUT 后可 POST `/config/model/test` 探活：一次最小调用） |
| POST | `/config/model/test` | 模型连通性测试 |
| GET | `/workspaces/{id}/kb/tree` | 该工作区知识库的链路/故事索引树（只读） |
| POST | `/kb/proposals` | **知识库写入申请**：仅生成 pending 提案与一次性确认令牌，不落库 |
| GET | `/workspaces/{id}/kb/proposals` | 提案列表（状态过滤） |
| POST | `/kb/proposals/{id}/confirm` | 显式确认 + 幂等键 + 一次性令牌校验后调 ReMeWriter；该端点不接受任何图运行内调用，重复确认返回首次结果或 409 |

### 5.7 SSE 事件类型

```
node_start / node_end          # {node, stage_version, batch_id}
batch_progress                 # {node, batch_id, done, total}
retrieval_summary              # {stage, batch_id, query_count, candidate_count, injected_count, degraded[]}
context_assembled              # {node, batch_id, item_count, tokens_est, budget, truncated}
budget_warning                 # {node, tokens_est, budget}
llm_token                      # {node, batch_id, chunk}
checkpoint_waiting             # {stage, artifact_id, stage_version}
clarification_needed           # {questions[]}
case_generated                 # {case_id, title, file_path, batch_id}
coverage_ready                 # {matrix_summary}
task_done / task_error         # task_error 复用 5.0 错误码模型
```

---

## 6. 模块划分与目录结构

```
testerAgent/
├── docs/                        # PRD、本文档
├── scripts/
│   └── dev.sh                   # 一键本地启动（后端 + 前端构建）
├── server/
│   ├── main.py                  # FastAPI 入口，托管 web/dist（含 SPA fallback、/healthz）
│   ├── memory/                  # L4：嵌入式 ReMe（pool/manager/tools/auto_memory）
│   ├── runtime/                 # 执行模型（挂 L3）
│   │   ├── runner.py            # Runner：互斥锁/心跳/取消/后台执行
│   │   ├── bus.py               # EventBus：进程内 pub/sub
│   │   ├── registry.py          # TaskRegistry + Reaper
│   │   ├── chat_agent.py        # 对话 tool_agent + memory_search
│   │   └── maintenance.py       # 对账/保留期清理等惰性任务
│   ├── api/                     # L2：REST + SSE 路由
│   │   ├── workspaces.py  agents.py  conversations.py  tasks.py
│   │   ├── cases.py  kb.py  config.py  traces.py  snapshots.py
│   ├── graph/                   # L3：LangGraph 控制环 + 能力 + 检索管线
│   │   ├── registry.py / main_graph.py / control/ / nodes/ / retrieval/
│   │   └── state.py
│   ├── eval/                    # 最小离线评测管线（黄金集回归）
│   ├── adapters/                # L5：协议适配
│   │   ├── reme.py              # Protocol / Caps / Factory / Writer Protocol
│   │   ├── reme_sdk.py          # SdkReMeReader / SdkReMeWriter / PoolRoutingWriter
│   │   ├── llm.py               # DeepSeek 客户端：超时/重试/限流/结构化修复
│   │   └── exporter.py          # MD zip / Excel 汇总（hash 校验）
│   ├── store/                   # L6
│   │   ├── db.py                # SQLite 连接（WAL）与迁移
│   │   ├── models.py            # 3.1 各表 DAO（强制 workspace 过滤）
│   │   └── workspace_files.py   # 原子写入/版本目录/快照偏移读/对账
│   └── prompts/                 # 各节点提示词模板（可被 agent 配置覆盖）
├── web/
│   ├── src/
│   │   ├── pages/               # Chat / Workbench / StageConfirm /
│   │   │                        #   RetrievalDebug / Workspaces / Settings
│   │   ├── components/          # MdViewer / MdEditor / CoverageMatrix /
│   │   │                        #   TraceTree / DiffList
│   │   └── api/                 # fetch 封装（错误模型/分页）+ SSE 客户端（Last-Event-ID）
│   └── package.json
└── data/                        # workspaces/{id}/reme/ + {task_id}/…（见 3.3）
```

迁移与运维约定（R35）：DB schema 变更走带 `schema_version` 的轻量 DDL 迁移（一期不引 alembic）；`data/` 根目录可由环境变量配置；`/healthz` 探活并报告 DB、ReMe、模型三项依赖状态；SPA history 路由统一 fallback 到 index.html。本地服务不设鉴权（单机单用户），监听地址默认仅绑定 127.0.0.1；ReMe 返回内容作为数据注入 prompt（不作为指令执行），提示词中明确知识块引用边界。

---

## 7. 关键技术决策汇总

| # | 决策 | 理由 | 对应 PRD |
|---|---|---|---|
| D1 | LangGraph checkpointer 用 SQLite（WAL）实现 | 与主库同栈，零额外服务 | US8.2 |
| D2 | 回退走应用层重跑（派生 run）而非图时间旅行；回退事务内含影响面分析与 checkpoint 切换 | 回退必伴随输入修改；两套状态显式协调，杜绝残留 | US8.3 / R2/R7 |
| D3 | 用例正文存文件系统，DB 存元数据；先文件后 DB + 原子 rename + Reconciler 对账 | MD 事实源、便于导出；跨源失败可收敛 | Q6 / R1 |
| D4 | ReMe 读/写拆成两个类，写类仅在确认端点 import 路径上，提案+一次性令牌+幂等键 | 结构上杜绝 agent 擅自写库 | 7 / US1.3 |
| D5 | 检索子图独立、阶段化配置、全程留痕 | 检索调试面板与归因指标的数据基础 | 6.1 / Epic 6 |
| D6 | 阶段间只传结构化产物，不传对话历史/知识正文/需求原文 | 长程任务上下文不膨胀，checkpoint 不复制大字段 | 6.1 / R11 |
| D7 | DeepSeek 走 langchain-openai 兼容客户端 | 协议兼容，换模型成本低 | 7 |
| D8 | 前端构建产物由 FastAPI StaticFiles 托管（单 worker） | 单机一键部署 | 7 |
| D9 | 上下文监控自研，不接 LangSmith | 避免业务数据出第三方；数据直接进调试面板 | 6.1 / Q8 |
| D10 | 快照三级开关；full 时全文 JSONL 落盘、DB 存元数据与字节偏移，流式写、按需读 | 零磁盘成本与可回放兼得 | 4.4 / 用户决策 |
| D11 | 工作区一等实体；agent↔workspace 多对多；阶段标识字符串化 + 图按 agent_type 注册 | 业务隔离；多智能体预留落在数据层与执行注册层，不耦合表结构 | Epic 7 / Q7 / R37 |
| D12 | 单进程单 worker + 进程内 Runner/EventBus + 心跳/Reaper，不引分布式队列 | 一期部署约束内解决并发与重启语义，复杂度最低 | 7 / R6/R18/R19 |
| D13 | 节点内以批次为最小持久化/取消/重试单元（游标 + 幂等键） | 长节点崩溃不丢全部进度，取消延迟可控 | US8.2 / R3 |
| D14 | 外部调用统一韧性契约：超时/退避重试/限流/结构化修复；检索每步有显式降级链 | 长程任务高频故障源前置收敛；ReMe 能力缺口不阻断开工 | R4/R9/R20 |
| D15 | 事件先落 task_event 表再广播，SSE 以 Last-Event-ID 补发 | 断线/重启/多标签页可靠回放 | US6 / R12 |
| D16 | 一期内置最小离线 eval 管线（复用图组件，黄金集冒烟） | 上下文精准策略的任何调优需可度量、可回归 | 2.2 / Q4 / R5 |
| D17 | 内置工具走 LangChain Tool + LangGraph ToolNode 子图；不替换用例主图 | 与 harness 语义对齐；对话/产线共享运行时；ReMeWriter 不可达 | 内置工具规格 |
| D18 | 同进程嵌入 ReMe（一工作区一实例）；废除 HTTP service；对话仅 `memory_search` | 与 QwenPaw 对齐；单 worker + vault 隔离；PRD 7 写门禁不变 | 7 / US1.3 |

---

## 8. 待验证项（spike）与一期结论

| # | 事项 | 状态 | 一期结论 |
|---|---|---|---|
| S1 | ReMe 三能力（metadata_filter / entry_version / passage_api）与接入模式 | **done（修订）** | **一期默认同进程嵌入** `reme.ReMe`（见 [reme-memory 设计](superpowers/specs/2026-09-28-reme-memory-module-design.md)）；HTTP `service` 已删除；caps 仍 `(False, False, True)` + IndexMirror / `local_entry_version`；handoff [Reme-Memory] 取代 WP-09 HTTP 交付 |
| S2 | langgraph interrupt / Command(resume) / 派生 thread | **done (GO)** | 走图原生路径；`run_from_stage` 仅预留接口；见 handoff [SP-2] |
| S3 | 需求最大体量下 intake 实测 | **deferred** | 一期未做真实超大文档实测。已落地：条款化 + 按需读原文；link_identify 需求摘要 N=500 占位。超大需求章节分批确认 → 二期运维标定 |
| S4 | 索引摘要体量 / 200 条硬上限 | **partial** | 硬上限 + `budget_warning` 已落地；未做真实索引体量实测。超限二级索引策略 → 二期 |
| S5 | 检索成本标定（20/40 测试点） | **deferred** | 初值发布：`batch_size=5`、`llm_concurrency=4`、runtime_config 预算表、规则分 `type_weights=1.0`。运维期用真实流量标定后固化；Q9 仍开放 |
| S6 | ReMeWriter 幂等与失败语义 | **done（预案即正式）** | 提案侧 Idempotency-Key + 令牌门禁；写后 read 回查；`verified=False` → `needs_manual_check`；写失败保持 pending 可同键重试 |
| S7 | 黄金集可获得性 | **done（基线）** | 无真实黄金集；eval smoke 用 3~5 组手工 fixture + FakeReader/FakeLLM；指标仅作回归基线，真实集到位后替换 |

## 9. 与开放问题的衔接

| PRD 开放问题 | 本文档处理（v0.3 / WP-X2） |
|---|---|
| Q1 用例 MD 模板 | **closed**：detailed-design §4.2 v1 即为一期定稿 |
| Q2 导出细节 | **closed**：MD zip（INDEX + v 目录）；Excel/CMS → 二期 |
| Q3 采纳率口径 | 基数已定（active 最新评审）；折算细则仍开放 |
| Q4 黄金集 | 见 S7；D16 最小 eval 管线已落地 |
| Q6 持久化范围 | **closed**：D3 / §3.3 |
| Q7 多智能体预留深度 | **closed**：数据模型 + 图注册预留；UI 一期不建 |
| Q8 模型合规 | 仍 open；127.0.0.1 绑定；不开快照外发 |
| Q9 耗时上限 | 仍 open；等 S5 运维标定 |
| Q11 知识库写入交互 | **closed**：§5.6 两阶段提案；CP1 写库为可选项 |
| Q12 ReMe md 结构约定 | **closed（适配层）**：SP-1/WP-09 映射表（path / bucket / `chain:*`） |

---

## 附录 A：评审意见处理记录

v0.1 于 2026-09-26 经过两轮架构评审，共 39 条。处理口径：**已纳入**（v0.2 正文落地）/ **部分纳入**（给出最小方案，余留跟踪）/ **暂缓**（一期接受风险或依赖开放问题）。

### A.1 高风险

| # | 问题 | 处理 | 落地位置 |
|---|---|---|---|
| R1 | DB↔文件双源无一致性协议，崩溃产生孤儿/悬空行 | 已纳入 | §3.3 协议、Reconciler、D3 |
| R2 | 回退时 stage_artifact 与 checkpoint 两套状态漂移 | 已纳入：派生 run + 同临界区初始化 + 旧 checkpoint 标记废弃 | §3.2②、D2 |
| R3 | 节点内无断点，长节点失败进度全丢、无重试幂等 | 已纳入：批次游标 + 边界 checkpoint + 幂等键 | §4.2③、D13 |
| R4 | 核心召回能力押在未验证的 ReMe 能力上，无 fallback | 已纳入：S1 前置 + 索引摘要镜像降级链 | §4.3④、S1、D14 |
| R5 | 缺离线评测闭环，调优不可度量 | 已纳入：最小 eval 管线 | §4.5、D16、S7 |
| R6 | run 重入/状态并发无防护；SQLite 锁；有 aborted 无取消 | 已纳入：互斥锁/状态守卫/WAL/协作式 cancel/cancel API | §4.5、§5.3、D12 |

### A.2 中风险

| # | 问题 | 处理 | 落地位置 |
|---|---|---|---|
| R7 | 回退一刀切作废下游，评审成果丢失 | 已纳入：影响面分析 + unaffected 继承 | §3.2② |
| R8 | 局部重生成绕过主图，版本/溯源裂缝 | 已纳入：同节点函数同写入路径，lineage 挂血缘 | §4.2④ |
| R9 | multi_query/rerank/extract 成本与重复调用无缓存 | 已纳入：run 内版本化 LRU + 降级 + S5 标定 | §4.3④⑤、S5 |
| R10 | 引用闭环信号弱（假阳/假阴污染归因） | 部分纳入：ID 白名单 + 幻觉识别 + 重叠度标记；不做语义裁判 | §4.3⑥ |
| R11 | 缺 message/kb_proposal 表；requirement 入 checkpoint 膨胀；无索引规划 | 已纳入：补三表（另加 task_event）、需求文件化、索引清单 | §3.1、§4.1 |
| R12 | SSE 无持久化/续传 | 已纳入：task_event + Last-Event-ID | §4.5、§5.0、D15 |
| R13 | context_snapshot 无读取接口 | 已纳入：snapshots 三组端点 | §5.5 |
| R14 | 无统一错误模型 | 已纳入：错误码分段 + retryable | §5.0 |
| R15 | 列表接口无分页 | 已纳入：游标分页统一约定 | §5.0 |
| R16 | confirm/编辑/提案确认缺版本校验与幂等 | 已纳入：If-Match、expected_version、幂等键 | §5.0、§5.6 |
| R17 | 缺 cancel/消息/连接测试/ad-hoc 检索/导出语义端点 | 已纳入 | §5.1~5.4 |
| R18 | Runner 生命周期与重启僵尸任务未定义 | 已纳入：心跳 + Reaper + 单 worker 约束 | §2.1、§4.5、D12 |
| R19 | run 与 events 之间缺事件总线组件 | 已纳入：EventBus（落库后广播） | §4.5、D15 |
| R20 | LLMClient 无超时/重试/限流/结构化修复 | 已纳入：韧性契约表 | §4.5、D14 |
| R21 | 配置运行中变更语义缺失；快照不记模型版本 | 已纳入：run/批次冻结 + model_ref | §3.1、§4.5 |
| R22 | link_identify "全量索引"无上限，随数据量退化 | 已纳入：200 条硬上限 + 告警 + S4 | §4.3、S4 |
| R23 | 跨批用例重复、知识矛盾无处理 | 部分纳入：查重合并；矛盾仅标注留痕不裁决 | §4.3⑤ |
| R24 | 长任务跨阶段知识视图不一致，trace 无条目版本 | 已纳入：entry_version + 更新提示 | §4.3、S1 |

### A.3 低风险与设计空白

| # | 问题 | 处理 | 落地位置 |
|---|---|---|---|
| R25 | position 偏差只观测不处理 | 部分纳入：首尾锚点排序 + 策略版本入快照 | §4.3 |
| R26 | 需求条款 ID 未定义，覆盖矩阵/溯源无锚点 | 已纳入：intake clause_id 规则与稳定性 | §4.2① |
| R27 | 文件命名不支持一点多用例 | 已纳入：case_id 主名 + front-matter | §3.3 |
| R28 | 检查点用户修改与 stage_version 关系不清 | 已纳入：origin + 连续占号 | §3.1、§3.2⑤ |
| R29 | review_status 转换规则与统计口径缺失 | 已纳入：转换规则 + active 统计口径 | §3.2④ |
| R30 | 挂起无超时/陈旧化提示 | 已纳入：7 天阈值 + 变更提示 | §3.2⑥ |
| R31 | 工作区删除级联、快照/事件无保留期 | 已纳入：软删除 + 惰性清理 + 30 天保留 | §3.3、§3.4 |
| R32 | 澄清提问只挂 intake | 已纳入：任意节点可挂 waiting_input | §4.2② |
| R33 | PRD"对话驱动调整"无通道 | 已纳入：change_request 消息 → 续跑/回退判定 | §4.2②、§5.2 |
| R34 | 导出筛选口径未定、大批量阻塞 | 已纳入：active+采纳态、hash 校验、超阈异步 | §5.4 |
| R35 | 迁移/备份/SPA fallback/健康检查缺失 | 部分纳入：schema_version 迁移、/healthz、SPA fallback；备份随 data/ 目录复制即可（一期文档说明），不做内建备份 | §6 |
| R36 | 本地无鉴权、知识内容提示注入面 | 部分纳入：默认绑 127.0.0.1 + prompt 数据边界声明；不做鉴权（一期单机） | §6 |
| R37 | 多智能体预留泄漏到写死枚举与固定主图 | 已纳入：阶段字符串 + 图注册表 | §3.4、§4.2⑥、D11 |
| R38 | 快照"按偏移读单行"但 items 无偏移字段 | 已纳入：char_offset/byte_length | §3.1、§4.4 |
| R39 | 无算子耗时/错误率观测与失败重试 UX | 已纳入：latencies、candidates.latency_ms/error、degraded、error_info retryable | §3.1、§4.4、§5.0 |
