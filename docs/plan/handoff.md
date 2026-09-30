# WP 状态表与交接单

| 项 | 内容 |
|---|---|
| 版本 | v1.1 |
| 日期 | 2026-09-28 |
| 配套 | [work-breakdown.md](work-breakdown.md) |

> 本文件是跨会话的唯一进度事实源：① 状态表（一眼看清能开哪些包）；② 每包交接记录（下个会话的"记忆"）。每个 WP 收尾必须同时更新两处。

## 0. 范式切换（2026-09-28）

用例智能体主图已从固定五阶段流水线切换为 **Plan-Execute + Reflexion + 子任务评审**（控制环）。

| 项 | 路径 |
|---|---|
| 设计 | `docs/superpowers/specs/2026-09-28-plan-execute-reflexion-design.md` |
| 实现计划 | `docs/superpowers/plans/2026-09-28-plan-execute-reflexion.md` |
| 状态 | Task 1–11 已落地主干；**遗留五阶段拓扑已删除**——生产仅控制环；回退走 `start_run_from_plan`；confirm 统一 `gate_kind` 路径 |
| 后续 | 将 capability 接到真实节点函数与 TaskContext；完善 ReviewProposal 前端卡片；同步 detailed-design 全量契约章节 |

### 0.1 嵌入式 ReMe 记忆模块（2026-09-28）

| 项 | 路径 |
|---|---|
| 设计 | `docs/superpowers/specs/2026-09-28-reme-memory-module-design.md` |
| 实现计划 | `docs/superpowers/plans/2026-09-28-reme-memory-module.md` |
| 状态 | M1–M4 done：同进程嵌入、删 HTTP、`memory_search` + 可选 `auto_memory`；KB 写仍仅 confirm |
| 交接 | §3 记录区 [Reme-Memory] |

## 1. 状态约定

- `todo` 未开始 ｜ `doing` 进行中 ｜ `done` 已完成且验收通过 ｜ `blocked` 被外部依赖阻塞 ｜ `needs-design` 发现设计缺口待裁决
- done 的判定：验收列场景通过 + 测试已落库 + 本交接单已填写。
- doing 超过单次会话未完成：在交接记录写"已完成切片/剩余切片"，允许保留 doing 由下个会话续做。

## 2. 状态总表

### β Spike（最高优先，阻塞关键路径）

| 编号 | 名称 | 状态 | 完成日期 | 交接记录锚点 |
|---|---|---|---|---|
| SP-1 | ReMe 三能力探测 | done | 2026-09-28 | §3 记录区 [SP-1] |
| SP-2 | LangGraph interrupt/派生 thread 验证 | done | 2026-09-27 | §3 记录区 [SP-2] |

### α 后端主线

| 编号 | 名称 | 体量 | 状态 | 完成日期 | 交接记录锚点 |
|---|---|---|---|---|---|
| WP-01 | 工程骨架 | S | done | 2026-09-26 | §3 记录区 [WP-01] |
| WP-02 | DB 与迁移 | S | done | 2026-09-26 | §3 记录区 [WP-02] |
| WP-03 | DAO 核心（task/artifact/testcase） | M | done | 2026-09-26 | §3 记录区 [WP-03] |
| WP-04 | DAO 其余 + 分页 | M | done | 2026-09-26 | §3 记录区 [WP-04] |
| WP-05 | FileStore 与 MD/快照文件 | M | done | 2026-09-26 | §3 记录区 [WP-05] |
| WP-06 | 异常体系与错误信封 | S | done | 2026-09-26 | §3 记录区 [WP-06] |
| WP-07 | LLMClient 韧性 | M | done | 2026-09-27 | §3 记录区 [WP-07] |
| WP-08 | ReMe 契约/FakeReader/IndexMirror | M | done | 2026-09-27 | §3 记录区 [WP-08] |
| WP-09 | ReMe 真实适配 | M | done | 2026-09-28 | §3 记录区 [WP-09] |
| WP-10 | 检索算子 A | M | done | 2026-09-27 | §3 记录区 [WP-10] |
| WP-11 | 检索算子 B + 缓存 | M | done | 2026-09-27 | §3 记录区 [WP-11] |
| WP-12 | retrieve_pipeline/trace/snapshot | M | done | 2026-09-27 | §3 记录区 [WP-12] |
| WP-13 | eval smoke 管线 | S | done | 2026-09-27 | §3 记录区 [WP-13] |
| WP-14 | prompts 包 | S | done | 2026-09-27 | §3 记录区 [WP-14] |
| WP-15 | build_graph/wrap/gate | M | done | 2026-09-27 | §3 记录区 [WP-15] |
| WP-16 | intake 条款切分 | M | done | 2026-09-27 | §3 记录区 [WP-16] |
| WP-17 | link_identify | S | done | 2026-09-27 | §3 记录区 [WP-17] |
| WP-18 | 批次执行器 + point_write | M | done | 2026-09-27 | §3 记录区 [WP-18] |
| WP-19 | case_generate + 用例批次提交协议 | M | done | 2026-09-27 | §3 记录区 [WP-19] |
| WP-20 | coverage_check | S | done | 2026-09-27 | §3 记录区 [WP-20] |
| WP-21 | EventBus + SSE | M | done | 2026-09-27 | §3 记录区 [WP-21] |
| WP-22 | Registry + Runner | M | done | 2026-09-27 | §3 记录区 [WP-22] |
| WP-23 | Reaper + 启停序列 | M | done | 2026-09-27 | §3 记录区 [WP-23] |
| WP-24 | 回退协议 | L | done | 2026-09-27 | §3 记录区 [WP-24] |
| WP-25 | API-A 工作区/会话/配置 | M | done | 2026-09-27 | §3 记录区 [WP-25] |
| WP-26 | API-B 任务/确认/取消 | M | done | 2026-09-27 | §3 记录区 [WP-26] |
| WP-27 | API-C 用例/导出 | M | done | 2026-09-28 | §3 记录区 [WP-27] |
| WP-28 | API-D 调试/知识库提案 | M | done | 2026-09-28 | §3 记录区 [WP-28] |
| WP-29 | Reconciler/幂等/清理 | S | done | 2026-09-28 | §3 记录区 [WP-29] |

### γ 前端线

| 编号 | 名称 | 体量 | 状态 | 完成日期 | 交接记录锚点 |
|---|---|---|---|---|---|
| WP-F0 | 脚手架/api client/sse.ts | S | done | 2026-09-28 | §3 记录区 [WP-F0] |
| WP-F1 | ChatPage | M | done | 2026-09-28 | §3 记录区 [WP-F1] |
| WP-F2 | StageConfirmPage | M | done | 2026-09-28 | §3 记录区 [WP-F2] |
| WP-F3 | WorkbenchPage | L | done | 2026-09-28 | §3 记录区 [WP-F3] |
| WP-F4 | RetrievalDebugPage | M | done | 2026-09-28 | §3 记录区 [WP-F4] |
| WP-F5 | Workspaces/Settings + 抛光 | S | done | 2026-09-28 | §3 记录区 [WP-F5] |

### δ 收尾

| 编号 | 名称 | 状态 | 完成日期 | 交接记录锚点 |
|---|---|---|---|---|
| WP-X1 | E2E + 场景 8/9 | done | 2026-09-28 | §3 记录区 [WP-X1] |
| WP-X2 | 收尾发布与文档回灌 | done | 2026-09-28 | §3 记录区 [WP-X2] |

### ε 上下文管理层（context，设计 2026-09-28）

| 编号 | 名称 | 状态 | 完成日期 | 交接记录锚点 |
|---|---|---|---|---|
| WP-30 | context 纯函数层（models/tokens/budget/journal/store/policy/scopes/assembler/p0/registry） | done | 2026-09-28 | §3 记录区 [WP-30] |
| WP-31 | 消费面接线（chat_agent / tool_agent hook / batch-item scope / methodology.md，feature flag 回退） | done | 2026-09-28 | §3 记录区 [WP-31] |
| WP-32 | 控制环/持久化/验收（005 journal + rebuild + T1 + 调试 API + 场景 13 + 门禁 + eval） | done | 2026-09-29 | §3 记录区 [WP-32] |
| WP-33 | 运行时干预（intervention 执行器 + commands API + context_* 工具/斜杠） | done | 2026-09-29 | §3 记录区 [WP-33] |

## 3. 交接记录（按完成顺序倒序追加，最新在最上）

<!-- 记录区开始：新记录插入到本行下方 -->

### [WP-33] context 运行时干预 — done（2026-09-29）

- 状态：done（设计 `docs/superpowers/specs/2026-09-28-context-management-design.md` §14；计划 Wave D Tasks 17–19）
- 交付物：
  - **Task 17**：`context/intervention.py`（selector 六语法 id/kind/step/recent/item/batch/all；pin/unpin/forget/refresh/set_goal/budget/freeze/unfreeze/show；P0→422、pinned forget→CONFIRM_REQUIRED、终态 `store.closed`→409；journal `reason=manual:*`）；store 增 `closed` / `budget_overrides` / `reactivate` / `note_policy`；journal 增 REFRESH/POLICY；assembler 优先取 store.budget_overrides
  - **Task 18**：`api/context.py` 扩展 `POST /tasks|conversations/{id}/context/commands`（Idempotency-Key 重放、跨 ws 404、completed 写 409/show 只读、SSE `context_command_executed` + `context_policy_changed`）
  - **Task 19**：`tools/context_tools.py`（context_pin/unpin/forget/show/set_goal/budget；forget 多命中返回候选不执行）；`ToolBuildContext.context_store` + `context.intervention.enabled` 开关注册；`chat_agent.try_slash_context_command`（`/context <verb> …` 直通执行器）
  - 测试：`tests/test_context_intervention.py`（22）
- 验收：
  - `pytest tests/test_context_intervention.py -p no:zframe` → **22 passed**
  - 全量 `pytest tests/ -p no:zframe` → **1041 passed**，1 失败为预存 flaky `test_files.py::test_cleanup_ignores_fresh_tmp_and_non_tmp`（mtime 时序，handoff 已登记与本包无关）；门禁 `test_context_gates` 全绿
- 与设计偏离：
  ① `recall` 指令一期仅返回 deferred（retrieve_pipeline 未在执行器内联）；
  ② journal 动作仍用 typed pin/demote/refresh/policy，人工语义落在 `reason=manual:*`（便于 rebuild 重放）；
  ③ commands API `model_window` 兜底 128000（playground 同口径），未异步读 ConfigDAO.model_dict
- 下个包起步点：上下文管理层（WP-30～33）一期闭环完成。可选后续：前端调试页（γ 线）、recall 接 retrieve_pipeline、registry shutdown 钩子

### [WP-32] context 控制环 / 持久化 / 验收 — done（2026-09-29）

- 状态：done（设计 `docs/superpowers/specs/2026-09-28-context-management-design.md`；计划 Wave C Tasks 12–16）
- 交付物：
  - **Task 12**：`store/migrations/005_context_journal.sql` + `ContextJournalDAO`；`DEFAULT_RUNTIME_CONFIG` 增 `context.*`；Runner 注入 task `context_store`；测试 `test_migration_005_context.py` / `test_context_config.py`
  - **Task 13**：`context/rebuild.py`（artifact/message + journal 重放）；chat 过渡回填改为 registry.restore；测试 `test_context_rebuild.py`
  - **Task 14**：`graph/control/context_t1.py` + wrap phase 作用域；confirm pin / outline SHARED；测试 `test_context_control_graph.py` / `test_context_wrap_scope.py`
  - **Task 15**：`api/context.py` 调试视图/evictions/playground + SSE 事件名；测试 `test_context_api.py`
  - **Task 16**：场景 13 `tests/test_scenario_13_context.py`（200 chat + 12-step replan×2/repair×3/review×2 + 100 条批次，§10.2 六项 + O(1) + 阶段隔离）；门禁 `test_context_gates.py`（叶子反向依赖区分 `from .store` vs `from ..store`；ReMeWriter 零引用；12a 不回退）；eval `05_context_pressure.json` + `context.enabled` config-diff 护栏（reference_rate ≤-5pt / clause_coverage 不降）
- 验收：
  - 全量 `cd server && python -m pytest tests/ -p no:zframe --tb=no -q` → **1020 passed**（基线 WP-31=966，本包约 +54）
  - Task 16 子集 `test_scenario_13_context` / `test_context_gates` / `test_eval`（含 context 护栏）全绿
  - `-W error`：收集期撞既有 anyio `BlockingPortal` DeprecationWarning（非本包引入；与 WP-31 登记的 sqlite unraisable flaky 同属环境噪声），业务用例无失败
- 与设计偏离：
  ① 场景 13 O(1) 对照对 CASE_ITEM 使用 `persist_evictions=False`，避免第 1 条装配 demote 污染第 100 条墓碑窗口；
  ② eval `context.enabled` 为对照标记键，不写入 RetrievalConfig（检索面字节不变，护栏看 reference_rate/clause_coverage）；
  ③ 门禁 AST 扫描显式放过 level=1 相对 import（`context.store`），只禁 level≥2 / 绝对 `tester_agent.store` 等外层包
- 下个包起步点：**WP-33** Task 17——`context/intervention.py` selector 六语法 + pin/unpin/forget/refresh/set_goal/budget/freeze；读 design §14

### [WP-31] context 消费面接线 — done（2026-09-28）

- 状态：done（设计/计划同 WP-30；范围计划 Wave B Tasks 8–11）
- 交付物：
  - **Task 8 组合根接线位**：[runtime/context.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/context.py) `AppContext.context_registry`（default_factory 独立实例，可注入）、`DAOs.journal`（占位字段）、`TaskContext.context_store`（默认 None）；[context/registry.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/context/registry.py) 重写为 `ContextRegistry` 实例类（模块级默认实例与函数委托保留，WP-30 路径不变）；测试 `tests/test_context_wiring.py`（6）
  - **Task 9 对话历史窗**：[runtime/chat_agent.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/chat_agent.py) 按 `context.enabled` 分派——context 路径 owner(conversation) store 首次从 MessageDAO 过渡回填 CHAT_TURN（role + turn_seq）、`bootstrap_p0(methodology + extra_static persona)`、`scope(turn_seq=)` 内 `assemble(CHAT)`、`run_tool_agent(initial_messages=, before_model_hook=)`，assistant/异常文本落库后同 turn_seq append；`_run_legacy` 保持 40 条旧路径；新增 [prompts/methodology.md](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/prompts/methodology.md)（v2026-09-28.1，等价类/边界值/判定表/场景法）；[api/conversations.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/conversations.py) 透传 `app_ctx.context_registry`；测试 `tests/test_context_chat.py`（7）
  - **Task 10 工具环 hook**：[graph/tool_agent.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/tool_agent.py) `build_tool_agent_graph` 与 `run_tool_agent` 增默认 None 的 `before_model_hook`，并支持 `initial_messages`（history/system_prompt 改可选）；新增 [graph/context_hooks.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/context_hooks.py) `make_tool_message_hook`：新 ToolMessage 登记 P2 TOOL_RESULT，超 2000 字符者 demote(budget_cut) 并以 tombstone SystemMessage 替换模型输入位；只改入参不回写 state；测试 `tests/test_context_tool_agent.py`（5）
  - **Task 11 批次/条目隔离**：[graph/batch.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/batch.py) worker 包 `scope(phase=WRITE, batch_id=)`、批次成功后 `close_batch`（context_store None 时全跳过）；[graph/nodes/case_generate.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/nodes/case_generate.py) 每点 push `item_key={batch_id}:{point_id}` 并 append ITEM 级 ARTIFACT_DIGEST（确定性 entry_id，repair 幂等）；`context.case_index_digest` 默认关、开时向本批 LLM 注入一行已生成用例索引；`test_context_scoping.py` +3、`test_case_generate.py` +2
- 验收：
  - 全量 `cd server && ../.venv/bin/python -m pytest tests/ -p no:zframe --tb=no` → **966 passed**（基线 943 + 新增 23：wiring 6 / chat 7 / tool_agent 5 / scoping +3 / case_generate +2）
  - 全量 `-W error`：唯一失败仍为 WP-30 已登记预存 flaky——Python 3.14 未关闭 sqlite 连接 GC 时序 `ExceptionGroup: multiple unraisable exception warnings (2 sub-exceptions)`；加入新文件后归因在测试间漂移（本次落在 test_context_tool_agent::test_consecutive_tool_calls_all_replaced 与 test_context_scoping::test_item_assembly_*），两个文件隔离 `-W error` 运行均全绿；966 用例无业务回归
  - 反向依赖：新增 hook 放 graph 层（graph → context 合法），context 包零新外层 import
- 与设计偏离（同时登记于代码 docstring）：
  ① `AppContext.context_registry` 字段为 dd 字段表未列项（默认工厂持有独立实例；WP-30 registry 已重构为实例类，偏离登记在 registry.py docstring）；
  ② `DAOs.journal` 本包仅落 `Any | None` 占位字段——具体 `ContextJournalDAO` 随 005 迁移在 WP-32 Task 12 落型；
  ③ model_config 无 `context_window` 既有约定键，组装时 `.get("context_window", 128000)` 兜底；
  ④ P0 形态：methodology 走模板，会话 persona（含记忆指引）经 `extra_static` 注入，未并入模板；
  ⑤ 首次会话从 MessageDAO 回填 CHAT_TURN 为过渡逻辑（代码内 TODO 指向 WP-32 Task 13 的 rebuild_store + journal 重放）；
  ⑥ case_index_digest 直接注入 case_generate 的 LLM messages（该节点不经 assemble），未落 ContextEntry；
  ⑦ 既有 `test_memory_tools.py::test_run_chat_turn_passes_memory_manager_to_tools` 显式置 `context.enabled=false` 走旧路径，断言意图（记忆指引入 system_prompt、manager 入 tools）不变
- 遗留：
  - Runner 尚未向 TaskContext 注入 context_store（待 WP-32 迁移/开关键落地后接线）；`DEFAULT_RUNTIME_CONFIG` 无 context.* 键（Task 12）；owner store 仅内存态、进程重启不恢复（Task 13 rebuild）；JournalSink 真实落库仍 pending
- 下个包起步点：**WP-32**。Task 12：① 新增迁移 005（context_journal 表，只增不改）；② 落 `ContextJournalDAO`（适配 JournalSink，替换 DAOs.journal 占位）；③ `DEFAULT_RUNTIME_CONFIG` 增加 context.* 键并接 Runner 注入 context_store。Task 13：rebuild_store（artifact/message 回放 + journal）替换 chat 过渡回填。第一步先读 design §8 与 store/db.py DEFAULT_RUNTIME_CONFIG 现状

### [WP-30] context 纯函数层 — done（2026-09-28）

- 状态：done（设计 `docs/superpowers/specs/2026-09-28-context-management-design.md` v0.5，计划 `docs/superpowers/plans/2026-09-28-context-management.md`）
- 交付物：
  - 新增叶子包 `server/tester_agent/context/`：`tokens.py`（自 ops_b 平移，ops_b 改 re-export）、`models.py`（5 枚举 + ContextEntry/EvictionRecord/AssemblyReport/ContextView，extra=forbid）、`budget.py`（6 profile 预算 + 10% spill capacity + 80% 窗口校验）、`journal.py`（JournalSink 协议/JournalRecord/8 action）、`store.py`（append 幂等+scope 继承、demote/evict/pin/set_goal/bulk_demote/close_batch/flush_degraded/frozen/bind_p0，journal 失败降级不阻断）、`policy.py`（打分 0.35R+0.30引用+0.20目标+0.15种类−重复、eviction_order、T3 漂移、tombstone 文案）、`scopes.py`（contextvars 作用域栈 async `scope()` + visible_in_window）、`assembler.py`（唯一组装入口四步管线 + AssemblyResult）、`p0.py`（bootstrap_p0：sha256[:12] 版本）、`registry.py`（owner 注册表，重复 start 幂等）、`_time.py`
  - 新增测试 90 个：`tests/test_context_{tokens(8),models(9),budget(9),store(15),policy(14),scoping(10),assembler(13),p0(7),registry(5)}.py`；`ops_b.py` 仅 import 改动，`test_retrieval_ops_b.py` 53 用例回归通过
- 验收：
  - `cd server && ../.venv/bin/python -m pytest tests/test_context_*.py tests/test_retrieval_ops_b.py -W error -q` 全绿（143 用例）
  - 全量 `pytest tests/ --tb=no -q` exit=0（共收集 920 用例）
  - 全量 `-W error`：context 用例 0 失败；残留失败为既有 Python 3.14 `ExceptionGroup: multiple unraisable exception warnings`（未关闭 sqlite 连接 GC 时序），落点随测试顺序漂移（本次落在 test_graph_build；排除 context 新文件后落在 test_llm/test_skeleton/test_tool_agent_graph，均为已登记 flaky 家族），单测隔离运行 3/3 通过
  - 反向依赖扫描：`context/` 内无 `runtime/graph/memory/adapters/store（外层包）` import（grep 复核；仅允许依赖 domain/errors/prompts/logging/第三方）
- 与设计偏离（不碰冻结签名）：
  ① `ContextEntry` 增加可选 `role` 字段（CHAT_TURN 的 user/assistant → HumanMessage/AIMessage，spec 模型表未列角色字段）；
  ② 裁剪阈值用 `ProfileBudget.capacity()`（本区+10% spill），仅超 capacity 才出窗；
  ③ T3 漂移条目一旦满足"非pinned+overlap=0+距离>W"即无条件 goal_drift 出窗（不要求预算先超限），预算未超也裁；
  ④ CHAT profile 最近 K=6 轮为**硬窗口**（reason=`chat_window`，spec 未命名该 reason），不依赖预算/漂移启发式；
  ⑤ 消息粒度：P1 合并为单条 `【业务知识】` SystemMessage（按挂载 step_seq 降序、`---` 分隔），P2 非对话条目逐条 SystemMessage，CHAT_TURN 按 (turn_seq,created_at) 渲染 Human/AI；
  ⑥ 当次新裁条目不在当次窗口渲染墓碑（demote 落 store，下次组装进入 tombstone 段，最多 20 行 + "其余 N 条已归档"）；
  ⑦ registry 为进程内 dict、无锁（当前调用面单事件循环）；scopes 仅提供 async context manager
- 遗留与提问：
  - JournalSink 的真实落地（DB/文件 sink）不在 WP-30，后续 WP 承接；registry 无 shutdown/生命周期清理钩子；
  - AST/import-linter 门禁**尚未配置**，叶子层约束当前靠 grep 人工保证，建议后续 WP 加 import-linter 契约固化；
  - `AssemblyReport` 的 P0 token 仅按传入消息估算；P0 超 p0 预算时不裁不记 override（P0 永不淘汰语义），如需观测请 WP-31 决定
- 下个包起步点：**WP-31 消费面接线**。① `runtime/chat_agent.py` 用 registry+assemble 替换 40 条硬编码历史（CHAT profile + role 字段 + K=6 硬窗口）；② `graph/tool_agent.py` 增默认 None 的 `before_model_hook`；③ `graph/batch.py`/`nodes/case_generate.py` push `scope(phase=WRITE, batch_id=, item_key=)`；④ 新增 `prompts/methodology.md` 并由 bootstrap_p0 引导、store.bind_p0；⑤ feature flag `context.enabled=false` 时旧行为完全可回退。第一步先读 design §8 接线序列与 runtime/chat_agent.py 现状

### [Reme-Memory] 嵌入式 ReMe 记忆模块 — done（2026-09-28）

- 状态：done（取代 WP-09 HTTP 路径与 SP-1「优先 service」结论）
- 交付物：
  - `memory/`：`reme_config` / `ReMeMemoryManager` / `WorkspaceMemoryPool` / `prompts` / `tools`（仅 `memory_search`）
  - `adapters/reme_sdk.py`：`SdkReMeReader` / `SdkReMeWriter` / `PoolRoutingWriter` / `register_sdk_builder`
  - **删除** `adapters/reme_http.py`、`tests/test_reme_http.py`；`kb_config` 拒绝 `mode=service`
  - Factory 缓存键 = `workspace_id`；vault = `data/workspaces/{id}/reme/`
  - `main.py` lifespan：pool + sdk builder + `PoolRoutingWriter`；shutdown `pool.shutdown_all`
  - chat：`run_chat_turn` 注入 pool → `memory_search` 工具 + guidance；`auto_memory_interval>0` 时异步 `auto_memory`
  - 依赖：`reme-ai>=0.4.1.5,<0.5`；web `KbConfig` 无 service 字段
- 验收：`pytest tests/test_memory_pool.py tests/test_memory_tools.py tests/test_reme_sdk.py tests/test_reme.py -q` 全绿；场景 12 import-linter 保持（graph/chat 不触达 Writer）
- 与设计偏离：无；CI 仍 mock `reme_ctor`/`FakeReMeApp`，真实嵌入冒烟非必跑
- 遗留：真实 `reme-ai` 本机冒烟；前端记忆浏览器 UI（一期非目标）
- 下个包起步点：按主线继续；联调时工作区填 `kb_id` + 可选 `options.memory_search_enabled` / `auto_memory_interval`

### [PE-cleanup] 删除遗留五阶段拓扑 — done（2026-09-28）

- 状态：done
- 交付物：
  - 删除 `build_legacy_stage_graph` / `legacy=` / 静态 `GATE_CP*`；生产仅控制环
  - 回退：`start_run_from_plan`（AgentPlan 入口 + `as_node=plan`）
  - confirm：统一 `gate_kind` 路径（含 expected_version / modify 落库）
  - Runner：仅函数式 interrupt 收口；`human_gates` 从 runtime_config 注入
  - 测试：`caps_from_stage_nodes` 注入假能力；intake/link 单测改 wrap 小图
- 验收：PE/图/回退/API-B/e2e 相关用例通过（全量约 850+；少量 CLI/migration 预存失败与本包无关）
- 遗留：coverage 自动补例改纯评审驱动；真实 LLM 联调

### [PE-followups] Plan-Execute 后续 1→2→4→3 — done（2026-09-28）

- 状态：done
- 交付物：
  - capability → 真实节点（`invoke_capability` + async `execute_step` + confirm 同步 link/point plan）
  - Session 内 `ReviewProposalCard`（review_decision）
  - e2e：`ConfirmIn.gate_kind` 默认 `plan_confirm`；`test_e2e_main` / 默认 gates 中断场景
  - **detailed-design v0.4** 契约对齐控制环；tech-design 指针更新
- 验收：相关 server/web 单测已绿（e2e_main、scenario_plan_execute、capability_wiring、ReviewProposalCard）
- 遗留：coverage 自动补例改纯评审驱动；真实 LLM 联调

### [WP-X2] 收尾发布与文档回灌 — done（2026-09-28）

- 状态：done
- 交付物：
  - backup CLI：`store/backup.py` + `cli backup <out_dir>`（SQLite online backup + workspaces/ 打包）；`tests/test_backup_cli.py`
  - 导出+提案+保留期发布门禁：`tests/test_release_x2.py`
  - **bugfix**：`FileStore.cleanup_exports` 误扫 `*/tasks/*`，改为 `workspaces/{ws}/{task}/exports`（与 `_task_dir` 对齐）；`test_maintenance_cleanup` 路径同步
  - 文档回灌：PRD v0.7（Q1/Q2/Q6/Q7/Q11/Q12 closed）、tech-design v0.3（S1~S7 结论）、detailed-design v0.3（Q1/Q2 定稿、§19.5 接线）
  - 根 `README.md` + `docs/plan/release-checklist.md`
- 验收：`pytest -p no:zframe tests/test_backup_cli.py tests/test_release_x2.py tests/test_maintenance_cleanup.py` **12 passed**
- 与设计偏离：① S3/S4/S5 **未做真实环境标定**（结论记 deferred/partial，初值发布）；② cli `reap` 仍占位（启动 lifespan 已跑 Reaper/对账/清理）；③ obsolete 用例 MD 物理删除仍未单独实现（`obsolete_cases_days` 现用于 `.tmp` 清理口径，与 WP-29 一致）
- 遗留：真实 ReMe/LLM 联调；Playwright 全主场景；runtime_config UI；Q3/Q8/Q9/Q10；S3~S5 运维标定
- 下个包起步点：**无后续 WP**——按 [release-checklist.md](release-checklist.md) 勾选后候选发布；联调/标定属运维迭代

### [WP-X1] E2E + 场景 8/9 — done（2026-09-28）

- 状态：done
- 交付物：
  - 场景 8/9：`server/tests/test_scenario_8_9.py`（镜像本地过滤 + rerank `llm_failed→rule_score`；`close_retrieval_trace` hallucinated / injected_not_used）
  - API E2E：`server/tests/test_e2e_main.py`（ASGI+假图节点：需求→CP1→CP2→评审→导出；回退继承；failed+progress→/run 续跑）
  - Playwright opt-in：`web/e2e/smoke.spec.ts` + `playwright.config.ts`；`npm run e2e`（不入 `npm test`/PR）；浏览器 MSW 合入 confirm/workbench/debug/settings；`public/mockServiceWorker.js`
- 验收：`pytest -p no:zframe tests/test_scenario_8_9.py tests/test_e2e_main.py` **5 passed**；前端 `npm test` **38 passed**；`npm run e2e` **3 passed**
- 与设计偏离：① **主路径/回退/崩溃 E2E 落在 ASGI API 层**（非 Playwright 打真后端；对齐已确认设计）；② Playwright 仅 UI smoke（壳导航+会话+设置+工作区），不覆盖真实 SSE/LLM；③ 场景 8 用 `link_identify`+链路索引条目（镜像只收录 LINK/STORY，业务条目进 scope 会被 `filtered_scope`，无法同时覆盖 rerank 降级）
- 遗留：真实 ReMe/LLM 联调；Playwright 全主场景；WP-X2 文档回灌；runtime_config UI
- 下个包起步点：**WP-X2 收尾发布与文档回灌**

### [WP-F5] Workspaces/Settings + 抛光 — done（2026-09-28）

- 状态：done
- 交付物：`api/domain.ts` KbConfig/KbTestOut/Agent/ModelConfig/ModelTestOut；`endpoints` workspace CRUD+kb/test、agents list/bind、model GET/PUT/test；`components/settings/CapsReadonly`；`pages/WorkspacesPage`（列表选中=当前会话区、表单、kb/test 能力位只读、智能体勾选绑定、删除 409）；`pages/SettingsPage`（保存后 model/test）；MSW `mocks/settingsHandlers.ts`；路由替换占位；Chat 无工作区引导链 `/workspaces`（不再静默建默认区）
- 验收：前端 `npm test` **38 passed**（F0~F4 + F5：kb/test 能力位三勾选 disabled+passage_api 勾选 / 创建工作区写 session+绑定智能体 / DELETE 活跃任务 ErrorBanner TASK_STATE_CONFLICT / model save→test latency+model）；`npm run build` 成功
- 与设计偏离：① **仍未引 TanStack Query**（与 F1~F4 一致）；② **智能体不可解绑**（后端无 unbind HTTP，已绑定 checkbox disabled）；③ **runtime_config 高级设置未做**（本包仅 model_config）；④ KB tree 仍未做（明确留给后续）
- 遗留：真实联调需嵌入式 ReMe（`kb_id`）+ 已保存 model；Chat 空工作区需先走工作区页；unbind API 若需要再补
- 下个包起步点：**WP-X1 E2E + 场景 8/9**（或真实后端联调走通主场景）

### [WP-F4] RetrievalDebugPage — done（2026-09-28）

- 状态：done
- 交付物：`api/domain.ts` Trace*/Snapshot*/Playground*/Funnel*；`listTraces`/`getTrace`/`listSnapshots`/`getSnapshot`/`getSnapshotItem`/`runPlayground`；`lib/funnel.ts`；`components/debug/{FunnelChart,TraceTree,SnapshotList,SnapshotItemDialog,Playground}`；`pages/RetrievalDebugPage.tsx`；MSW `mocks/debugHandlers.ts`；路由 `/debug`
- 验收：前端 `npm test` **34 passed**（含 F4：漏斗四段 4/3/2/2 + candidates drop_reason；full 快照弹窗正文 / meta 档黄标禁用；trace degraded 黄标；Playground 提交 funnel+degraded）；`npm run build` 成功
- 与设计偏离：① **漏斗用 CSS 条**（未引图表库）；② **仍未引 TanStack Query**；③ Playground overrides UI 未暴露（API 可传，本包固定空）；④ KB tree 未做（属 F5/后续）
- 遗留：真实联调需任务已跑出 traces/snapshots；meta/off 档全文 404 由 UI 禁点处理
- 下个包起步点：**WP-F5 Workspaces/Settings + 抛光**（kb/test 能力位只读、model/test；联调 WP-25）

### [WP-F3] WorkbenchPage — done（2026-09-28）

- 状态：done
- 交付物：`api/domain.ts` CaseSummary/CaseDetail/Review*；`listCases`/`getCase`/`updateCase`(If-Match)/`reviewCases`；`components/case/{CaseList,MdViewer,MdEditor,ReviewBar,AdoptionSummary,ConflictBanner}`；`lib/adoption.ts`；`pages/WorkbenchPage.tsx`（三栏+评审过滤/版本切换+编辑抽屉+冲突红条）；MSW `mocks/workbenchHandlers.ts`；路由 `/workbench`；依赖 `react-markdown`
- 验收：前端 `npm test` **29 passed**（F0~F2 + F3：If-Match 保存→edited_adopted / VERSION_CONFLICT 提示 expected·current 并刷新正文 / 批量 adopt 后采纳率 0/4→2/4）；`npm run build` 成功
- 与设计偏离：① **MdEditor 用 textarea**（未引 CodeMirror，A 范围够用）；② **仍未引 TanStack Query**（与 F1/F2 一致）；③ **VERSION_CONFLICT「diff」展示 expected/current hash**（后端 details 无正文 diff）；④ 导出/regenerate/覆盖矩阵按范围 A 未做
- 遗留：真实联调需任务已生成用例；file_missing 仅禁保存+提示，无后端重建入口；hash_conflict「覆盖文件」先 GET 再 PUT
- 下个包起步点：**WP-F4 RetrievalDebugPage**（漏斗/轨迹/快照偏移全文/Playground；联调 WP-28）

### [WP-F2] StageConfirmPage — done（2026-09-28）

- 状态：done
- 交付物：后端 `GET /api/v1/artifacts/{id}`（`api/tasks.py` ArtifactDetailOut，含完整 payload）；前端 `api/domain.ts` LinkPlan/PointPlan/ArtifactDetail；`getArtifact`/`confirmTask`；`components/confirm/{LinkPlanEditor,PointPlanEditor}`；`pages/StageConfirmPage.tsx`（左清单右详情、hit/new 分色、confidence<0.5 置灰、脏改 modify / 干净 confirm、VERSION_CONFLICT 刷新提示、CP2→链路 ImpactPreview 回退）；MSW `mocks/confirmHandlers.ts`；路由 `/confirm`
- 验收：后端 `pytest tests/test_api_artifact_get.py` **2 passed**；前端 `npm test` **25 passed**（F0/F1 + F2：干净 confirm / 脏改 modify+expected_version / VERSION_CONFLICT 提示并刷新）；`npm run build` 成功
- 与设计偏离：① **新增 GET /artifacts/{id}**（dd/tech-design 端点表未列；TaskOut.active_artifacts 无 payload，确认页必需）；② **hit=false 绑定 entry_id 用文本框**（完整 KB 树选择器留给后续）；③ **仍未引 TanStack Query**（与 F1 一致，显式 load）
- 遗留：真实联调需后端已起 + 任务停在 waiting_confirm；KB 树选择器 / 写库提案不在本包
- 下个包起步点：**WP-F3 WorkbenchPage**（用例列表/MD 渲染编辑/评审条/版本过滤；联调 WP-27）

### [WP-F1] ChatPage — done（2026-09-28）

- 状态：done
- 交付物：`api/domain.ts` + `api/endpoints.ts`（F1 所需 REST 子集）；Zustand `stores/session.ts` / `stores/taskStream.ts`（SSE→phase/checkpoint/clarification）；`lib/parseStageMention.ts`（@链路/@测试点）；组件 `chat/{RequirementInput,MessageList,ClarificationCard}` + `confirm/ImpactPreview`；`pages/ChatPage.tsx`（引导建工作区/会话→需求提交建任务自动 run→订阅→澄清卡 answer→checkpoint 横幅链确认页；change_request 二次确认后 rollback）；MSW `mocks/chatHandlers.ts` + `test/scriptedEventSource.ts`；路由 `/` 挂 ChatPage
- 验收：`npm test` **22 passed**（F0 14 + F1 新增：parseStageMention / taskStream 事件归一 / ChatPage 脚本事件→checkpoint_waiting 横幅、澄清卡提交、@链路 ImpactPreview→rollback）；`npm run build` 成功
- 与设计偏离：① **未引 TanStack Query**（F1 消息/任务用本地 state + 显式 refresh，F2/F3 列表页再引）；② **ImpactPreview 预确认为静态风险说明**（无独立 analyze_impact API，真实 summary 在 rollback 响应后展示）；③ 中文 @别名不用 `\b`（JS `\w` 不含汉字）
- 遗留：真实后端联调需 `VITE_ENABLE_MSW=0` + 后端已起且已有工作区；StageConfirmPage（F2）消费 checkpoint 横幅跳转；composer 的普通 chat 消息后端暂只落库不编排（WP-25 遗留）
- 下个包起步点：**WP-F2 StageConfirmPage**（Link/Point 清单编辑器 + expected_version confirm/modify + VERSION_CONFLICT；复用 ImpactPreview；联调 WP-24/26）

### [WP-F0] 脚手架/api client/sse.ts — done（2026-09-28）

- 状态：done
- 交付物：新建 `web/`（Vite + React 19 + TS + Tailwind v4 + react-router）；`api/client.ts`（错误信封解包/中文映射/分页/`Idempotency-Key`/NetworkError）；`api/sse.ts`（`after_event_id` + localStorage lastEventId + 退避 1/2/5…30s + 未知事件忽略）；`AppShell`（顶栏占位导航 + ErrorBanner + ReconnectBanner）；六页占位；dev-only `/_dev/f0` + MSW handlers；Vitest 14 例；dev 代理 `/api`→`:8080`
- 验收：`npm test` **14 passed**；`npm run build` 成功。覆盖：400/404/500 信封中文码、分页、幂等头、网络失败文案、SSE 退避与 after_event_id 续传、ErrorBanner 展示 `[VALIDATION_BODY]`、黄条「连接中断，重连中…」
- 与设计偏离：① **React 19**（dd §12.1 写 18，create-vite 默认 19，API 兼容）；② **未装 TanStack Query/Zustand**（F0 用最小 subscribe store；F1+ 再引）；③ 生产包不含 `/_dev/f0`（`import.meta.env.DEV` 守卫）
- 遗留：真实后端联调需 `VITE_ENABLE_MSW=0` + 后端已起；视觉体系留给后续页面 WP
- 下个包起步点：**WP-F1 ChatPage**（需求输入/建任务自动 run/澄清卡/change_request；联调 WP-26 已就绪）

### [WP-09] ReMe 真实适配 — done（2026-09-28）⚠️ **已被 [Reme-Memory] 取代**

> **修订（2026-09-28）**：HTTP `reme_http` / `mode=service` / `WorkspaceRoutingWriter` 已删除；生产路径改为同进程嵌入 + `reme_sdk` + `PoolRoutingWriter`。下文为历史交付记录，勿按此复现。

- 状态：done（依赖 SP-1 done）→ **superseded by [Reme-Memory]**
- 交付物：新建 [adapters/reme_http.py](file:///D:/code/github/testerAgent/server/tester_agent/adapters/reme_http.py)——`HttpReMeReader`（`POST /knowledge_search|/read|/frontmatter_read|/list`）、`HttpReMeWriter`（`/save_to_knowledge` + `/read` 回查）、`WorkspaceRoutingWriter`（按工作区 `kb_config` 路由）、`register_service_builder` / `bucket_to_entry_type` / `build_http_reader`（构造期 list 探活）；caps 固定 SP-1 `(metadata_filter=False, entry_version=False, passage_api=True)`；`entry_id`=相对 path；IndexTree 由 `signals` 中 `chain:*` 派生；[main.py](file:///D:/code/github/testerAgent/server/tester_agent/main.py) lifespan 注册 `service` builder + `kb_writer=WorkspaceRoutingWriter(db)`；[api/kb.py](file:///D:/code/github/testerAgent/server/tester_agent/api/kb.py) confirm 向 payload 注入 `_workspace_id`；新增 tests/test_reme_http.py（15 用例）
- 验收：`pytest tests/test_reme_http.py tests/test_reme.py tests/test_api_d.py tests/test_api_a.py` **133 passed**。覆盖 search 映射 / types·scope 不传远端 / 网络→KbUnreachable / get_entry frontmatter+chain / 缺失 404 / IndexTree 双 story / Factory probe caps / Writer 成功 verified + 拒绝 ok=False + 网络抛错；既有 Factory 未注册 sdk 行为与场景 12 import-linter 仍绿
- 与设计偏离：① **优先 service 而非 SDK**（SP-1）；② **sdk mode 本包不注册**（仍 400 VALIDATION_BODY）；③ IndexTree 无原生 API，靠 `chain:*` 派生（Q12 仍 open，缺标记→空树+镜像降级）；④ confirm 注入 `_workspace_id` 不扩 Writer Protocol 形参
- 遗留：未对真实 ReMe 进程做联通（需本机 `reme start config=business_kb` + kb/test）；`TestTaskAnswer`/`test_cleanup_ignores_fresh_tmp` 偶发失败与本包无关（时序/mtime）
- 下个包起步点：γ 前端 WP-F0，或联调真实 ReMe 探活回填 Q12

### [SP-1] ReMe 三能力探测 — done（2026-09-28）⚠️ **接入模式结论已修订**

> **修订（2026-09-28）**：一期改为**默认同进程嵌入**；HTTP service 适配已删除。caps 三能力位结论仍有效。见 [Reme-Memory] 与 [reme-memory 设计](../superpowers/specs/2026-09-28-reme-memory-module-design.md)。

- 探测源：`D:\code\github\ReMe` 分支 `feature/linchao`（只读源码；未起本地服务、未改生产代码）
- 接入模式结论（**历史**）：~~优先 `service`（HTTP），`sdk` 作可选二路~~ → **现行：仅嵌入式 SDK**
  - ~~HTTP：`HttpService`…~~（已删除）
  - SDK：同进程 `ReMe`/`Application` + `await app.run_job(name, **kwargs)`——**现行默认路径**；依赖 `reme-ai`，vault 按工作区隔离
  - kb_config 约定（现行）：`{kb_id, knowledge_dir?, create_knowledge_base?, options}`；拒绝 `mode=service`
- 三能力位结论（相对 dd §8.4）：

  | 能力 | 结论 | 依据 | WP-09 预案 |
  |---|---|---|---|
  | `metadata_filter` | **否**（相对 dd 语义） | `search`/`knowledge_search` 支持 `search_filter.prefixes`（→ bucket 路径前缀）与 path/日期；**默认** `include_frontmatter_in_metadata: false`，chunk 不带 frontmatter；**无** `types[]` / `scope={link_ids,story_ids}` 原生参数 | `caps.metadata_filter=False` → 启用 **IndexMirror 本地过滤**；类型可另做 bucket→EntryType 粗映射作可选增强，但不标能力位为真 |
  | `entry_version` | **弱有 / 标 False** | 节点 frontmatter 有 `updated_at`（ISO）；KB.md 有整库 `version:int`；`FileNode.st_mtime` 存在；search 结果 `FileChunk` **不**带版本/hash；无内容 hash API | `caps.entry_version=False` → **`local_entry_version`（h-sha256 前12）**；`get_entry` 仍把 `updated_at` 填入 `Entry.updated_at`/`raw` 供面板展示 |
  | `passage_api` | **是** | `knowledge_search`/`search` 默认 **chunk 级**召回（path + start_line/end_line + text）；`node_search` 为实体级无正文（盘点用） | `caps.passage_api=True`；passage_extract 可直接消费 chunk 文本，少走全量 get_entry |

- 读路径映射（→ `ReMeReader`）：
  - `search` ← `POST /knowledge_search`（`query`,`limit`,`bucket`）；hits = `metadata.results[]`（FileChunk dump）
  - `get_entry` ← `POST /read`（path）+ 必要时 `POST /frontmatter_read`
  - `list_index_tree` ← **无原生 API**；须适配层扫描 `knowledge/` + frontmatter（`name`/`description`/`signals` 中 `chain:*`）或解析链路树 md 派生 `IndexTree`（补 Q12）
- 写路径映射（→ `ReMeWriter`）：
  - `write_proposal` ← `POST /save_to_knowledge`（`title`,`content`,`bucket`,…）；合并侧有 `expected_updated_at` 乐观并发（stale→inbox），**无**一次性令牌语义——令牌仍由 testerAgent confirm 端门禁，远端只做写入
  - 回查：`read`/`frontmatter_read` → `WriteResult.verified`
- Entry / 类型映射草案（WP-09 落表）：
  - `entry_id` = workspace 相对 path（稳定、可 round-trip 到 `read`）
  - `entry_type` ← bucket：`business/{wiki,procedure,personal}`→`business`；`business/openapi`→`api`；`business/dbInfo`→`db`；`test/defects`→`defect`；`test/{test_cases,test_design,test_data}`→`flow_case`；索引派生节点→`link_index`
  - `link_id`/`story_id` ← frontmatter `signals` 的 `chain:<中文标题>`（kb-chain-tag 约定）；无标记则空，靠 IndexMirror 降级
- 与设计偏离/缺口：① IndexTree 无服务端接口（Q12）；② dd `types`/`scope` 与 ReMe `bucket`/`prefixes` 不等价；③ 默认不把 frontmatter 打进 chunk metadata，不能指望服务端按 `chain:` 过滤；④ SDK 非首选（与 dd「S1 前优先 SDK」原文相反——以本探测为准）
- 验收：一页结论已写入本交接单；不通过项均挂 §8.4 预案（镜像 / 本地 hash / chunk 直用）
- 遗留：本地未起 ReMe 服务做联通实测（源码级结论）；WP-09 实现后用 `POST /workspaces/{id}/kb/test` 做真实探活
- 下个包起步点：**WP-09**（`adapters/reme_http.py` 主实现 + Factory `register("service", …)`；可选 stub/sdk；Writer 替换 `UnavailableWriter`；caps 按上表硬编码/探测；IndexTree 派生逻辑）

### [WP-28] API-D 调试/知识库提案 — done（2026-09-28）

- 状态：done
- 交付物：[graph/retrieval/pipeline.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/retrieval/pipeline.py) 增量——`retrieve_pipeline(..., persist=True)`：persist=False 时跳过 TraceDAO.append 与 snapshot 写（playground 临时 task_id 不落任何业务表，dd §8.7），最终 candidates 挂 `outcome.candidates` 随响应返回；[graph/retrieval/ops_b.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/retrieval/ops_b.py) `RetrievalOutcome.candidates` 字段（节点路径恒空）；[adapters/reme.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/adapters/reme.py) §9.2 段——`WriteResult`/`ReMeWriter` Protocol/`UnavailableWriter`（默认 502 writer_not_registered）；[store/models.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/models.py) `ProposalDAO.bind_idempotency_key`（只加不改，原子 UPDATE ... WHERE idempotency_key IS NULL）；新建 [api/debug.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/debug.py)——GET /tasks/{id}/traces（键集分页+stage/version 过滤，摘要计数维度）、GET /traces/{id}（候选 kept/drop_reason、injected/referenced/hallucinated/weak、degraded 全集）、GET /tasks/{id}/snapshots、GET /snapshots/{id}（items 含偏移/usage/latencies/model_ref）、GET /snapshots/{id}/items/{position}（full 档 read_snapshot_line 偏移读全文；非 full 与越界 position 均 404）、POST /workspaces/{id}/retrieval/playground（临时 TaskContext task_id=playground-{uuid} + snapshot_level 强制 off + RETRIEVAL_PRESETS 阶段校验 + overrides 白名单 top_k/query_paths/types 外键 400；funnel/candidates/injected/degraded/latencies 随响应返回）、GET /workspaces/{id}/kb/tree（reader.list_index_tree 只读）；新建 [api/kb.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/kb.py)——POST /kb/proposals（201+Location；32B 令牌 secrets.token_hex，仅 sha256 入库，明文仅响应一次；task_id 跨工作区 404）、GET /workspaces/{id}/kb/proposals（status 过滤+分页，永不携带令牌哈希）、POST /kb/proposals/{id}/confirm（Idempotency-Key 必带 400；事务内校验 状态/令牌 hash/期限 并原子绑定幂等键 → 事务外调 ReMeWriter → 成功 mark_confirmed+write_result 终态；已确认同键重放返回首次 WriteResult、异键 409；错令牌 400 KB_TOKEN_INVALID；过期先提交 expired 再 409 PROPOSAL_EXPIRED；写失败保持 pending+fail_count+1 → 502 KB_UNREACHABLE 可同 token 同键重试；verified=False → needs_manual_check=true）；[main.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/main.py) 接线（kb_writer 入 app.state + 两路由）；新增 tests/test_api_d.py（23 用例）
- 验收：`pytest tests/ -W error` **757 passed**（734+23）。**场景 12a（import-linter 门禁）**：AST 扫描 graph/ runtime/ store/ 全部 .py，断言无 ReMeWriter/WriteResult/UnavailableWriter 导入——writer 不在 AppContext/TaskContext 任何字段上，图运行代码路径物理不可达（PRD 7 硬性要求）。**场景 12b（confirm 幂等重放）**：confirmed 后同 Idempotency-Key 重复确认 → 200 返回首次 WriteResult 且 writer 调用计数恒 1；异键 → 409 TASK_STATE_CONFLICT。另覆盖：traces 空列表/倒序分页无重无漏/stage+version 过滤/非法游标 400/任务 404/详情全字段（drop_reason=budget_cut、weak_ref、hallucinated）；snapshots 列表元数据（has_full/item_count/truncated）/详情 items 偏移+usage/full 档双 position 逐字节对拍读回 content/meta 档与越界 position 404；playground happy（funnel 三项和=total、injected⊆kept、**retrieval_trace/context_snapshot 行数恒 0**）/overrides 类型过滤生效/未知键与非法类型与未知阶段各 400/工作区 404/无 app_ctx 409；kb/tree 派生树非空+工作区 404；提案创建（64 位 hex 令牌、库内仅 sha256、expires_at 未来时、Location 头）/创建守卫三 404/列表 status 过滤+分页+不泄漏哈希；confirm 错令牌 400/缺字段 400/缺幂等键 400/提案 404 且均不落账；写失败（ok=False 与抛异常两路径）pending+fail_count=1+键已绑定，同 token 同键重试成功；needs_manual_check；默认 UnavailableWriter 502
- 与设计偏离（均不碰冻结契约，文件头 docstring 已登记）：① **retrieve_pipeline 增 persist 形参**——dd §8.7 要求 playground"不落任何业务表"，而 §8.3 管线恒写 trace（task 外键会违例）；persist=False 跳写并把 candidates 挂 outcome，节点路径行为不变；② **确认令牌有效期定 24h**（api/kb.py PROPOSAL_TOKEN_TTL_SEC）——dd §11.4 要求 expires_at 但未定时长，§13.2 无对应配置项；③ **写失败复用 KB_UNREACHABLE(502,retryable)**——dd §17.1 错误码目录（已冻结）无 KB_WRITE_FAILED 专码，details.error_code 透传 writer 返回区分网络失败/写入拒绝；④ **过期在 confirm 触达时惰性置 expired**（dd §6.5"过期由惰性任务置 expired"同口径，未新增调度器）；⑤ **ReMeWriter 形参落为 str/dict**——§9.2 的 OneTimeToken/KbPayload 具名类型 v1 未单建（语义=令牌明文串与提案 payload dict）；⑥ **非 full 档 items/{position} 走 404 NOT_FOUND** 而非 400（资源不存在语义）；⑦ traces 列表项为摘要计数（query/candidate/kept/injected/referenced/degraded 计数），候选全集仅在详情返回——dd 未定列表/详情粒度，按 §5.5"详情=候选/裁剪原因"文字切分
- 遗留与提问：① **真实 ReMeWriter 待 WP-09** 注册（UnavailableWriter 为默认替身，同 reader 的 Factory 替身口径）；② **create 端点未做 (端点,key) 幂等去重**——提案创建无 Idempotency-Key 语义（幂等键在 confirm 才绑定入库），tech-design §5.0"所有 POST 支持可选 Idempotency-Key"的通用去重存储仍属 WP-29；③ expired 提案不会被自动清理之外的置位——列表中 pending 但已过期的行在 confirm 触达前仍显示 pending（Maintenance 只清终态，WP-29 可考虑补惰性过期扫描）；④ playground 每次请求新建 IndexMirror（ensure_fresh 每次拉树）——低频调试端点未复用缓存，如后续接面板高频调用可考虑按 workspace 缓存
- 下个包起步点：**WP-29 Reconciler/幂等/清理**（缺文件/改文件/孤儿三态检测+消解、IdempotencyStore 内存 TTL（接线 review/regenerate/run 幂等键去重）、保留期惰性清理含 exports/ 目录；dd §11.3 §6.6 §3.3；FileStore.list_case_files/soft_cleanup 与 Maintenance.lazy_purge 已就位）

### [WP-29] Reconciler/幂等/清理 — done（2026-09-28）

- 状态：done
- 交付物：[store/models.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/models.py) DAO 增量（只加不改）——`TestcaseDAO.list_all_for_task(task_id)` 全量含 obsolete、`WorkspaceDAO.list_all()` 未软删全量、`TaskDAO.list_all_by_workspace(workspace_id)` 全量含终态；新建 [runtime/idempotency.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/idempotency.py)——`IdempotencyStore`（内存 dict[(endpoint,key)]→IdemRecord，TTL 24h，threading.Lock）：`lookup` 命中同 hash 返记录、hash 不同抛 VersionConflict(409)、未命中/过期返 None；`record` 落键并惰性清过期；`request_hash(body_json)` sha256；`purge_expired` 显式清理；新建 [runtime/reconciler.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/reconciler.py)——`Reconciler(db, file_store)`：`reconcile_workspace` 三态检测（file_missing→mark_error；orphan→报告 orphan_paths 不挂接；hash_conflict 仅 error_info 为空的行比对→mark_error），`reconcile_all` 全工作区；`ReconcileReport.to_dict` 含计数与 tasks 明细；[store/workspace_files.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/workspace_files.py) 增量——`FileStore.cleanup_exports(retention_days)` 扫 `workspaces/*/tasks/*/exports/*` 按 mtime 删过期 zip 整目录；[runtime/maintenance.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/maintenance.py) 扩展——DEFAULT_RETENTION 新增 `obsolete_cases_days=30`、`exports_days=30`；`Maintenance.__init__` 新增 `file_store`、`idempotency_store`；`lazy_purge` 新增 `soft_cleanup(.tmp)`、`cleanup_exports`、`idempotency_store.purge_expired()`；[api/common.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/common.py) 增量——`idem_check`/`idem_record` 幂等助手；[api/cases.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/cases.py) review & regenerate 接线（Idempotency-Key 头→同 hash 重放、异 hash 409、成功后 record）；[api/tasks.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/tasks.py) run_task 接线（无 body 用空串 hash）；新建 [api/maintenance.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/maintenance.py)——`POST /workspaces/{id}/reconcile` 手动触发对账返回 ReconcileReport；[main.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/main.py) 接线——IdempotencyStore 入 app.state、Maintenance 注入 file_store+idem、启动时 `reconcile_all()`、挂载 maintenance 路由；新增 tests/test_reconciler.py（7）、test_idempotency.py（8）、test_maintenance_cleanup.py（5）
- 验收：`pytest tests/ -W error` **777 passed**（757+20）。**场景 10（三态检测与恢复）**：file_missing（删文件→mark_error file_missing，报告计数+清单）、hash_conflict（外部改文件内容→mark_error hash_conflict）、orphan（盘上多余 MD→orphan_paths 不挂接不改行）、已带 error_info 行不参与 hash 比对（error_info 保持原值不覆盖）、reconcile_workspace 不存在工作区 404、手动端点返回计数与清单。**幂等（dd §6.6）**：同 key 同 body 重放返回一致响应且不产生第二条 review_record；同 key 异 body 409；TTL=0 过期 lookup 返 None；purge_expired 清过期键；record 写入顺带清过期。**清理（dd §11.1 §6.5）**：soft_cleanup 删过期 .tmp 留新鲜 .tmp 与正常 md；cleanup_exports 删过期 exports job 目录、不动 cases/snapshots；Maintenance.lazy_purge 联动 tmp_files 与 idempotency_keys 计数。
- 与设计偏离（均不碰冻结契约，文件头 docstring 已登记）：① **新增 POST /workspaces/{id}/reconcile 手动端点**——tech-design §5 端点清单未列，但 dd §11.3"用户在 UI 手动点检查文件一致性"需要触发入口，登记为偏离；② **Idempotency-Key 为可选头**——tech-design §5.0 称 tasks/run、regenerate"必带"，但 review 等列"支持"，统一为可选（无键不幂等、不阻断）以兼容前端渐进接入；③ **幂等重放用 rec.body 重建响应模型而非原始 JSONResponse**——FastAPI 端点返回 Pydantic 模型，重放时用 `ReviewOut(**rec.body)` 重建，状态码恒 200（仅成功响应才落键）；④ **hash_conflict 仅对 error_info 为空的行比对**——dd §11.3 伪码 by_path 排除已带 error_info 行，避免覆盖用户已标记的缺失/冲突状态
- 遗留与提问：① **幂等存储为单进程内存**——dd §6.6 明确一期简化，多 worker 部署前须迁移 Redis（tech-design §6.6 已登记）；② **hash_conflict 的冲突消解无专用后门**——dd §11.3 规定走普通 PUT /cases/{id} 二选一覆盖，本包未实现"以文件为准重新入库"快捷动作；③ **reconcile_all 在 lifespan 同步执行**——工作区/用例量大时可能拖慢启动，后续可改后台任务（但 dd §11.3 要求启动对账，先满足语义）；④ **expired 提案惰性置位仍未补**（WP-28 遗留④，Maintenance 只清终态），可在 WP-29 后续迭代补扫
- 下个包起步点：α 后端线除 **WP-09（ReMe 真实适配）** 外已全部 done；WP-09 依赖 **SP-1（ReMe 三能力探测）** 结论（handoff §2 仍 todo）。下一步可选：① 执行 **SP-1** 探测并写结论以解锁 WP-09；或 ② 启动 **γ 前端线 WP-F0 脚手架**（WBS §4 标注"可任意早启动"，不阻塞后端）

### [WP-27] API-C 用例/导出 — done（2026-09-28）

- 状态：done
- 交付物：[store/workspace_files.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/workspace_files.py) 增量——`FileStore.edit_case`（编辑专用：服务端重算元数据两遍渲染 → `_overwrite_case_sync` 带 expected_hash 乐观锁覆盖；不重算落盘路径，标题改名不迁移文件）与 `FileStore.export_zip_path`（exports/{job_id}/export.zip，仅拼路径）；新建 [runtime/export.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/export.py)——`ExportService(app)`：`export_md_zip`（默认筛选 active 且 adopted/edited_adopted，显式 case_ids 仍过同口径且跨任务 404 → 逐文件 hash_of 对拍（file_missing/hash_mismatch 列 skipped，R34）→ ≤200 条且 ≤20MB 同步 `asyncio.to_thread(_write_zip_sync)` 返 ready+download_url，否则登记 `_EXPORT_JOBS` 进程内 job 后 `asyncio.create_task` 异步返 running+job_id）、`get_job`/`job_zip_path`（task/job 404、failed 抛错）；zip 结构 dd §9.4 `v{n}/{point_id}-{slug}.md`（重名 -2/-3）+ INDEX.md（序号/标题/优先级——从 MD front-matter 提取/评审状态/溯源条款），内存 BytesIO 打包后 `_atomic_write` 落盘；完成/失败落 task_event（export_ready/export_failed）；新建 [api/cases.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/cases.py)——GET /tasks/{id}/cases（list_by_task 键集分页 + status/review/version 过滤）、GET /cases/{id}（MD 正文+content_hash+lineage+trace_refs）、PUT /cases/{id}（If-Match 必带 400 守卫；非 active 400；parse 失败 400；服务端重算 front-matter（case_id/point_id/stage_version/trace_refs 以 DB 为准，priority 非法归 P1）；FileConflict→409 VERSION_CONFLICT details={expected,current}；落账 update_content+update_review(EDITED_ADOPTED)+review_record(edit,{diff,new_hash})，diff 为 difflib 行粒度统计）、POST /cases/review（全量校验循环：dao.get 404/非 active→422 reason=case_not_active/转换表外→422 details 含 allowed，任一非法整体 422 不落账；全部通过才 immediate_tx 内逐条 update_review+review_record({from,to})）、POST /cases/regenerate（app_ctx 守卫→首个 case 定位 task_id→regenerate_cases(WP-26)→RegenerateOut{task_id,status,new_case_ids}）、POST /tasks/{id}/export + GET /tasks/{id}/export/{job_id}（?download=1 → FileResponse zip 流，否则轮询）；[main.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/main.py) 挂载 cases_router；新增 tests/test_api_c.py（29 用例）
- 验收：`pytest tests/ -W error` **734 passed**（705+29）。**WBS #27 场景 11a**：编辑过期 If-Match→409 VERSION_CONFLICT 且 details.current=盘上当前 hash、未落账（review_status 仍 pending、无 review_record）；正确 hash→200，front-matter 误改（case_id: hacked-id / stage_version: 99）以服务端重算为准，review_status→edited_adopted + review_record(edit,diff)。**场景 11b**：批内含同状态重复评审（adopted 再 adopt）→整体 422 VALIDATION_REVIEW_TRANSITION，合法项（case-1 adopt）也不落账、无 review_record；非 active 评审→422 reason=case_not_active。另覆盖：列表空页/键集分页（limit=2 两页无重无漏，倒序）/review=adopted 与 status=active 过滤/非法游标 400；详情正文与盘上一致/404；编辑缺 If-Match 400/obsolete 400/坏 MD 400；评审三者互转允许（adopted→reject 改判）、unknown case 404/非法 action 400（pydantic Literal）/空 items 400；regenerate HTTP 入口（new_case_ids+lineage 挂旧 case+文件落盘+任务回 completed、跨任务 404 不置 failed、首 case 未知 404、空指令 400）；导出（默认筛选只进 adopted/edited_adopted，zip 名字节序、INDEX 序号按生成序、export_ready 事件落库、同步 job 可轮询可下载；hash 漂移+缺文件列 skipped 不进 zip；显式 case_ids 仍过滤 pending、跨任务 404；monkeypatch SYNC_MAX_CASES=0 走异步 running→轮询 ready→下载；job 404/跨任务 404；空选择 ready 空 zip 仅 INDEX.md）
- 与设计偏离（均不碰冻结契约，文件头 docstring 已登记）：① **导出 job 状态为进程内存 `_EXPORT_JOBS` dict**（dd §9.4 一期口径"后台 asyncio task + 结果落 task_event/临时目录"——事件落库不变，job 结果路径不落 DB 表；重启后 job 查询 404 但 zip 文件仍在盘，单 worker 模型合法）；② **zip 落任务目录 exports/{job_id}/export.zip** 而非系统临时目录（复用 FileStore 路径安全校验，WP-29 保留期清理可按任务目录统一扫描）；③ **评审转换表补"同状态恒等转换非法"口径**——tech-design §3.2④ 文字为"pending→三者均可、三者互转允许"，12 种 (from,action) 组合中仅 3 种恒等转换（adopted+adopt 等）判非法 422（重复评审无意义且会产生误导性 review_record）；④ **非 active（obsolete）用例评审走 422** VALIDATION_REVIEW_TRANSITION reason=case_not_active（转换表只管 review_status 维度，status 维度的非法操作同走评审类 422 而非 400）；⑤ **regenerate 端点接受 Idempotency-Key 头但不去重**——IdempotencyStore 属 WP-29（迁移 004 未建），本包不现场扩范围
- 遗留与提问：① **幂等键去重缺口**：tech-design §5.0 要求 mutate 端点幂等键必带+去重，待 WP-29 IdempotencyStore 落地后接线（regenerate/review/edit 均需要）；② 导出 zip 的物理清理（exports/ 目录保留期）属 WP-29 维护任务；③ 编辑 diff 为行粒度粗统计（lines_added/deleted/changed），未做结构化段级 diff——review_record.detail 已留 new_hash，前端如需精细 diff 可自行对拍；④ 导出 INDEX.md 的优先级从 MD front-matter 正则提取（testcase 行无 priority 列），front-matter 缺 priority 时显示 "-"
- 下个包起步点：**WP-28 API-D 调试/知识库提案**（检索调试面板 traces/snapshots 只读端点 + kb_proposal 提案确认流；dd §10.2 §10.6；ReviewDAO/TraceDAO/SnapshotDAO/ProposalDAO 已就位）

### [WP-26] API-B 任务/确认/取消 — done（2026-09-27）

- 状态：done
- 交付物：[runtime/runner.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/runner.py) 增量——`Runner.start` 增加 `event`（None=直跑不入队 TRANSITIONS 事件，回退接管用）与 `resume`（Command(resume=...) 入参）参数；抽出 `build_task_context`（reconcile/replay 与 API 层共用的 task 上下文构建）；[store/models.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/models.py) 增量 DAO——`TaskDAO.mark_confirmed`、`TestcaseDAO.set_lineage`、`TestcaseDAO.mark_obsolete_by_ids`；**`TaskDAO.start_new_run` 同时清空 `runner_heartbeat`**（见偏离⑥）；新建 [api/tasks.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/tasks.py)——POST /tasks（201+Location，先文件后 DB 落账）、GET /tasks/{id}（TaskOut：active_artifacts 按 stage 键 / progress / error_info / stale 八天判定 / clauses）、POST /tasks/{id}/run（RunOut+resume_from 批次游标）、POST /tasks/{id}/cancel（三态：waiting_*→200 即生效 / running→202+cancel_requested / cancelling→202 / 终态→409）、POST /tasks/{id}/confirm（confirm→mark_confirmed+Runner.start(event="confirm")；modify→immediate_tx{旧版 supersede+next_version+put(v+1,user_revised)+checkpoint_revision 消息}+aupdate_state 注入新 payload+Runner.start(event="confirm")）、POST /tasks/{id}/answer（校验 question_id/answer→clarification_qa 留痕→Runner.start(event="answer",resume=Command resume 值)）、POST /tasks/{id}/rollback（预检→WP-24 rollback 协议→Runner.start(event=None) 接管新派生 thread）；新建 [runtime/regenerate.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/regenerate.py)——regenerate 入口（§7.6）：Registry.acquire→前置校验（build_task_context/case artifact/_load_point_plan/旧用例循环校验）→置 running（started 标志，其后失败按 Runner 同口径收口 failed，前置校验失败保持原状态）→message/story_to_link/generate_case_batch→_attach_lineage（按 point 分组 (created_at,id) 排序 zip，新旧一一挂 Lineage(root_case_id=旧 root, regenerated_from_case_id=旧 id)）→keep_original=False 时旧行 obsolete→completed+task_done；[main.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/main.py) 接线：app_ctx 注入图 state + 挂 tasks 路由；新增 tests/test_api_b.py（36 用例）
- 验收：`pytest tests/ -W error` **705 passed**（669+36）。**WBS #26 场景 4**：CP1 停等后以 expected_version=2 迟到 confirm→409 VERSION_CONFLICT；正确版本 1 放行→跑到 CP2（waiting_confirm@point_write）。**场景 5**：waiting_input（澄清挂起）取消→200 立即生效。另覆盖：create 404（会话不存在）/201+Location；get 双任务字段断言（stale 仅 waiting_* 生效、running 不 stale）；run happy path（run→CP1→confirm→CP2→confirm→completed→终态双 confirm 409 TASK_STATE_CONFLICT）；resume_from 批次游标（failed@point_write+progress b0 done/b1 started→RunOut 精确回 {node,batch_id,done,total}）；duplicate run 409 TASK_BUSY；completed run 409；runner_not_ready 409；cancel 四态（running 202+cancel_requested→假图轮询 cancelled()→aborted）；confirm modify（v2 落账+checkpoint_revision 消息+state 更新，自定义图捕获下游 point_write 读到修订后 plan）；modify 无 payload 400/坏 payload 422/阶段不符 400/superseded 400（迟到 modify 后 v1 confirm→VERSION_CONFLICT）/跨任务 404/running 409；answer（interrupt 恢复+captured resume 值+clarification_qa+clarification_needed 事件留痕、缺 question_id 400/缺 answer 400/waiting_confirm 409）；rollback（无修订→graph_run_id 变、新派生 thread ::run1 重跑到 CP1、节点重跑产出 art-link-v2 active；错版本 409/running 409/跨任务 404）；regenerate async（lineage 挂旧 case+regen_instruction 消息+task_done、keep_original=False 旧行 obsolete、状态冲突不置 failed、缺 case artifact 保持 completed、跨任务 NotFound）
- 与设计偏离（均不碰冻结契约）：① **confirm/answer 经 Runner.start 后台执行**——dd §10.3 伪码在 API 处理器内 ainvoke，实际改为 Runner.start(event=...) 后台任务执行：API 事务（mark_confirmed/modify 落账）与图执行解耦，API 快速返回句柄，天然获得 Registry 并发守卫与心跳；② **Runner.start 增加 event/resume 可选参数**——原签名只支持首跑，confirm/answer 需要带事件恢复、回退需要 event=None 直跑（TRANSITIONS 表无对应迁移）；③ **rollback 的 ctx.reader=None**——rollback 协议不需要 ReMe 读（analyze_impact 纯本地 diff），构造 API 层 ctx 时省略；④ **regenerate 的 _attach_lineage 按 point 分组 zip**——dd §7.6 只说"新用例挂旧用例 lineage"，未定多对多时的配对口径；按 (created_at,id) 排序后逐 point 一一对应（旧用例数>新时多余旧用例不挂），保证 root_case_id 语义稳定；⑤ **regenerate 的 HTTP 端点归 WP-27**（用例维度入口），本包只交付 runtime 入口函数与单测；⑥ **start_new_run 清空 runner_heartbeat**（本轮集成修复）——回退协议把任务切 running 后经 Runner.start 接管，Registry acquire 的"新鲜心跳=他方运行中"判活会命中上一次运行残留的心跳→409；且心跳 UPDATE 按 graph_run_id 匹配，新 run 永远续不上旧值。清空后 Runner.acquire 重新落自己心跳。WP-24 测试无断言心跳保留，不冲突；⑦ answer 留痕内容按 resume 契约键 `id` 取值（实现期修正：resume_value 为 [{"id","answer"}]）
- 遗留与提问：① confirm 返回 200 句柄时图在后台执行，HTTP 语义为"已受理"——前端须订阅 SSE 等 waiting_confirm@新阶段，测试以 status+current_stage 联合轮询规避"旧 gate 状态"竞态；② cancel 的 202 路径依赖节点轮询 `ctx.cancelled()`（WP-18 批次执行器已实现），纯 CPU 密集节点（无 await 点）实际要等节点自然结束才 abort；③ TaskOut.stale 采用 updated_at 八天（iso_ago(8*86400)）近似 §10.2 的"suspend_stale_days"语义，仅标记不改状态（Reaper 才负责收口）；④ regenerate 的 Registry.acquire 与前置校验间存在极小竞态窗口（acquire 后任务被并发改状态）——由 started 标志兜底（失败时若未置 running 则只释放锁不收口）
- 下个包起步点：**WP-27 API-C 用例/导出**（用例列表/详情/评审/内容更新 + regenerate HTTP 端点 + 导出；dd §10.4；TestcaseDAO 评审/内容更新方法已就位（WP-03/04），regenerate() runtime 入口已就绪（本包），导出复用 put_batch 幂等口径）

### [WP-25] API-A 工作区/会话/配置 — done（2026-09-27）

- 状态：done
- 交付物：[store/models.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/models.py) 增量 DAO（只加不改）——`TaskDAO.count_active_by_workspace`（活跃=running/waiting_confirm/waiting_input/cancelling，含"已建未启动"）、`TaskDAO.list_by_conversation`（会话维度任务分页，(created_at,id) 倒序口径）；新建 [api/common.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/common.py)——`checked_cursor`（decode_cursor 的 ValueError→ValidationError 400，dd §3.3 API 层翻译约定）、`page_response`（Page[Row]→§10.1 {items,next_cursor} 信封）；新建 [api/workspaces.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/workspaces.py)——GET/POST /workspaces（201+Location 头）、GET/PUT/DELETE /workspaces/{id}（软删；删除守卫"需确认无活跃任务"→409 TASK_STATE_CONFLICT；文件按保留期惰性清理不在本端点）、POST /workspaces/{id}/kb/test（factory.probe 一次性探活+能力位，dd §8.4）；新建 [api/agents.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/agents.py)——/agents CRUD（创建接口预留）、GET/POST /workspaces/{id}/agents（绑定幂等 INSERT OR IGNORE；前置校验工作区与智能体，二者缺失均 404 不暴露存在性）；新建 [api/conversations.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/conversations.py)——GET /workspaces/{id}/conversations、POST /conversations、GET /conversations/{id}（详情=会话+任务摘要 TaskSummaryOut+分页消息）、GET/POST /conversations/{id}/messages（SendMessageIn chat|change_request+context→payload，落库+ConversationDAO.touch）；新建 [api/config.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/config.py)——GET/PUT /config/model、POST /config/model/test（按**已保存**配置现场构造 OpenAICompatLLMClient 最小调用 "ping"；`_build_client` 为测试注入缝）；修改 [main.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/main.py)——ReMeReaderFactory 进程内唯一构造入 app.state.reme_factory（kb test 与 Runner build_ctx 共用），挂载 4 路由；新增 tests/test_api_a.py（36 用例）
- 验收：`pytest tests/ -W error` **669 passed**（633+36）。**跨工作区 404 不暴露存在性**：POST /workspaces/{missing}/agents（智能体存在）与 POST /workspaces/{ws}/agents（智能体缺失）同 404 NOT_FOUND；软删工作区 GET 与不存在 GET 的 code/retryable/details 同形态；列表排除软删行。**探活错误码**：kb test builder 抛 KbUnreachable→502 KB_UNREACHABLE(retryable=true)、未注册 mode→400 VALIDATION_BODY、成功回 caps 三态（entry_version=False 仍 200=降级标记非错误）；model test 未配置→400 LLM_BAD_REQUEST、超时→504 LLM_TIMEOUT、成功回 model+latency（FakeLLM 断言最小调用=单条 user "ping"）。另覆盖：workspaces CRUD/分页无重无漏/非法游标 400/部分更新、四种活跃状态各阻删除 409、终态后可删、agents CRUD/绑定级联/幂等、conversations 消息分页倒序、touch 刷 updated_at、change_request 落 payload、详情含任务摘要、model config PUT 回读
- 与设计偏离（均不碰冻结契约）：① 探活失败 HTTP 状态取 errors.py 类属性（KbUnreachable→502、LLMTimeout→504），dd §10.1 表写"424（同步探活类接口）"——以 dd §17.1（自述"HTTP 映射唯一来源"，errors.py 已冻结）为准，文档间预存差异未改代码；② GET /conversations/{id} 任务部分为轻量 TaskSummaryOut（id/status/current_stage/created_at/updated_at），完整 TaskOut 属 WP-26 tasks API；③ MessageOut.author 按 role 映射固定展示名（用户/助手/系统）——v1 无用户体系；④ DELETE 成功返回 200 {"ok":true}（§10.1 状态表未列 204）；⑤ POST /workspaces/{id}/agents 返回 200 AgentOut（绑定幂等非新建，不带 201/Location）
- 遗留与提问：① **WP-09 未完成**——kb test 按"依赖已被 mock"口径先行：端点面已按 Factory.probe 完成，当前默认工厂无 sdk/service builder，探活返 400 VALIDATION_BODY；WP-09 注册真实 builder 后即插即用（替身=Factory+FakeReMeReader）；② POST /conversations/{id}/messages 只落库+touch，"系统决定续跑或回退（tech-design §4.2②）"编排触发在 WP-26 接线；③ api_key 原样回显：本地单用户无鉴权（tech-design §6 运维约定），如需脱敏需同步定义 PUT"保持原值"哨兵协议；④ conversation 详情任务列表不分页（一次 limit=200），消息分页正常
- 下个包起步点：**WP-26 API-B**（tasks create/run/cancel/confirm(含 modify)/answer + 状态守卫 + regenerate 入口接线；dd §10.2 §10.3 §7.6；rollback 协议函数（WP-24）与 Runner.start/TRANSITIONS（WP-22）已就位，confirm/answer 需按中断类型分别接 resume 入参）

### [WP-24] 回退协议 — done（2026-09-27）

- 状态：done
- 交付物：[domain.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/domain.py) 补 §2.9 回退影响面类型 `IdChange`/`StageImpact`/`ImpactAnalysis`（含 `affected_points()`）；[store/models.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/models.py) 增量 DAO——`ArtifactDAO.inherit_to_run`（graph_run_id 改挂新 run + payload 写 `inherited_from_run` 留痕，status/review_status 原样保留）、`ArtifactDAO.superseded_and_obsolete(task_id,from_stage,downstream,new_run)`（按 StageImpact.affected_ids 判 supersede/inherit，返行动记）、`TestcaseDAO.mark_obsolete_by_points`（受影响 point 的 active 用例置 obsolete，unaffected 继承）、`TaskDAO.run_seq`（从 thread_id 推算下一个 run 序号）；新建 [graph/rollback.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/rollback.py)——`analyze_impact(stage,old,new)` 结构化 diff（link_identify：story 仅 summary 变→unaffected、删除/link 结构变→affected 且波及同 link 下 story；point_write：仅 priority/title 变→unaffected、删除/clause_ids 变→affected）、`RollbackIn(artifact_id,expected_version,revised_artifact)`、`rollback(ctx,task_id,body)` 主函数（`BEGIN IMMEDIATE` 单事务：状态机校验→版本校验→analyze_impact→解析 affected_point_ids→superseded_and_obsolete→写用户修订版 v+1(user_revised)→mark_obsolete_by_points→start_new_run+status=running→change_request 留痕消息；事务后 `start_run_from_stage` 启动派生 thread + emit node_start）、`_build_entry_state`（从 task.clauses + 上游 active artifacts 构建入口 state）；[graph/registry.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/registry.py) 新增 `_STAGE_PREDECESSOR` 映射 + `start_run_from_stage(new_thread,stage,entry_state)`（SP-2 fork 配方：`aupdate_state(cfg,entry_state,as_node=前驱)`）；新增 tests/test_rollback.py（18 用例）
- 验收：`pytest tests/ -W error` **633 passed**（615+18）。场景 3a「改摘要全继承零 LLM」：改 S2 summary→impact 无 affected→point_write/case_generate artifact inherit（graph_run_id 改挂新 run、payload.inherited_from_run=旧 run、status 仍 active）→节点重放逻辑 `existing.graph_run_id==run_id` 命中→零 LLM；link v1→superseded、v2(user_revised,confirmed_by=user) active 挂新 run；用例全部 active。场景 3b「删 story 精确作废」：删 S2→S2 affected→point_write/case_generate artifact supersede→pt-2-1 的用例 obsolete、pt-1-1 的用例 active；link v2 仅 1 条 story。另覆盖：状态守卫（running→TASK_STATE_CONFLICT）、版本冲突（expected_version 错→VERSION_CONFLICT 不落账）、无 revised_artifact（不写新版本、下游 unaffected 继承）、事务原子性（artifact 不存在→异常回滚、task 未切新 run、无 v2）、analyze_impact 五规则（summary-only/删除/link 变/priority-only/clause_ids 变）、run_seq 递增、inherit_to_run 留痕、superseded_and_obsolete 三态（inherited/superseded/skipped）、mark_obsolete_by_points 精确、start_run_from_stage fork（as_node=前驱→next=目标阶段、入口 state 已注入）
- 与设计偏离（均不碰冻结契约，文件头 docstring 复述）：① **继承复用节点现有重放逻辑**——`inherit_to_run` 把 unaffected artifact 的 graph_run_id 改挂新 run，节点侧 `existing.graph_run_id==run_id` 判定自然命中并重放，无需在节点入口额外判 `inherited_from_run`（dd §18.3 注"unaffected 直接继承的判定在节点入口做"由 graph_run_id 重写隐式实现，零新增节点逻辑）；② **link.story_ids 不计入结构变更**——story_ids 是从 stories 列表派生的引用字段，仅因 story 增删而变化；若计入会导致"删一个 story 波及同 link 下其余 story"的过度作废，故 `_diff_links` 只比对 link_id/title/summary/hit/entry_id/entry_version/confidence；③ **start_run_from_stage 签名从 entry_plan 改为 entry_state**——dd §11.2 伪码传 `entry_plan`，但派生 thread 需要完整入口 state（含 clauses 与上游产物），由 rollback 内 `_build_entry_state` 构建后传入；目标阶段的修订产物已在 DB 落账（graph_run_id=new_run），节点经 get_active 回放，无需注入 state；④ **point_write 回退 affected 时整 artifact supersede**——dd §11.2 未明确部分继承粒度，当前实现按"阶段有任一 affected item 则 supersede 整 artifact、节点重跑"处理（point_write 重跑后 unaffected story 的 point_id 确定性不变、case_generate 重跑时 INSERT OR IGNORE 跳过已有 unaffected 用例），不影响"精确作废"验收口径；⑤ **rollback 直接调用 ctx.app.graphs.start_run_from_stage**——不经 Runner.start（回退已在事务内切 running+新 run，Runner 会重新 acquire 锁并跑图；当前实现由 API 层 WP-26 决定是否经 Runner 还是直接调 rollback，本包只提供协议函数）
- 遗留与提问：① rollback 协议函数已就绪，但 API 入口（POST /tasks/{id}/rollback）与 Runner 接线（回退后是否经 Runner.start 跑图、还是直接 ainvoke）属 WP-26；② point_write/case_generate 在"部分 affected"场景下会重跑 LLM（unaffected 项靠确定性 ID + INSERT OR IGNORE 幂等，不产生重复行/文件，但有 LLM 调用）——若需零 LLM 部分继承，需在节点入口增加"按 point 粒度跳过已有 active 用例"的逻辑（dd §18.3 只要求"改摘要全继承"，未要求删 story 后 unaffected 零 LLM）；③ coverage_check 回退未在 analyze_impact 中处理（无下游，直接重跑即可）；④ `inherited_from_run` 仅写在 artifact.payload，未单独建表——dd §11.2 注"继承映射写 review_record 之外的一张轻量留痕"，当前用 payload 字段满足留痕需求，若需独立查询再考虑拆表
- 下个包起步点：**WP-25 API-A**（工作区/会话/消息/配置 CRUD + kb test；dd §10.2 §5.1~5.2；回退协议 rollback 函数已就位，WP-26 接 API 时直接调用）

### [WP-23] Reaper + 启停序列 — done（2026-09-27）

- 状态：done
- 交付物：新建 [runtime/reaper.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/reaper.py)——常量 `INTERRUPTED_BY_RESTART`；`Reaper(task_dao,stale_sec=120)`：`reap_on_startup()` 取 cutoff=iso_ago(stale_sec) 经 `list_stale_running` 找陈旧行（running/cancelling 且心跳空或更早），逐条 `update_status(failed,error_info={code,message,retryable:true,node:null,details:{}})`，返 task_id 列表（warning 日志）；`graceful_shutdown(registry,timeout_sec=30)`：`registry.all_tasks()`→逐个 cancel→`asyncio.wait(timeout)`，有 pending 记 error 返 False（不强制 kill，重启 reap 兜底），全部完成返 True，已取消任务正常、其他异常 warning。新建 [runtime/maintenance.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/maintenance.py)——`DEFAULT_RETENTION{events_days:7,snapshots_days:30,proposals_days:14}`；`Maintenance(db,config_dao=None,*,retention=None)`：`_resolve_retention` 合并默认值与 runtime_config.retention（非法/负值退回默认）；`lazy_purge()` 按天数 cutoff 调 EventDAO.purge_before / SnapshotDAO.purge_before / ProposalDAO.purge_terminal_before，返 `{events,snapshots,proposals}` 行数（"lazy"=仅启动跑一次，无后台周期任务）。DAO 增量（[store/models.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/models.py)）：SnapshotDAO.purge_before、ProposalDAO.purge_terminal_before（只删 status!=pending，保护等确认提案）；[store/db.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/db.py) 新增 iso_ago(seconds)（timedelta cutoff）；[runtime/runner.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/runner.py) TaskRegistry 新增 all_tasks()。重写 [main.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/main.py) lifespan 为 §6.5 序列：① 迁移+open db+FileStore → ② EventBus/config 读取 → ③ GraphRegistry+TaskRegistry（stale_sec 读 runtime_config）→ LLM 有效才建 AppContext/Runner（hb_interval 读 config；未配置 warning 不阻断）→ ④ reap_on_startup → ⑤ lazy_purge → ⑥ 服务；finally：graceful_shutdown(30s)→graphs.aclose→db.close。新增 tests/test_reaper.py（14 用例）
- 验收：`pytest tests/ -W error` **615 passed**（601+14）。场景 6 前半端到端：TestClient 启动预种的心跳空 running 任务 → 启动后 failed + INTERRUPTED_BY_RESTART（healthz 200）；后半（/run 游标恢复）由 WP-22 `test_failed_run_resumes_from_checkpoint` 已覆盖。另覆盖：null/旧心跳 running、cancelling 均改判；新鲜 running 与 waiting_confirm/waiting_input/completed/failed/aborted 全不动；error_info 定型（retryable=true/node=null）；优雅关闭三态（无任务 True、worker 收取消退出 True、immune 吞取消 0.05s 超时 False）；lazy_purge 旧 event 删近期留、旧快照删近期留、终态旧提案删（pending 旧提案与近期终态均保留）、retention_days=0 边界
- 与设计偏离（均不碰冻结契约，文件头 docstring 复述）：① **不注册信号处理器**——SIGTERM/SIGINT 由 uvicorn 自身处理并触发 FastAPI lifespan 关闭，集成点在 lifespan finally，避免与 uvicorn 信号处理冲突；② **不强制 kill 中途写盘**——超时未退出任务随进程终止，原子写协议保证现场可恢复，重启 reap 兜底改判（与"优雅关闭超时"语义一致）；③ 提案清理只删终态（status!=pending），pending 提案可能仍在等用户确认，dd 未明确，按安全口径；④ lazy_purge 不做后台周期任务（"lazy"），单 worker 启动时一次足够；obsolete_cases_days 的用例归档对账留给 WP-29 Reconciler
- 遗留与提问：failed→/run 的 API 触发路径（WP-26）经 Runner.start(event="run") 已可用；confirm/answer 的恢复入参（Command(resume=)/ainvoke(None)）需 WP-26 接线时按中断类型分别处理；reaped/purged 结果目前仅写日志和 app.state（last_reaped/last_purged），WP-25 是否需要暴露运维只读端点待定
- 下个包起步点：**WP-24 回退协议**（L 包，dd §9：回退派生新 run（start_new_run）、checkpoint 游标与阶段版本回滚策略；WP-26 的 rollback/regenerate 事件已在状态表就位）

### [WP-22] Registry + Runner — done（2026-09-27）

- 状态：done
- 交付物：新建 [graph/registry.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/registry.py)——`GraphRegistry`（`create_production(checkpoint_db_path,nodes=None)`：aiosqlite→AsyncSqliteSaver→setup 后经 build_graph 编译生产五节点图，名常量 `CASE_DESIGNER="case_designer"`；`from_graph` 测试直构；`get/aclose`）+ `PRODUCTION_NODES` 映射；新建 [runtime/runner.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/runner.py)——① `TRANSITIONS` 全表 + `validate_transition(current,event)`（非法→TASK_STATE_CONFLICT，API/Runner 共用）；② `TaskRegistry(task_dao,stale_sec=120)`：`acquire`（per-task asyncio.Lock 内：本地 owner 在→TaskBusyError；DB 行 running/cancelling 且心跳新鲜→TaskBusyError；其余发 uuid token）、`release`（token 匹配才释放）、`attach/is_running/get_task`；③ `Runner(app,heartbeat_interval=10)`：`start(task_id,event="run")`——acquire+迁移校验同步完成后建后台 asyncio.Task，立即返 `RunHandle{task_id,graph_run_id,events_url:"/api/v1/tasks/{id}/events"}`；`_run`：build_ctx（读 task/workspace→factory.for_workspace 造 reader、全量 DAOs、Emitter(bus,task_id)、cancel_event）→置 running+首心跳→心跳循环→ainvoke（ctx 经 config.configurable 注入）→按 aget_state 收口：next=()→completed+task_done、next 含 gate→waiting_confirm+checkpoint_waiting（stage/artifact_id/stage_version 取 get_active）、tasks 有 functional interrupts→waiting_input+clarification_needed；TaskCancelled→aborted；AppError/其余→failed（ErrorInfo 落 error_info）+task_error；finally 停心跳+release。修改 [runtime/context.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/context.py)（graphs→GraphRegistry|None、registry→TaskRegistry|None）；修改 [main.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/main.py)（lifespan 造 FileStore/GraphRegistry/TaskRegistry，LLM 配置有效时构造 AppContext+Runner，finally graphs.aclose+db.close）；新增 tests/test_runner.py（39 用例）
- 验收：`pytest tests/ -W error` **601 passed**（562+39）。验收两条：① **重复 run→409**（本地 owner 在飞时第二次 start → TaskBusyError；跨进程 running+新鲜心跳 acquire → 409）；② **心跳 0 行更新自杀**（外部换走 graph_run_id→heartbeat 返 0→cancel_event 置位→节点边界 TaskCancelled→aborted）。另覆盖：状态机 17 合法迁移+6 非法；Registry 错误 token 不能释放、stale 心跳放行、waiting_* 残留新鲜心跳不阻塞续跑、缺任务 404；Runner 全链路 task_done（handle 三字段、node start/end 成对）、CP1 中断（artifact 经假 link 节点落库→checkpoint_waiting 三字段）、澄清中断、ValidationError→failed/error_info/task_error 对账、**failed→run 从 checkpoint 恢复**（input None，flaky 节点二次成功→completed）、request_cancel→aborted、非法事件 409 且锁已释放
- 与设计偏离（均不碰冻结契约，文件头 docstring 复述）：① **不挂 EventBridge LangGraph callback**——节点事件自 WP-15/21 起由节点经 ctx.emit 直接发射（wrap 的 node_start/end、batch/coverage 各节点事件），EventBridge 与之重复；llm_token 流后续随 stream writer 单独接；② langgraph 1.2 的 ainvoke 遇中断不抛 GraphInterrupt 而返回含 `__interrupt__` 的 dict，Runner 经 aget_state.next/tasks 判定中断类型；③ 取消检测经心跳循环每轮查 is_cancel_requested→置 cancel_event，节点边界直接转 aborted（不经 cancelling 中间态——running→cancelling 的状态置位留给 WP-26 API 层）；④ `_mark_failed` 在无节点归属时用 `_UNSET` 保持原 current_stage（列为 NOT NULL，不可置 NULL）；⑤ lifespan 对引导行 model_config={}（LLM 未配置）容忍：warning 后不构造 Runner 但不阻断启动（SSE/health 可用），WP-23 统一启停口径
- 遗留与提问：Reaper（reap_on_startup 把陈旧 running/cancelling→failed）与优雅关闭 30s 属 WP-23；confirm/answer 的 resume 入参（ainvoke(None) / Command(resume=)）经 API 层 WP-26 调用，Runner.start 已预留 event 参数，WP-26 需把 confirm/answer 事件接到 Runner；回退派生新 run（start_new_run）属 WP-24；agent_config 当前默认 {}，WP-25 智能体能力落地后从绑定 agent 读取
- 下个包起步点：**WP-23** Reaper + 启停序列（dd §6.5：lifespan 6 步序列、reap_on_startup 陈旧 running/cancelling→failed(INTERRUPTED_BY_RESTART)、优雅关闭取消在飞任务等 30s；现有 lifespan 已有迁移/DB/GraphRegistry 接线可直接扩展）

### [WP-21] EventBus + SSE — done（2026-09-27）

- 状态：done
- 交付物：新建 [runtime/bus.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/bus.py)——`EventBus(events: EventDAO)`（`emit(task_id,type_,payload)->int`：per-task `asyncio.Lock` 内"落库→投递"原子；`subscribe(task_id,after_id=0)->AsyncIterator[EventRow]`：锁内"注册队列→list_after 回放"，回放后释放锁进入实时 `await q.get()`，`finally` 反注册；队列满 `_QUEUE_MAXSIZE=1000` 时 `put_nowait` 抛 QueueFull 记 warning 丢实时帧但 DB 不丢）+ `Emitter(bus,task_id)` 闭包 task_id 注入 `TaskContext.emit`；新建 [api/events.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/events.py)——`events_endpoint`（抽到模块顶层以便直测）+ `make_events_router()`，`GET /api/v1/tasks/{task_id}/events`，`_resolve_after(after_param,last_event_id)`（query 优先→header→非法/负值归 0）、`_serialize(row)`（`id/event/data` 三段+空行）、首帧 `yield b": ping\n\n"` 强制首字节刷新、`_ping_wrapper` 用 `asyncio.wait({task},timeout=15)` 发 15s 心跳、StreamingResponse headers（Cache-Control:no-cache / Connection:keep-alive / X-Accel-Buffering:no）；修改 [runtime/context.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/context.py)（`AppContext.bus` 类型 `EventBus|None`、`DAOs.event` 字段）；修改 [main.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/main.py)（lifespan 构造 Database→EventDAO→EventBus 存 `app.state.bus/db`，finally db.close，挂载 events 路由）；新增 tests/test_eventbus.py（22 用例）
- 验收：`pytest tests/ -W error` **562 passed**（540+22）。EventBus 侧（8）：emit 落库返 id、广播到订阅者、回放 after_id、**闭窗不重不漏**（并发 emit 期间订阅恰好一次，数学保证：落库早于查询→走回放且队列未注册不重；落库晚于查询→队列已注册走实时不丢）、多订阅者各得全量、队列满丢实时保 DB、生成器关闭反注册、Emitter 绑定 task_id；SSE 侧（8 端点 + 6 helper）：content-type/headers、after_event_id 回放、Last-Event-ID 头回放、query 优先、实时事件转发、15s ping 心跳、断连清理；helper：帧序列化（含中文 ensure_ascii=False）、游标解析 6 种边界
- 与设计偏离（均不碰冻结契约）：① 端点处理函数抽到模块顶层（原为 `make_events_router` 内嵌套），便于测试直接调用，路由注册改用 `add_api_route`；② SSE 测试直接调用端点函数并在当前事件循环内消费 `StreamingResponse.body_iterator`（绕过 HTTP 传输层），因 httpx 0.28 ASGITransport 对长连接流有缓冲挂起问题；③ `_ping_wrapper` 的 `pending` set 实际只含单个 stream task（当前实现每时刻仅一个待取 task），保留 set 结构供未来多流扩展
- 遗留与提问：`task_done`/`task_error` 事件由 Runner（WP-22）在图结束/异常时发射；EventBus 当前进程内单例（单 worker 模型，dd 约束），多进程部署需换外部队列（不在本包）；`EventRow` 实时帧的 `created_at` 由 DAO.append 写入，回放帧与实时帧结构一致
- 下个包起步点：**WP-22** Registry + Runner（dd §7：Registry 管理 graph_run 生命周期、Runner 启动/取消图并发射 task_done/task_error、消费 EventBus 接线节点 emit）

### [WP-20] coverage_check — done（2026-09-27）

- 状态：done
- 交付物：[domain.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/domain.py) 补 §2.6 `CoverageRow`（clause_id/object_type[point|case]/object_id/covered/evidence）/`CoverageMatrix`（rows/uncovered_clauses/supplemental_rounds/degraded）；[case_generate.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/nodes/case_generate.py) 抽出 `generate_case_batch`（检索→LLM→finalize→先文件后 DB 提交→trace 闭环的单批函数，原 worker 薄封装复用），`_batch_scope` 对空 story_id 虚拟单元返回 None（仅类型过滤，避免空白名单 filtered_scope 全裁）；新建 [graph/nodes/coverage_check.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/nodes/coverage_check.py)——纯函数 `build_virtual_points`（未覆盖条款→虚拟测试点 `pt-sup{round}-{seq}`，条款序确定性序号，story_id=""/source_entry_ids=[]）、`build_coverage_matrix`（active clause × point.clause_ids 点覆盖 + point 下 active case 例覆盖，evidence 取标题；obsolete 例/deleted 条款不计）、`matrix_summary`、`count_supp_rounds`（从 case_generate progress 读 sup 批最大轮号）；`coverage_check_node`（初始矩阵→崩溃确定性重建历史 sup 轮零 LLM→≤`coverage_max_rounds`（runtime_config，默认 2）轮 `generate_case_batch(batch_id=sup{round})` 后重算→达上限 degraded=true→artifact(coverage_check,v1,CoverageMatrix)→发 `coverage_ready{matrix_summary,warnings}`，轮边界查取消）；nodes/__init__ 导出；新增 tests/test_coverage_check.py（12 用例）
- 验收：`pytest tests/ -W error` **540 passed**（528+12）。覆盖 WBS 三态：① 全覆盖：零补充 LLM、warnings=[]、matrix_summary.covered_clause_count 对账；② 部分覆盖：sup1 一轮补齐（sup1 两行挂 pt-sup1-1/2、batch_id=sup1、提示词含虚拟点 ID、progress 挂 case_generate artifact、mq+rr+生成 3 调用）；③ 达上限：两轮空产出→degraded=true、uncovered 保持、coverage_ready warnings=[{reason:uncovered_after_max_rounds,uncovered_clauses,max_rounds}]（实测第二轮 rerank 命中 run 内缓存零调用：kinds 计数 case=2/mq=2/rerank=1）。另覆盖：纯矩阵点/例行与 evidence、obsolete 例与 deleted 条款排除、count_supp_rounds 前缀判别、runtime_config coverage_max_rounds=1 覆盖、**sup1 提交后崩溃重建**（progress 有 sup1→重建虚拟点零 LLM，仅跑 sup2 共 3 调用且最终全覆盖）、同 run coverage artifact 终态回放零 LLM、缺 clauses/point_plan/case_generate artifact 三类报错；全五节点注入 build_graph 编译通过
- 与设计偏离（均不碰冻结契约，文件头 docstring 复述）：① 矩阵只落 covered=True 命中行（对象存在才有行），未覆盖仅经 uncovered_clauses 表达——CoverageRow.covered 字段保留供 review_export 后续承载失效/人工行；② 虚拟点 story_id=""/source_entry_ids=[]，补充用例正常落 MD/testcase 行（point_id 挂虚拟点，评审列表 WP-27 可见），检索 scope=None 不做归属裁决；③ 补充批 progress 随 commit_case_batch 落在 **case_generate** artifact（"走同一写入路径"），coverage artifact 只存终态矩阵、自身无批次/progress；④ coverage_max_rounds 读 runtime_config，引导行缺失/值非法/负值退回默认 2
- 遗留与提问：`coverage_ready` 现直接走 ctx.emit（EventBus 落库+广播与 SSE 回放属 WP-21，事件名/payload 已按 §10.4 定型可直接接线）；图正常结束后 task→completed 与 task_done 事件由 Runner（WP-22）负责；虚拟点补充用例在 CP 修订（WP-24 回退）作废 PointPlan 版本时的级联策略随回退协议统一处理
- 下个包起步点：**WP-21** EventBus + SSE（dd §6.2 §10.4：先落库后广播+订阅闭窗、GET /tasks/{id}/events、Last-Event-ID/after_event_id 回放、ping；coverage_ready/batch_progress/node_* 等事件发射点已在节点与 batch.py 就位）

### [WP-19] case_generate + 用例批次提交协议 — done（2026-09-27）

- 状态：done
- 交付物：迁移 [003_testcase_batch_id.sql](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/migrations/003_testcase_batch_id.sql)（testcase 加 batch_id 列+索引，sweep 维度）；[models.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/store/models.py) CaseRow 加 batch_id、TestcaseDAO.put_batch 含 batch_id + 新增 sweep_stale_idem（同 task+version+batch_id 未产出行置 obsolete）+ list_by_batch、ArtifactDAO.attach_case_ids（回填 progress.result_ids）；[runtime/context.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/context.py) DAOs 加 testcase/trace 字段；新建 [graph/nodes/case_generate.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/nodes/case_generate.py)（纯函数 build_case_intent/finalize_cases[case_id=uuid5(CASE_ID_NS, f"{task}|{v}|{batch}|{point}|{seq}") 确定性赋值、entry_ids 白名单过滤+degraded] + commit_case_batch[先文件后 DB：write_case 原子写→immediate_tx 内 put_batch+sweep_stale_idem+attach_case_ids] + case_generate_node[CP2 后取 point_plan→按 5 点/批 run_in_batches→每批 passage 档检索+LLM+finalize+commit+trace 闭环→回填 artifact summary]）；nodes/__init__ 导出；新增 tests/test_case_generate.py（10 用例）
- 验收：`pytest tests/ -W error` **528 passed**（518+10）。覆盖：① finalize 确定性 case_id（同输入恒等、不同 batch_id 不同）+ 白名单剔除 ghost/ghost2 + degraded 记 `case_generate.source_whitelist/entry_not_injected:{eid}/drop_source_entry` + 缺 point 允许空 + 空 title/空 steps 报 LLMBadOutput + 非法 priority→P1；② commit_case_batch 写文件+行、重放幂等（INSERT OR IGNORE 无重复行、同 hash 无重复文件）、sweep_stale_idem 把上一轮孤儿行置 obsolete 且正常行保持 active；③ case_generate_node 端到端 artifact(case_generate,v1,summary{case_count,case_ids})+testcase 3 active 行+progress+trace 闭环、同 run 全部 done 批零 LLM 重放、缺 point_plan 报错
- 与设计偏离（均不碰冻结契约，文件头 docstring 复述）：① LLM 输出采用 `by_point` dict（point_id→cases 数组）而非 prompt 分段文本，便于服务端按点对齐 seq；② case_generate 产物主要落 testcase 行，artifact 仅存 summary+progress；③ 检索 scope 同时传 link_ids/story_ids（从 state.link_plan 映射 story→link，无 link_plan 时仅 story_ids）；④ CASE_ID_NS 用固定 UUID（非配置项），保证跨 run 同 task+version+batch+point+seq 恒等（重跑同批同 ID）
- 遗留与提问：CP3 人工确认/评审状态迁移属 WP-22/26；regenerate 入口（dd §7.6）调 case_generate worker 单批重跑属 WP-26；case MD 文件解析（mistune）属 WP-05 已预留，本包未触
- 下个包起步点：**WP-20** coverage_check（dd §7.5④：程序化点覆盖/例覆盖矩阵，未覆盖条款→虚拟单元调 case_generate 单批兜底，达上限 degraded=true 告警降级）

### [WP-18] 批次执行器 + point_write — done（2026-09-27）

- 状态：done
- 交付物：[domain.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/domain.py) 补 §2.4 `TestPoint`/`PointPlan`；新建 [graph/batch.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/batch.py)（`BatchProgress` 进度管理、`start_cursor`、`deterministic_key`、`run_in_batches` 骨架——取消边界/done 跳过/started 重做/failure mark_failed、事件、progress 落盘）；新建 [graph/nodes/point_write.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/nodes/point_write.py)（纯函数 `order_stories_by_link`/`build_point_intent`/`finalize_point_plan`（point_id 确定性赋值 `pt-{story序号}-{批内位置}`、source_entry_ids 白名单过滤+degraded、悬空 story/空 title/非空 clarifications → LLMBadOutput）+ `point_write_node`（CP1 后取 link_plan→按 link 分组排序→建 active artifact→run_in_batches 驱动每批 passage 档检索+LLM+finalize+trace 闭环→回填 payload→state 增量 point_plan/current_stage_version/batch_cursor））；nodes/__init__ 导出；新增 tests/test_point_write.py（19 用例）
- 验收：`pytest tests/ -W error` **518 passed**（499+19）。覆盖 WBS 验收三测：① done 跳过（progress b0=done→worker 只跑 b1）；② started 重做（progress b0=started→重做并落 done）；③ cancel 边界（cancel_event 置位→b0 跳过、b1 边界抛 TaskCancelled）+ failure mark_failed。另覆盖：point_id 确定性与 story 序号、source 白名单剔除+trace.degraded 记 `point_write.source_whitelist/entry_not_injected:{eid}/drop_source_entry`、幻觉 entry 进 hallucinated_ids、artifact(point_write,v1,active,system,confirmed_by=null)+progress(unit_ids/result_ids) 落库、同 run 完整 payload 崩溃重放零 LLM、空 payload 崩溃中途重跑跳过 done 批、缺 link_plan 报错、priority 非法→P1
- 与设计偏离（均不碰冻结契约，文件头 docstring 复述）：① `idempotency_nonce` 改为由 `(task_id, run_id, node)` 经 uuid5 确定性派生（而非随机）——state.batch_cursor 仅在节点出口回写，崩溃时可能丢失，确定性 nonce 是同 run 崩溃恢复幂等的兜底（新 run→新 run_id→新 nonce，仍满足"随新 run 重新生成"）；② `run_in_batches` 显式接收 `state` 形参（TaskContext 上无 state 属性），返回 `(全部结果, 更新后 cursor dict)` 由节点写回 state.batch_cursor；③ 单元接受对象列表（非纯 ID），由 `unit_id`/`result_id` 回调取 id；④ story 序号取"按 link 分组排序后"的 1-based 位置（DD "story 在 LinkPlan 中的序号"未明确分组口径，按 §7.5③"按 link 分组排序"取分组后序位，CP2 修订 LinkPlan 不变则稳定）；⑤ point_write 无澄清流（仅 cp2 静态 gate），LLM 输出非空 clarifications 按 LLMBadOutput 失败；⑥ 知识块渲染复用 link_identify.render_knowledge_block
- 遗留与提问：case_generate（WP-19）复用 run_in_batches 时，worker 须按批 idem 做孤儿清理（sweep_stale_idem，dd §7.3 非确定性防护）——point_write 产物仅在内存聚合无 DB 行，暂不需要孤儿清理；point_write 批大小取 BATCH_DEFAULT_SIZE=5（stories 通常较少，单批覆盖多 link）；CP2 人工确认后的 artifact 状态迁移属 WP-22/26
- 下个包起步点：**WP-19** case_generate + 用例批次提交协议（dd §7.4③ §11.1 §7.3：复用 run_in_batches，单元=active 测试点，每批 N 次 LLM 生成+MD 文件原子写+testcase 行 INSERT OR IGNORE+uuid5 确定性 case_id+sweep_stale_idem 孤儿清理）

### [WP-17] link_identify — done（2026-09-27）

- 状态：done
- 交付物：[link_identify.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/nodes/link_identify.py)（新）：纯函数 `build_link_intent`（标题路径+anchor 拼接，dd §8.1）、`render_knowledge_block`（`[ID:eid]`+镜像类型/归属+passage，镜像空退化仅标签）、`render_user_message`（clauses/knowledge/前 500 字需求摘要三件套）、`finalize_link_plan`（白名单+临时 ID 改写+归属回填，返回 plan/degraded/asserted）+ `link_identify_node`（检索→LLM→澄清→finalize→trace 两写→artifact 落库→state 增 link_plan/current_stage_version）；[domain.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/domain.py) 补 §2.3 冻结模型 LinkRef/StoryRef/NewLinkSuggestion/LinkPlan；DAOs 增 `artifact`（ArtifactDAO，节点显式校验 None）；TraceDAO 增 `append_degraded`（read-merge-write degraded 列）；nodes/__init__ 导出；新增 tests/test_link_identify.py（14 用例）
- 验收：`pytest tests/ -W error` **499 passed**（485+14）。覆盖两条 WBS 验收口径——FakeLLM 脚本驱动：artifact(link_identify, v1, active, system, confirmed_by=null) 落库 + 图停 `("cp1_gate",)` + state.link_plan；幻觉 entry（ent_ghost/ghost-link/ghost-story）：降 hit=false、改写 new-link/new-story（新增序列独立编号）、trace.degraded 记 `link_identify.entry_whitelist/entry_not_injected:{eid}/downgrade_hit_to_new`、hallucinated_ids 回填、真命中进 referenced_ids。另覆盖：entry_version 由注入条目回填、镜像权威 ID 覆盖模型乱填值、story.link_id 经模型ID→最终ID 映射改写、悬空引用/缺段/坏 confidence/空 title/坏 JSON → LLMBadOutput、link.story_ids 按故事输出序聚合、澄清 interrupt/resume 二次生成（message answered=false→true、resume 后仍提问→失败、两轮 trace、rerank 缓存命中零调用共 5 chat_calls）、零注入全新增（rerank 零候选跳过）、同 run active artifact 崩溃重放零 LLM/检索且无 v2、daos.artifact 缺失/clauses 空显式报错、知识块 [ID]+归属双态渲染、json_schema 透传、模板 custom prompts_dir 由 agent_config 注入
- 与设计偏离（均不碰冻结契约，文件头 docstring 复述）：① 新增纯函数 `finalize_link_plan` 承担"临时 ID 改写+白名单+归属回填"；hit 项稳定 link_id/story_id 镜像可用时以 `mirror.meta(eid)` 为服务端权威，镜像空（§8.4 降级）才退回模型值；entry_version 一律注入条目回填（模型 schema 无此字段）；② 白名单降级并入本阶段同一 trace 行经 TraceDAO.append_degraded（§3.3 冻结三方法外增量，WP-16 先例）；③ "需求前 N 字摘要"N 取模块常量 `_REQ_SUMMARY_CHARS=500`（S3 标定前占位）；④ 崩溃重放：同 run 已有 active artifact 时节点直接回放 payload（零 LLM/检索，防重复版本）；⑤ 恢复轮澄清凭据复用 intake 范式（clarification_qa message payload.node=="link_identify"，从 intake 复用私有 `_parse_payload`）；⑥ assert 项额外渲染 `[ID:eid]` 标签随 raw_json 进 close_retrieval_trace，使裸 entry_id 字段能进入 referenced/hallucinated 对账
- 遗留与提问：恢复轮澄清后重跑检索管线（rerank 靠缓存零调用，multi_query 仍调一次）——是否复用首轮 outcome 待 WP-18 批次执行器统一权衡；CP1 人工确认后的 artifact 状态迁移（confirmed_by/superseded）属 WP-22/26；`new-link-n/new-story-n` 编号仅在单版 plan 内连续，CP1 修订生成 v2 时重新编号，与 v1 不保证跨版稳定（修订版 supersede 旧版，消费方只看 active）
- 下个包起步点：**WP-18** 批次执行器 + point_write（dd §7.3 §7.5③：批次执行器驱动 point_write 档检索+写入；retrieve_pipeline 支持 scope={"link_ids","story_ids"} 与 batch_id 已就绪，point_write 节点须照本包范式 finalize/artifact/trace 两写）

### [WP-16] intake 条款切分 — done（2026-09-27）

- 状态：done
- 交付物：新建 [graph/nodes/](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/nodes/intake.py) 包——[intake.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/nodes/intake.py)：`split_clauses` 纯函数（ATX `##`~`######` 切分、围栏/引用块标题忽略、clause_id=h2-1-h3-2 同级计数、title_path 链、anchor 前 32 字、text_hash=sha1(LF 规范化)、UTF-8 字节偏移；无有效标题→`root` 兜底）+ `ClauseSpan`（ClauseRef+偏移运行期扩展，不入 DB）+ `intake_node`（切分→缓存文件+task.clauses 落库→歧义检测→`interrupt()` 挂澄清→恢复轮回填 answers）；FileStore 增 `save/read_clauses_cache`（requirement.clauses.json 原子写）；TaskDAO 增 `update_clauses`、MessageDAO 增 `list_by_task/update_payload`（增量方法）；runtime/context.py 落 `DAOs` 组（task/message）+ `TaskContext.agent_config`；新增 tests/test_intake.py（18 用例）
- 验收：`pytest tests/` **485 passed**（467+18），`-W error` 三遍全绿。覆盖验收口径"重算 clause_id 稳定"（同文档重复切分全等 + 恢复轮 chat_calls 不增——问题取自挂起凭据而非重跑 LLM）与"无标题文档 root 兜底"（空文档/仅 H1/纯段落三态）；另覆盖：多级 id/title_path、不同父同名子节序号重计、围栏/引用块标题忽略（归属所在条款）、偏移与 read_clause 对拍（中文 UTF-8、去尾空白）、ATX 闭合序列 `## foo ##`、`#`/7 个 `#` 非边界、h2→h4 跳级、anchor 32 字、无歧义直通 cp1_gate、歧义 interrupt（payload+message answered=false+切分先行落库）、resume 后 answers 回填+图到 cp1、interrupt 前崩溃重入（凭据预置→零 LLM 重新挂起）、ambiguity_check=false 零调用、LLM 坏输出两次→LLM_BAD_OUTPUT 节点失败且图不前进、daos 缺失显式报错、缓存删后重算恒等、task.clauses 中文 JSON 往返
- 与设计偏离（均不碰冻结契约）：① **澄清挂起凭据**（dd §7.5① 未写恢复机制）：挂起前问题落库为 message(role=assistant, kind=clarification_qa, payload={"questions","answered":false})——兼作会话可见提问记录与恢复轮凭据，节点凭"未答复 message"跳过 LLM 直接 interrupt()（避免恢复轮重跑 LLM 导致问题漂移/恢复失败）；answer API（WP-26）只需写 QA message + `Command(resume=[{"id","answer"}])`，回填由节点完成；② §7.4① 口径"标题路径+anchor"→ task.clauses 存全量 ClauseRef 序列化（superset，link_identify 消费 title_path）；③ 任务态配置经 `TaskContext.agent_config`（Runner WP-22 从绑定智能体解析注入，含 prompts_dir/ambiguity_check）；④ DAOs 组仅 task/message 两 DAO（WP-17+ 按需扩）；⑤ 前导段（首个有效标题前内容，含 H1）不入任何 clause——由 §7.4①"需求前 N 字摘要"通道覆盖；级联影响：`#` 行不是边界，归属所在条款正文
- 遗留与提问：WP-26 answer API 须按 resume 值契约 `list[{"id","answer"}]` 构造（question id 由节点分配 q-n）；失败重试若重跑 intake 会重新触发歧义 LLM（答复只在消息历史，不在 LLM 输入）——用户可能被再次提问，是否将已答复 QA 注入歧义检测输入待 S5/用户裁决；DAO 增量方法与 TaskContext.agent_config/DAOs 属新增契约面，WP-22 Runner 接线时对齐
- 下个包起步点：**WP-17** link_identify（dd §7.5② §2.3 §20.3：子图 index_line 档检索→LLM LinkPlan→临时 ID 改写+白名单校验→artifact 落库；intake_node 已示范 ctx.daos/agent_config/interrupt 消费范式，检索走 retrieve_pipeline(scope=确认集合,stage_version) 并在生成后 close_retrieval_trace）

### [WP-15] build_graph/wrap/gate — done（2026-09-27）

- 状态：done（SP-2 已 GO，走图原生路径）
- 交付物：新建 [graph/state.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/state.py)（TaskState 全字段 total=False，§7.1）；新建 [graph/wrap.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/wrap.py)（`wrap()`：config.configurable.ctx 注入 TaskContext、节点前取消检查、node_start/node_end 事件（stage_version 取自 state、batch_id 节点级恒 None）、TaskCancelled/GraphInterrupt/AppError 穿透、未知异常包 INTERNAL+trace_id 日志、非 dict 返回兜底；`gate_node()` 纯透传；NodeFn 契约=`(ctx, state)->dict`）；新建 [graph/main_graph.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/main_graph.py)（`build_graph(checkpointer=None, *, nodes=None)`：§7.1 八边拓扑、interrupt_before 双 gate、未注册阶段装占位节点 NodeNotImplemented）；constants.py 增 GATE_CP1/GATE_CP2；runtime/context.py TaskContext 增 `cancel_event` + `async cancelled()`；graph/__init__ 导出；新增 tests/test_graph_build.py（16 用例）
- 验收：`pytest tests/` **467 passed**（451+16），`-W error` 三遍全绿。验收口径"图编译通过；断点位置断言"：静态断言 `interrupt_before_nodes==(cp1_gate,cp2_gate)`+八边集合；运行期 AsyncSqliteSaver 下三段 ainvoke 断在 cp1/cp2/END；另覆盖事件契约（start/end 成对、gate 无事件、latency_ms 非负）、ctx 同一性注入、取消前置/节点内 TaskCancelled、AppError 穿透、RuntimeError→INTERNAL（cause 链保留）、wrap 内 interrupt()+Command(resume=) 正常、占位节点执行→INTERNAL、emit=None 静默
- 与设计偏离（不碰冻结契约）：① 业务节点函数签名定为 `(ctx, state)->dict`（dd 只写 RunnableConfig 透传，wrap 内层屏蔽 config，WP-16~20 照此实现）；② 节点注册经 `build_graph(nodes=...)` 注入而非 dd 伪码的直接 import（节点 WP-16+ 才存在；生产侧 GraphRegistry WP-22 统一传入）；③ TaskContext 增 cancel_event 字段承载 §6.4③ 取消信号（daos/emit 之外的第三个 WP 延后字段，Runner WP-22 置 event）
- 关键工程教训（已写入 wrap.py 模块头）：LangGraph 1.2 节点包装器**不能用 functools.wraps**（按 __wrapped__ 探测签名会漏注 config）、config 参数**必须标注 RunnableConfig 本体且模块不能加 future annotations**（字符串化注解触发 UserWarning 并拒绝注入，-W error 下直接失败）；未在 TaskState 声明的返回键会被图静默丢弃（测试标记只能用冻结字段 clauses）
- 下个包起步点：**WP-16** intake 条款切分（dd §7.5① §4.3；ATX 切分+clauses 缓存文件+歧义 interrupt/answer 恢复；节点以 `nodes={STAGE_INTAKE: intake_node}` 注入 build_graph，其余阶段保持占位）

### [SP-2] LangGraph interrupt/派生 thread 验证 — done / **GO**（2026-09-27）

- 状态：done，结论 **GO**——WP-15 走图原生路径（`interrupt()`+`Command(resume=)`+fork），run_from_stage 仅按 dd §7.6 预留接口不实现
- 锁定版本：langgraph **1.2.12** / langgraph-checkpoint **4.2.0** / langgraph-checkpoint-sqlite **3.1.1**（升级时 test_sp2_langgraph.py 自动回归）
- 交付物：[tests/test_sp2_langgraph.py](file:///Users/test/Documents/python_project/testerAgent/server/tests/test_sp2_langgraph.py)（7 用例，镜像 §7.1 拓扑：intake/link_id 内 interrupt() + cp1/cp2 双 gate）；`pytest tests/` **451 passed**（444+7），`-W error` 三遍全绿
- 四验证点实测：① async 节点内 `interrupt(payload)` 挂起，payload 经 `state.tasks[0].interrupts[0].value` 取，`next` 为节点本身；② `Command(resume=ans)` 恢复，resume 值即 `interrupt()` 调用返回值，多轮（intake→link_id）正常；③ gate 用 `compile(interrupt_before=[...])`，到达断一次，`ainvoke(None)` 放行；modify 在暂停位 `aupdate_state(cfg, values)` 后 `ainvoke(None)`，下游读到修订 payload；④ 回退 fork：新 thread_id + `aupdate_state(new_cfg, entry_state, as_node=目标阶段前驱)` → `next=目标阶段`，重跑经真实边重新到达 gate 时 interrupt_before 照常生效；不拷贝 checkpoint、不继承旧 thread 历史、旧 thread 不受影响
- 生产发现（WP-15/22/23 照此实现）：① saver lifespan 构造 `aiosqlite.connect(path, check_same_thread=False)` → `AsyncSqliteSaver(conn)` → `setup()`（幂等），关闭时关连接；② 跨进程恢复实测成立：全新 saver 连同一 checkpoints.db + 同 thread_id，暂停态完整可续（WP-23 重启恢复前提）；③ 图已结束时 `Command(resume=...)` 为**静默 no-op 不报错** → answer API 必须应用层先校验 waiting_input 状态（dd 状态机本就要求，此处坐实）；④ `as_node=gate 自身` 合成的挂起位不再触发 interrupt_before，要重跑某阶段必须 as_node 取其**前驱节点**
- 与设计偏离：无（全部按 dd §7.1/§7.6/§11.2 语义验证）；唯一措辞细化：dd 派生 thread 写法 `f"{id}::run{n}"` 实测 thread_id 无字符限制，冒号方案可用
- 下个包起步点：**WP-15** build_graph/wrap/gate（依赖已全部满足；constants.py 已由 WP-13 提前落地；state.py/main_graph.py 按本记录配方装配，checkpointer 在 AppContext 组合根建一次）

### [WP-14] prompts 包 — done（2026-09-27）

- 状态：done
- 交付物：新建 [prompts/](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/prompts/) 包——内置模板目录（7 个 .md：system.shared / intake.ambiguity / link_identify.main / point_write.main / case_generate.main / retrieve/multi_query / retrieve/rerank，文件头 YAML 声明 version="2026-09-26.1"）+ [loader.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/prompts/loader.py)（PromptLoader：自定义目录覆盖内置、文件头 version 解析、缓存、load_prompt/prompt_version 快捷函数）；ops.py / ops_b.py 内联提示词迁至模板文件（multi_query/rerank system+user 模板拆分渲染）；pipeline.py INLINE_PROMPT_VER 从 "inline-v1" 占位换为 `prompt_version("retrieve/multi_query")` 真实版本；新增 tests/test_prompts.py（21 用例：模板加载/版本读取/自定义覆盖/文件头解析/缓存/边界）
- 验收：`pytest tests/` **444 passed**（基线 423+21），`-W error` 三遍全绿。覆盖验收口径"覆盖顺序与版本号读取单测"（自定义目录同名覆盖内置、内置回退、version 从文件头 YAML 解析、单双引号剥除、缺文件头/缺 version 抛错）
- 与设计偏离（均不碰冻结契约）：① dd §7.4 文字路径 `server/prompts/` → 落 `server/tester_agent/prompts/` 包内（随包分发，与 WP-02 eval 落点先例一致）；② multi_query/rerank 模板内 system+user 合并为单文件（dd §20.6 未分文件，ops 内 split 渲染，避免一个节点两个模板文件的管理开销）；③ loader 未引入外部 YAML 库，正则解析简单字段（version/node），避免依赖膨胀
- 遗留与提问：eval smoke 已可经 `prompt_version` 联动模板版本（dd §20.7"改模板即跑回归"）；system.shared 等 4 个生成类模板尚未被节点消费（WP-16~20 接入）；模板变更升 version 的纪律需在 code review 中把关
- 下个包起步点：**WP-15** build_graph/wrap/gate（SP-2 未做，注意阻塞；若 SP-2 no-go 则按 run_from_stage 预留口实现，dd §7.1 §1.3）；或 WP-16 intake（依赖 WP-15）

### [WP-13] eval smoke 管线 — done（2026-09-27）

- 状态：done
- 交付物：新建 [graph/constants.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/constants.py)（dd §1.3 阶段常量全量 + §8.1 RETRIEVAL_PRESETS 三阶段预设——提前落地，WP-15 直接 import）；新建 [eval/](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/eval/run.py) 包（fixtures.py：knowledge/requirements fixture 落型加载，全字段 extra=forbid；runner.py：`run_case` 单用例执行——tmp 库 + FakeReMeReader/FakeLLM 脚本回放 + retrieve_pipeline + close_retrieval_trace + 五指标计算，aux usage 从 snapshot 行读回顺带验证 §8.3 链路；run.py：CLI `python -m tester_agent.eval.run --preset smoke [--stage] [--config-diff] [--threshold-inject-hit]`，基线/对比两张表 + hallucinated 脚注 + verdict）；新建 fixtures（knowledge/core.json 14 条 5 类+索引；requirements/ 4 例覆盖三阶段，含 s2 词面未召回、x99 幻觉、b1 注入未引用等判别力场景）；新增 tests/test_eval.py（18 用例）
- 验收：`pytest tests/` **423 passed**（基线 405+18），`-W error` 三遍全绿。验收口径"CLI 跑冒烟集输出指标对比"真机实测：基线表 mean recall/inject 0.938（01_link s2 未召回 0.750、04 reference 0.500 均符合手工核算）；`--config-diff '{"inject_limit":1}'` 输出 `0.750->0.250` 对比单元格、mean inject_hit Δ=-62.5pt → verdict REGRESSION + 退出码 1；中性 diff（recall_topk 45）退出码 0；坏 JSON/未知键/未知预设退出码 2；--stage 过滤生效；同一用例两跑指标逐字段相等（确定性）
- 与设计偏离（均不碰冻结契约）：① dd 文字路径 `server/eval/run.py` → 落 `server/tester_agent/eval/` 包内（WP-02 同款落点先例，随包分发、`python -m` 可跑）；② clause_coverage 用 fixture 声明的 entry→clauses 映射计算（覆盖矩阵 WP-20 落地前的冒烟期代理口径，WP-20 后可换成真实矩阵）；③ 生成侧为 FakeLLM 回放 fixture 期望产物文本（dd §15.3 "期望产物"的落法）——指标度量检索/注入/引用闭环接线，不度量真实模型质量，黄金集（S7）到位后替换；④ 阈值判定口径取 mean inject_hit（dd 只写"inject_hit −5pt"，未指定单用例还是均值）；⑤ tests/fakes.py 非包模块，eval 经 sys.path 挂接导入（dd §15.3"eval 与单测共享 FakeReader"的落法）；⑥ constants.py 从 WP-15 切片提前（只常量与预设，build_graph/state 仍归 WP-15）
- 遗留与提问：prompt 模板版本敏感性（dd §20.7"改模板即跑回归"）待 WP-14 后把 INLINE_PROMPT_VER 换成真实版本即可联动；wall_ms 列仅展示不参与阈值（抖动大）；preset 目前只有 smoke=全量，黄金集后扩展
- 下个包起步点：**WP-14** prompts 包（6 模板 v1 + loader 覆盖顺序 + version 读取，dd §20；multi_query/rerank 内联提示词迁出，pipeline.INLINE_PROMPT_VER 换真实版本号）；或 WP-15 build_graph（SP-2 未做，注意阻塞）

### [WP-12] retrieve_pipeline/trace/snapshot — done（2026-09-27）

- 状态：done
- 交付物：新建 [runtime/context.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/runtime/context.py)（dd §6.1：`AppContext` 组合根 + `TaskContext`，RetrievalCache 惰性导入破模块环）；新建 [graph/retrieval/pipeline.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/retrieval/pipeline.py)（`retrieve_pipeline`：start_run→plan_fallbacks→六算子序→TraceDAO.append→快照三档；`close_loop`/`ClosedLoop`/`parse_ids`（§8.6，字符 bigram Jaccard<0.08 标 weak）、`close_retrieval_trace` 两写回填、`funnel_counts`）；ops.py `parallel_recall` 增 recall 缓存接线（cache/task_id/workspace_id/kb_id，命中路 latency=0，纯增量）；ops_b.py `RetrievalOutcome` 增 trace_id/snapshot_id；__init__ 导出；新增 tests/test_pipeline.py（23 用例）
- 验收：`pytest tests/` **405 passed**（基线 382+23），`-W error` 三遍全绿；两种导入序（runtime 先/pipeline 先）均无环。覆盖验收口径"full 档按偏移读单行"（逐行 read_snapshot_line 还原、偏移连续 off=prev(off+len+1)、UTF-8 字节长度含中文对账、passage 仅在文件）与"三档行为表"（off 零行零文件/meta DB 行无 path/ full 带 path+offsets；三档 DB items 均剥离 passage）
- 与设计偏离（均不碰 §2.8 冻结字段）：① 管线增 keyword-only `scope`（§8.2 meta_filter 必需的确认 link/story 集合，冻结签名无通道）与 `stage_version`（trace/snapshot 行必需，默认 1）；② trace `query_variant` 落 `{"queries":[各路]}`（§8.3 要求存各路，Row helper 单 QueryVariant 容纳不下）；③ ordering_strategy 按 §8.5 明确许可并入 latencies JSON；④ prompt_template_ver 用 `inline-v1` 占位（WP-14 前提示词内联）；⑤ AppContext graphs/bus/registry 以 Any 占位（WP-15/21/22）、新增 retrieval_cache（§6.1 未列，默认工厂）；TaskContext daos/emit 延后（字段顺序后置）、新增 mirror 字段（WP-08 交接单"管线方持有"）；⑥ `truncated` 仅指 assemble 预算截断，rerank_cutoff 只体现在候选 drop_reason
- 遗留与提问：下游 WP-17/18/19 调管线须传 scope/stage_version，生成后调 close_retrieval_trace；mirror 建议在 run 启动（WP-22 Runner）构造一次、管线每调 ensure_fresh（TTL 内零开销）；WP-28 playground 强制 level=off
- 下个包起步点：**WP-13** eval smoke（fixtures 假知识库+构造需求+run.py+5 项指标，dd §15.3，直接复用 retrieve_pipeline 与 funnel_counts）；WP-14 prompts 包亦可随时开

### [WP-11] 检索算子 B + 缓存 — done（2026-09-27）

- 状态：done
- 交付物：domain.py 补 `RetrievalConfig`（§2.8 冻结域对象，此前漏落）；新建 [graph/retrieval/cache.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/retrieval/cache.py)（`RetrievalCache` 进程内 LRU 容量 512，内部键=task_id+业务键实现分区，`start_run` 同 run 幂等/异 run 清该 task 分区，`recall_key` types 排序归一、`entry_key` 含 entry_version 即 §8.5"版本校验"落法、`intent_hash` sha1 前 12 位）；新建 [graph/retrieval/ops_b.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/retrieval/ops_b.py)（`rerank`：>30 按主通道分桶每桶召回分 top10 一次 LLM 调用、LLM 失败/坏输出整桶回规则分记 degraded（llm_failed/llm_bad_output，fallback=rule_score）、部分覆盖不记降级、limit 外 kept=False+rerank_cutoff、空白 intent 跳过 LLM；`passage_extract`：index_line 不拉正文（镜像 meta 取 summary，缺省退化标题）、passage 走 get_entry+本地结构化切分（ATX 标题→空行段落→800 字固定窗口）、lexical 重叠选 top1~2 段按原文序拼接、单候选失败标 error 不阻他路、并发信号量同 recall；`assemble`：token 估算集中（estimate_tokens=cjk 字符数+ascii 词数×1.3 取 ceil）、防御性 dedup（精确归因同键低分候选，孤儿 item 不误标）、inject_limit/token_budget 尾部截断标 budget_cut、anchor-v1 锚点排序+position 重排、outcome 快照 sink.degraded/latencies；`RetrievalOutcome` 落本模块）；`RetrievalTraceBuilder` 增 `ordering_strategy` 属性（assemble 写入 anchor-v1，WP-12 记 snapshot）；新增 tests/test_retrieval_ops_b.py（53 用例）
- 验收：`pytest server/tests/` **382 passed**（基线 329+53），`-W error` 三遍全绿。覆盖验收口径"rerank_cutoff/budget_cut 正确"（cutoff 精确断言+error 行透传不被覆盖、budget 边界 4+4=8 恰好保留/超限尾部截断）与"锚点位置断言"（n=1..6 参数化，5 项全序 [a,c,e,d,b]）；另覆盖 >30 分桶（3 通道 3 次调用、每调用恰好 10 条、桶内召回分 top10 选择、单桶失败仅该桶降级）、规则分回退与类型权重注入、缓存命中零 LLM 调用/版本换键 miss、index_line 零 get_entry、结构化切分两段原文序、800 字窗口、get_entry 失败单候选标记、dedup 防御两分支、run 分区清空/LRU 逐出/跨任务隔离、rerank→extract→assemble 链路串测
- 与设计偏离（均不碰 §2.8 冻结字段）：① 三算子沿 WP-10 例增 keyword-only `sink`，rerank/passage_extract 另增 `cache`+`task_id`（§8.5 缓存接入口，成对强制否则 ValidationError）；② passage_extract 增 `intent`（dd 签名未带但选段必需）与 `mirror`（index_line 的 summary 来源，不拉正文）；③ assemble 增 `cands`（budget_cut/dedup 回写与 score 排序必需，InjectedItem 无 score）；④ 规则分"×类型权重"dd 未给数值 → v1 恒 1.0，留 `type_weights` 注入点待 S5 标定；⑤ rerank 输出 schema 落 `{"scores":[...]}` 对象根（§7.4④ 文字为裸数组，但 §9.3 JSON 模式=DeepSeek json_object 强制对象根，与 multi_query 同例）；⑥ 每候选产出恰好 1 个 InjectedItem（1~2 段拼接），assemble dedup 为防御性；⑦ dedup 前置于截断（常态与 dd 文字序等价，防御分支避免重复条目白占预算）；⑧ 截断为尾部截断不做装箱跳项；⑨ passage_api 能力位算子层无消费点（§9.1 Protocol 无段落级 API），passage 形态恒 get_entry+本地切分；⑩ extract 阶段 position/tokens_est 为占位，assemble 统一重排/重算（口径集中）；⑪ RetrievalOutcome 是 §8.2 管线类型而非 §2.8 冻结域对象，落 retrieval 包不进 domain.py
- 遗留与提问：rerank/multi_query 提示词均内联（WP-14 prompts 包统一迁移）；规则分类型权重待 S5；RetrievalCache 的 recall 读写接线与 `start_run` 调用在 WP-12 管线（算子只消费 entry 级键）；assemble 的 outcome.degraded/latencies 取自 sink 快照，WP-12 若在同一 sink 上跑全管线则天然是全量
- 下个包起步点：**WP-12**（retrieve_pipeline 编排：消费六算子+plan_fallbacks+RetrievalCache.start_run，trace 两写回填 referenced/hallucinated/weak、snapshot 三档 off/meta/full 偏移、latencies/degraded/ordering_strategy 落库，dd §8.3 §8.6，验收"full 档按偏移读单行；三档行为表测试"）。WP-09 仍需 SP-1 先行，不阻塞本线

### [WP-10] 检索算子 A — done（2026-09-27）

- 状态：done
- 交付物：新建 [graph/retrieval/](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/graph/retrieval/ops.py)（ops.py：`multi_query` raw 恒在 index 0 + LLM 生成 keyword/rewrite 交错路，JSON 模式 schema 贴尾；`parallel_recall` 信号量限并发 gather、(entry_id, entry_version) 并集、最高分与 latency 取高分路、source_channel 排序逗号聚合 tag、单路失败填 error 候选垫底、CancelledError 原样传播、entry_version 缺失→本地 h- hash；`meta_filter` 类型过滤恒用 Candidate.entry_type、scope 镜像降级走 IndexMirror.matches、mirror=None 放行、error 行透传；`normalize_scope` scope 落型校验（键 link_ids/story_ids、空列表=合法空白名单）；`lexical_score` 本地词面重叠分（ASCII 词+CJK bigram，标题 3 倍权重）；`RetrievalTraceBuilder` 内存留痕（latencies/degraded/aux_usage/step_candidates，§8.3 trace builder 雏形，WP-12 扩展落库））；tests/fakes.py 增 FakeLLM（脚本化响应/异常注入、请求记录、固定 usage）；新增 tests/test_retrieval_ops.py（37 用例）
- 验收：`pytest tests/` **329 passed**（基线 292+37），`-W error` 三遍全绿。覆盖验收口径"单路失败不阻他路"（FlakyReader 单路注入失败→error 候选垫底且他路三候选完整、全路失败 2 error 行、CancelledError 穿透不包装）与"filtered_* 裁剪留痕"（filtered_type/filtered_scope 精确断言×镜像与非镜像分支、类型优先于归属、error 行不重复裁决、就地修改返回同列表）；另覆盖并集去重+channel 聚合序、raw 恒在/LLM 失败与坏输出 degraded（reason 区分 llm_failed/llm_bad_output）、部分成功不降级、变体去重（含与 raw 重复剔除）、n<2 与空白 intent 零 LLM 调用、caps 开关两态的服务端过滤行为差、topk、并发信号量峰值实测 1/2、scope 三种非法形状参数化、本地 hash 对拍、lexiscore 权重、链路串测（multi_query→recall→filter 三步 sink 留痕）
- 与设计偏离（均不碰 §2.8 冻结字段）：① 算子加 keyword-only `sink`（§8.2 只写"记 degraded"未给通道，按 §8.3 落为注入式 trace builder）；② parallel_recall 加 `scope` 形参——§8.4 将 metadata_filter 定义为"search 是否支持 types/scope"，能力可用时 scope 在 recall 服务端过滤（Candidate 冻结字段无 link/story 归属，客户端无法二次判定），mirror=None 时 meta_filter 对 scope 放行；③ meta_filter 加 `mirror` 形参承载 §8.4 镜像降级（镜像空→管线不传 mirror＝skip 全量进 rerank，degraded 由 WP-12 经 plan_fallbacks 记）；④ source_channel 并集落型为排序逗号拼接（dd 文字"列表"与冻结 str 冲突）；⑤ recall 分数来源：Entry 不带分且 §9.1 raw 适配层外不消费，落为 lexical_score 本地词面分（口径同 FakeReMeReader，兼作 WP-11 rerank 规则分基础）；⑥ error 候选 entry_type 以 LINK_INDEX 占位（冻结模型必填），面板以 error 字段区分；⑦ 镜像空降级时 meta_filter 仍做本地类型过滤（entry_type 恒可得，"全量进 rerank"仅指放弃需服务端能力的归属过滤）
- 遗留与提问：multi_query 提示词暂内联 ops.py（v1），WP-14 prompts 包可迁；recall 并发默认 4 对齐 §13.2 llm_concurrency，S5 标定后经管线注入覆盖；aux.calls 按"发起次数"口径（失败调用也计 1，token 未知不累加）
- 下个包起步点：**WP-11**（检索算子 B：rerank >30 分桶/passage_extract 结构化切分/assemble 预算+锚点排序+去重 + run 内 LRU，dd §8.2 §8.5；lexical_score 已就位可作 rerank 规则分，RetrievalCache 需 run 分区清空语义）。WP-09 需 SP-1 先行，不阻塞本线

### [WP-08] ReMe 契约/FakeReader/IndexMirror — done（2026-09-27）

- 状态：done
- 交付物：新建 [adapters/reme.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/adapters/reme.py)（ReMeCaps/Entry/IndexTree（IndexLink/IndexStory/IndexEntryMeta）/`@runtime_checkable` ReMeReader Protocol 冻结契约；`plan_fallbacks` caps 三态降级纯函数 + `local_entry_version`（h-+sha256 前12位）；`IndexMirror`（TTL 默认 3600s 对齐 runtime_config.index_mirror_ttl_min、clock 注入、asyncio 单飞双检、ensure_fresh best-effort 空镜像/旧树 stale 两分支、refresh 显式抛错、meta/known_link_ids/known_story_ids/matches 本地过滤原语）；`ReMeReaderFactory`（(target,kb_id) 缓存 + per-key 锁单飞、mode→builder 注册表、kb_config 校验、builder 失败不落缓存、`probe` 一次性探测返 CapsProbe）；新建 [tests/fakes.py](file:///Users/test/Documents/python_project/testerAgent/server/tests/fakes.py) 夹具级 FakeReMeReader（词面打分标题 3 倍权重、caps 关时忽略 types/scope 与剥离 version 字段、NotFound/KbUnreachable/自定义异常注入、LINK_INDEX 树派生/显式树深拷贝、计数器）；新建 tests/test_reme.py（59 用例）
- 验收：`pytest tests/` **292 passed**（基线 233+59），`-W error` 三遍全绿。caps 三态：全能力无降级；metadata_filter 缺 ×镜像空/非空两分支（skip 全量进 rerank / mirror 本地过滤）；entry_version 缺→本地 hash；passage_api 缺→拉正文本地切；全缺组合 degraded 顺序与 fallback token 断言；local hash 格式/UTF-8 向量对拍。镜像 TTL：TTL 内零重拉/到期边界（<60 不刷、=60 刷）/force/5 并发单飞仅 1 次拉取/首载失败空镜像不抛并可恢复/成功后失败保旧树标 stale/refresh 显式失败/空树视同 empty；matches 类型与归属（未知 entry 类型不判定保留、scope 白名单不可证实→filtered_scope）。FakeReader/工厂另覆盖打分排序 topk、caps 开关行为差、版本剥离不污染夹具、工厂缓存键 options 不入键、7 种非法配置参数化、未注册 mode→VALIDATION_BODY、builder 失败可重试、probe 不写实例缓存
- 与设计偏离（均不碰冻结签名）：① **IndexTree 为 dd 缺口补型**——§9.1 引用但全文无类定义，按其 docstring（title/一句话/entry_id/version/归属）+ tech-design §4.3（仅标题+一句话+类型+归属）补 IndexLink/IndexStory/IndexTree/IndexEntryMeta，待裁决；② §8.4 文字写 `capabilities()` 方法而 §9.1 冻结 Protocol 为 `caps` 属性——以 §9.1 为准，探测由工厂 builder 在构造期完成，reader 只暴露缓存 caps；③ degraded 的 reason/fallback 字符串 dd 未冻结（drop_reason 仅 filtered_type/filtered_scope 等为冻结值），落为稳定 snake token；④ 未注册 mode 落 VALIDATION_BODY 400（§14 无专门 code），WP-09 register("sdk"/"service")；⑤ scope dict 测试约定键 `link_ids/story_ids`（dd 只冻结 dict 类型），WP-10 meta_filter 算子正式落型；⑥ 本地 hash 算法 dd 未写，取与全项目一致的 sha256（UTF-8）
- 遗留与提问：①②待用户裁决（③~⑥为实现口径说明，非阻塞）。真实 SDK/HTTP 适配与 caps 探测在 WP-09（按 SP-1 结论）；IndexMirror 实例由任务启动/管线方持有（WP-12），TTL 从 runtime_config 注入；工厂当前无生产 builder，WP-09 前真实 mode 调用会得 VALIDATION_BODY
- 下个包起步点：**WP-10**（检索算子 A：multi_query/parallel_recall/meta_filter，直接消费 ReMeReader Protocol/FakeReMeReader/plan_fallbacks/IndexMirror.matches，dd §8.1 §8.2 §2.8，验收"单路失败不阻他路、filtered_* 裁剪留痕"）；WP-09 需 SP-1 先行。第一步读 dd §8.1 §8.2，算子保持纯函数式并以 fakes.FakeReMeReader 驱动

### [WP-07] LLMClient 韧性 — done（2026-09-27）

- 状态：done
- 交付物：新建 [adapters/llm.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/adapters/llm.py)（`Msg` TypedDict、`LLMResult`、`@runtime_checkable LLMClient` Protocol、`OpenAICompatLLMClient`：httpx.Timeout 连接/读分离、流式相邻 chunk asyncio.wait_for 60s 可配、429 尊重 Retry-After + 1s/2s/4s±25% 抖动最多重试 3 次、4xx 不重试、实例级信号量（排队不计超时）、JSON 模式 schema 贴末条 user 消息 + json-repair 结构校验 + 携错误文本重请 1 次、流式 stream_options.include_usage 取流尾 usage、`from_configs` 映射 config 表两行 JSON、`aclose` 供 lifespan 关停）；新增 tests/test_llm.py（32 用例，fake server = httpx.MockTransport 注入 AsyncOpenAI）
- 验收：`pytest tests/` **233 passed**（基线 201+32），`-W error` 三遍全绿。五场景全覆盖：429（Retry-After 精确睡眠 3.0s / 无头时退避区间断言 / 耗尽→RateLimitedError 带 details.retry_after）；5xx+ConnectError（耗尽→LLMUpstreamError，1+3 次尝试）；超时（ReadTimeout 重试成功 / 耗尽→LLMTimeoutError / 流式 chunk 间隔超时同路径）；4xx（400/401/403/404/422 参数化：仅 1 次请求零睡眠→LLMBadRequest）；坏 JSON（本地 json-repair 修复尾逗号零重请 / 纯文本→重请 1 次成功且重请消息含错误文本 / 两次皆坏→LLMBadOutput / 缺 required 键触发重请 / 流式+JSON 重请链）。另覆盖流式 chunk 拼接与流尾 usage、取消在 chunk 等待点即时生效不包装不重试（CancelledError 原样传播）、信号量并发上限实测 ≤2、from_configs 两配置源映射与 model_config.timeout 优先、配置缺项→LLMBadRequest
- 与设计偏离（均不碰冻结契约）：① tech-design D7 写"langchain-openai 兼容客户端"，实现改用 openai 官方 AsyncOpenAI 直连兼容协议——dd §17.2 翻译示例即 openai 原生异常，且 langchain 包装层内置重试与 §9.3 显式退避表冲突；openai 已是直接依赖。② dd §9.3 "Pydantic 校验"落为结构校验（必须为 JSON 对象 + schema 顶层 required 键存在）：pydantic 不支持按 JSON Schema dict 校验，类型级校验按 D6 由调用方节点把 content 解析进各自模型完成；JSON 重请计入 LLMResult.retries（额外请求次数语义）。③ 顺手修复存量缺陷：tests/test_db.py `_open_raw` 误用 sqlite3.Connection 当 CM（只提交不关闭）导致连接泄漏，本包新增测试改变 GC 时序后在 `-W error` 下暴露为 PytestUnraisableExceptionWarning；改为 contextmanager（提交+关闭），属测试设施修复不改生产代码
- 遗留与提问：① 流式与非流式由 stream_writer 是否非空区分（dd 协议唯一判据）；② WP-25 探活端点构造时若 model_config 为空将直接得 LLMBadRequest（构造期校验），端点需按 WP-06 交接单在边界自行映射 424；③ 重试期间日志为 debug 级且不含正文（§19.2 合规），usage/run_id 维度归因由 WP-12 快照链承担
- 下个包起步点：Wave 1 余 **WP-08**（ReMe 契约/FakeReader/IndexMirror，dd §8.4 §9.1：caps 三态降级 + 镜像 TTL 刷新）。第一步读 dd §8.4 能力探测与 §9.1 ReMeReader Protocol

### [WP-06] 异常体系与错误信封 — done（2026-09-26）

- 状态：done
- 交付物：扩 [errors.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/errors.py)（§17.1 全量 16 个 AppError 子类 + `TaskCancelled`；`PathEscapeError` 从 workspace_files 收拢为唯一定义点；trace_id contextvar + `new_trace_id/current_trace_id`）；新建 [api/error_handling.py](file:///Users/test/Documents/python_project/testerAgent/server/tester_agent/api/error_handling.py)（TraceIdMiddleware、AppError/RequestValidationError/Exception 三 handler、`install_error_handling`，§10.1 信封）；main.py 接线；workspace_files 改导入并再导出 PathEscapeError；新增 tests/test_errors.py（53 用例）
- 验收：`pytest tests/` **201 passed**（基线 148+53），`-W error` 同样全绿无 warning。覆盖"每类 AppError 的 HTTP 映射"（16 类参数化：status/code/retryable/信封四字段+details 保留）；5xx（LLMUpstream/Timeout/BadOutput/KbUnreachable）details.trace_id 必带且 == X-Trace-Id 头；4xx 信封不带 trace_id；Pydantic 缺字段/坏 JSON→VALIDATION_BODY 400（明细 loc 保留）；未知异常→INTERNAL 500 带 trace_id；中间件每请求唯一 id、contextvar 请求内可见/请求后复位、重复安装幂等；PathEscapeError 唯一定义+再导出身份断言；healthz 集成带头
- 与设计偏离（均不碰冻结签名）：① trace_id contextvar 与生成器置于 errors.py（dd §17.3 未指定模块；WP-22 run 级 trace_id 直接复用）；② 增响应头 `X-Trace-Id`（dd 未规定，支撑"从用户反馈定位日志"，200/4xx 也可对账）；③ 4xx 信封不带 trace_id（§17.3"可选"的从严读法）；④ 未 envelope 化 Starlette HTTPException（非 404 状态码在 §14 无冻结 code，不生造），WP-25 端点一律抛 AppError
- 遗留与提问：§14/§10.1 与 §17.1 的 HTTP 值有表面出入——LLM_UPSTREAM/LLM_TIMEOUT/RATE_LIMITED/KB_UNREACHABLE 的类属性已按 §17.1 冻结层次落 502/504/429/502；§10.1 的 424 限定"同步探活类接口"，WP-25 探活端点需在端点边界自行返回 424 JSONResponse（冻结 __init__ 无 status 覆盖参）。非阻塞
- 下个包起步点：WP-07（LLMClient 韧性，依赖 06 已满足）/WP-08（ReMe 契约层）均可开。第一步读 dd §9.3 §17.2 翻译示例，异常类已全部就绪直接 raise

### [WP-05] FileStore 与 MD/快照文件 — done（2026-09-26）

- 状态：done
- 交付物：新建 `store/workspace_files.py`（~620 行：slug/路径纯函数、MD 渲染与 mistune token 树解析、canonical/hash、`FileStore` 需求/用例读写+乐观锁覆盖+原子写+路径防护+清单+`soft_cleanup`、同步 `SnapshotWriter` JSONL 偏移读写）；`domain.py` 补 `CaseStep/CaseFileContent/FileRef`；`errors.py` 补 `FileConflict(FILE_CONFLICT,409)`；新增 `tests/test_files.py`（35 用例）
- 验收：`pytest tests/` **148 passed**（基线 113+35；无 warning）。覆盖 WBS 口径"提交崩溃文件侧行为"（monkeypatch os.replace 失败→目标缺失+`.tmp.{uuid}` 留存+恢复重提成功+保留期内不清/到期清理报告计数与字节）、"hash 对拍"（write_case 返回值 == 盘上 hash_of == 重算 read_case；外部追加/删文件被 hash_of 检出/NotFound；同内容重放返回同一 WrittenCase 且 mtime 不变）、"单行偏移读"（snapshot 两行 offset 连续 `o2=o1+l1+1`、JSONL 按 UTF-8 字节长度还原中文、requirement 字节切片条款）；另含渲染/解析往返（缩进子项与软换行续行两种预期写法、缺段宽容、坏 FM/YAML/缺 case_id/缺 H1→ValueError）、CRLF/末尾空行/FM hash 值不影响 hash、overwrite 连续三次覆盖自校验（见偏离⑤）、FileConflict 带 expected/current、坏 ws/task 段与 `../`/绝对 rel_path 全 PathEscape、清单按版本分目录且不含快照/tmp
- 与设计偏离（均不碰 dd §5 冻结签名）：① **路径基准裁决**：`WrittenCase.file_path`、read/overwrite/hash_of/list/read_snapshot_line/SnapshotWriter 的 rel_path 全部相对**任务目录**（`cases/v{n}/..`、`snapshots/..`，dd §11.3 与 list_case_files 同基准可直接对账）；仅需求 `FileRef.path` 相对 **data 根**（dd §2.9 注释）；② slug 未实现 dd §4.1 的 `-2/-3` 后缀——case_id8 前缀已保证同目录唯一，冲突不可能发生；③ FileConflict 提前落 errors.py（WP-06 只扩不重建，同 WP-03 先例）；④ `soft_cleanup` 只清遗留 `.tmp.*`，快照/提案保留期联动 DB 清理由 WP-29 `runtime/maintenance.py` 承担（dd §6.5/§11.3）；⑤ mistune 3.3.4 已删 AstRenderer，解析改用 `create_markdown(renderer=None)` 的 token 树；实施中发现 overwrite 逐次覆盖会吃掉 FM 后换行致二次覆盖 hash 失配，改为固定 `---\n` 边界重建（有连续三次覆盖回归测试）；⑥ parse 失败抛 **ValueError**（非 AppError），供 WP-19/27 调用方降级保留原文，不越界包成 HTTP 错误
- 遗留与提问：无；调用方注意 read_clause/snapshot 偏移一律按 **UTF-8 字节**（中文 3 字节/字，dd §4.3）；原子写 rename 失败刻意不删 tmp（与 kill -9 同构）
- 下个包起步点：Wave 1 余包 WP-06（异常体系：AppError 三子类+FileConflict 已就绪，补齐 §17.1 其余与错误信封）/WP-08（ReMe 契约层）均可开；WP-05 无下游阻塞。第一步先读 dd §17 与 errors.py 现状

### [WP-04] DAO 其余 + 分页 — done（2026-09-26）

- 状态：done
- 交付物：扩 `domain.py`（§2.1 补 ProposalStatus/MessageRole/MessageKind/EntryType；§2.8 新增 QueryVariant/Candidate/InjectedItem/DegradedStep）；`store/models.py` 追加 10 Row+10 DAO（workspace/agent/conversation/message/retrieval_trace/context_snapshot/task_event/review_record/kb_proposal/config），`_fetch_page` 加 `qualify` 参数支持 JOIN 消歧；新增 `tests/test_dao_rest.py`
- 验收：`pytest tests/` **113 passed**（本包新增 46；无 warning）。覆盖验收口径"分页无重无漏"（ws/agent-JOIN/conv/message/trace/snapshot/review/proposal 均 limit=2 走完全部 + 同时间戳 id tiebreak）、"workspace 强制过滤"（conv/message/proposal 跨 ws 不可见、trace/snapshot/event/review 跨 task 不可见、agent list_for_workspace JOIN 隔离）；另覆盖 ws 软删视同不存在 + include_deleted 逃生口 + update 哨兵不覆盖、agent bind 幂等/unbind/硬删级联 agent_workspace、event 自增 id/list_after 开区间边界+限量+任务隔离/purge_before 只删更旧、trace append→update_referenced 引用回填+stage/version 过滤、snapshot items/usage 等 JSON 往返、proposal 幂等键→VersionConflict/fail_count 递增保持 pending/mark_confirmed 终态/status 过滤、config 引导行读取与 model/runtime 互不覆盖、全部 JSON 列 `{}`/`[]` 默认值闭环、各 get/update 缺行→NotFoundError
- 与设计偏离（均不碰 §3.3 冻结签名）：① 冻结签名仅明确 Trace/Snapshot/Event 三 DAO，其余七类签名按 tech-design §5 端点表反推（list_*统一 `(owner, *, filters, cursor, limit) -> Page`，与 WP-03 同构）；② `WorkspaceDAO.get` 加 `include_deleted=False` kwarg（默认软删视同不存在不暴露存在性，审计/对账需取回时显式打开）；③ Agent 采用硬删（agent_workspace 双外键 ON DELETE CASCADE 由 FK=ON 实测级联），bind 用 INSERT OR IGNORE 幂等；④ `_fetch_page` 增 `qualify=""` 可选参，JOIN 查询传 `"a."` 给 (ts,id) 排序与游标谓词消歧
- 遗留与提问：无；review_record.testcase_id 有 NOT NULL FK，评审写入链路须先确保 testcase 落库（WP-27 注意）
- 下个包起步点：Wave 1 余包均可开（仅依赖 01，部分依赖 06）：建议 **WP-05 FileStore**（dd §4 §5：tmp→fsync→rename 原子写、用例 MD 渲染/解析/content_hash、SnapshotWriter 偏移读写、路径越界防护）；或 WP-06（errors 仅需扩不重建，AppError 三子类已在 WP-03 落地）/WP-08（ReMe 契约层）。第一步先读 dd §4 目录布局与 §5 原子写协议

### [WP-03] DAO 核心（task/artifact/testcase） — done（2026-09-26）

- 状态：done
- 交付物：新增 `tester_agent/domain.py`（§2 跨层 Pydantic 契约：5 枚举 + ClauseRef/RequirementRef/TraceRefs/Lineage/CaseRecord/ErrorInfo）、`tester_agent/errors.py`（§17.1 子集 AppError/NotFoundError/VersionConflict）、`store/models.py`（TaskRow/ArtifactRow/CaseRow + Row 工厂与转换 + Page/游标 + TaskDAO/ArtifactDAO/TestcaseDAO 全签名）；改 `store/db.py` 加 `aexecutemany`；新增 `tests/test_dao.py`
- 裁决补记（同日）：用户已裁决 ①RequirementRef 命名认可；②testcase 增 `created_at`——新增迁移 `002_testcase_created_at.sql`（schema_version=2：ALTER ADD COLUMN NOT NULL DEFAULT '' 后按 updated_at 回填存量行），dd §3.1/文首变更注记同步，CaseRow/put_batch/`_COLUMNS` 联动，用例列表游标时间列 updated_at→**created_at**（编辑/评审只动 updated_at，不串页）；WP-02 迁移测试与 CLI 断言随之更新（applied=[1,2]），新增存量 001 库升级回填用例
- 验收：`pytest tests/` **67 passed**（WP-03 新增 36；无 warning）；覆盖 CRUD、workspace 隔离、游标分页（5 条/2 页无重无漏、同时间戳 id tiebreak、status/version/review 过滤、**编辑后顺序仍按 created_at**）、update_status 三态（不改/置 NULL/写 dict+心跳）、取消、陈旧 running/cancelling 候选、get_for_update 事务内读、next_version（superseded 计入 MAX）、版本冲突→VersionConflict、supersede/mark_obsolete/write_progress、put_batch 重放幂等（2 行重放仍 2 行、**重放不覆盖 created_at**）、002 全新/存量升级/再跑幂等、评审/内容更新、Row↔Pydantic 全量往返
- 与设计偏离/决策（均不违反冻结签名）：① §2 领域类型 dd 未指定代码模块，新建 `tester_agent/domain.py` 为跨层共享唯一来源（各层可 import、不反向依赖），仅放本包所需类型；② errors.py 属 WP-06，因 DAO get() 契约依赖提前落 §17.1 冻结形态三子类，WP-06 只扩不重建；③ `RequirementRef` dd 无类定义（仅 DDL 注释），按注释补型——**用户已认可**；④ `heartbeat()` 冻结签名写 →None，实际返回 int rowcount（§6.4"0 行自杀"必需，调用方可忽略）；⑤ ~~游标用 updated_at~~ 已由 002 改为 created_at；testcase.error_info 是 `{code}` 对账标记（DDL 注释）而非 task 的 ErrorInfo，CaseRow 提供 `error_code()`；⑥ update_status 用 `_UNSET` 哨兵区分"不改列/显式 NULL"，start_new_run 仅切 run/thread/stage 并清取消标志，状态迁移由调用方同事务 update_status
- 遗留与提问：③⑤已裁决关闭；§11.2 伪码方法名（superseded_and_obsolete、mark_obsolete_by_versions_or_points）与 §3.3 冻结签名不一致，WP-24 以 §3.3 为准；本机无 data/ 存量库，002 升级路径已由测试覆盖
- 下个包起步点：WP-04（DAO 其余 + Page 游标分页，依赖 02 已满足）。第一步：dd §3.1 §3.3，在 `store/models.py` 续补 workspace/agent/conversation/message/trace/snapshot/event/review/proposal/config 十类 DAO，直接复用已落地的 Page/_fetch_page/游标与 NotFoundError 模式；注意 testcase 现为 schema_version=2

### [WP-02] DB 与迁移 — done（2026-09-26）

- 状态：done
- 交付物：`server/tester_agent/store/migrations/001_init.sql`（dd §3.1 全量 DDL，15 表 13 索引）、`server/tester_agent/store/db.py`（`run_migrations`/`seed_defaults`/`split_sql`/`discover_migrations` + `Database`：aexecute/aquery/aquery_one/immediate_tx）；`cli.py` init-db 接线（退出码 0+JSON 结果）；`main.py` lifespan 启动迁移；pyproject 补 `.sql` package-data；新增 `tests/test_db.py`，改 `test_skeleton.py`（init-db 不再 exit 2）
- 验收：`pytest tests/` **30 passed**（新增 14：全新迁移 15 表/13 索引齐全、重复迁移返回 [] 且不重复落账、裸连接验 WAL 持久、busy_timeout=5000/foreign_keys=1、外键拒绝、immediate_tx 提交/回滚后可复用、50 协程并发串行无错、种子幂等与不覆盖、CLI 双跑）；真机冒烟：`init-db` 首跑 migrated=[1]/再跑 []；uvicorn 不跑 init-db 启动即建库迁移成功、/healthz 200
- 与设计偏离：① dd §3.2 路径写 `server/store/migrations/`，实际置于包内 `server/tester_agent/store/migrations/`（与 WP-01 包结构及 dd §3.3 `store/db.py` 一致）；② 001 DDL 已冻结内置 `INSERT config('{}','{}')` 引导行，种子改以"两列均为 '{}' 才补默认 runtime_config"实现，model_config 保持空待引导页填写（§19.4 语义不变，已测试不覆盖用户改动）
- 遗留与提问：① lifespan 只跑迁移不跑种子（种子仅 init-db，dev.sh 保证先执行）；若接受"裸 uvicorn 也自动补种子"请在后续 WP 明示；② 种子 agent `name` 用中文"用例设计智能体"（dd 未定名，可改）
- 下个包起步点：WP-03（DAO 核心，依赖 02 已满足）。第一步：读 dd §3.3 §2.9，基于 `store/db.py` 的 `Database` 实现 TaskDAO/ArtifactDAO/TestcaseDAO + Row dataclass 与 Row↔Pydantic 转换，验收 CRUD/状态更新/next_version/put_batch(INSERT OR IGNORE) 单测

### [WP-01] 工程骨架 — done（2026-09-26）

- 状态：done
- 交付物：`server/pyproject.toml`、`server/.env.example`、`server/tester_agent/{__init__,settings,logging_config,main,cli}.py`、空子包 `store/adapters/graph/runtime/api`、`scripts/dev.sh`、`server/tests/test_skeleton.py`
- 验收：`pytest tests/` 16 passed；uvicorn（单 worker）启动 `/healthz` 返回 200 与版本号；`TESTER_AGENT_SINGLE_WORKER=4` 时启动即拒（exit=1，报"必须为 1"）；日志为 JSON 行（ts/level/logger/message+extra）
- 与设计偏离：无。细节说明：env 解析对取值做 strip（"1 " 视为合法 1），未改变"非 1 拒启"语义
- 遗留与提问：① 建议补 `.gitignore`（.venv/data/.env），属本包范围外未创建，待用户裁决；② `cli init-db` 为占位（exit 2），WP-02 接线；③ venv 装于仓库根 `.venv`（与 dev.sh 一致），Python 3.14.3 实测兼容
- 下个包起步点：WP-02（DB 与迁移，依赖 01 已满足）。第一步：读 dd §3.1 §3.2，实现 `server/tester_agent/store/001_init.sql` + `db.py`（WAL/busy_timeout=5000/immediate_tx）+ `schema_meta` 迁移执行器，验收为"全新库迁移成功/重复执行幂等/PRAGMA 验证"

## 4. 交接单模板

复制以下模板填写，插入"记录区开始"标记下方。三段各不超过 10 行；超长内容放代码或测试，不堆进交接单。

```markdown
### [WP-xx] 名称 — done（YYYY-MM-DD）

- 状态：done / doing(续) / blocked / needs-design
- 交付物：新增/改动的关键文件与模块（路径列表，不贴代码）
- 验收：执行的命令与结果（pytest node id、关键断言条数）
- 与设计偏离：无 / 列出偏离点及理由（若涉及契约变更必须先获批）
- 遗留与提问：下个会话需要先决策/注意的事项
- 下个包起步点：建议下一个 WP 编号及其第一步动作
```

## 5. needs-design / blocked 台账

设计缺口与外部阻塞在此集中登记，关闭后划掉（保留痕迹）。

| 日期 | 来源 WP | 类型 | 问题 | 建议方案 | 状态 | 裁决 |
|---|---|---|---|---|---|---|
| 2026-09-28 | SP-1 | design | Q12：ReMe 无原生 IndexTree；链路/故事靠 `signals.chain:*` 或链路树 md 派生 | WP-09 适配层扫描 frontmatter/`td-链路树` 派生 IndexTree；缺标记则空树+镜像降级 | ~~closed~~ | WP-X2：PRD/tech-design 按适配层映射关闭；无原生 API 风险由镜像降级承接 |

## 6. 使用示例

- 用户："执行 SP-1，先读交接单" → Agent 读本文件与 work-breakdown §5 的 SP-1 行 → 探测 → 在 §2 置 done、§3 追加交接单。
- 用户："执行 WP-19" → Agent 先核对 WP-18=done；读 WP-18 交接单与 dd §7.3/§11.1 → 实现 → 场景 1/2 测试绿 → 写交接单。
- 发现 langgraph 版本不支持 `Command(resume=)` → WP-15 状态置 needs-design，§5 登记问题，停止实现等待裁决。
