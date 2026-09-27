# TesterAgent 工作分工总表（WBS）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-09-26 |
| 依据 | [tech-design.md v0.2](file:///Users/test/Documents/python_project/testerAgent/docs/tech-design.md)、[detailed-design.md v0.2](file:///Users/test/Documents/python_project/testerAgent/docs/detailed-design.md) |
| 配套文件 | [handoff.md](file:///Users/test/Documents/python_project/testerAgent/docs/plan/handoff.md)（WP 状态表与交接单） |

> 目的：把实现拆成可在**单次专注会话**内完成的工作包（WP），用交接单而非全量回读来跨会话恢复上下文，避免一次性实现导致的上下文膨胀与注意力漂移。

---

## 1. 协作硬规则

1. **一个 WP = 一个全新会话**：S 包≈半天、M 包≈一天、L 包一天半以内；会话开工只读本包"输入锚点"章节，不重读全部设计文档。
2. **契约冻结，只实现不重设计**：Pydantic 类型、DAO 签名、错误码（dd §14）、SSE 事件（dd §10.4）、DDL（dd §3.1）以 detailed-design v0.2 为准。发现设计缺口 → 停机、在交接单"遗留/提问"记录并向用户确认，**禁止现场扩范围或私自改契约**。
3. **测试即记忆**：每个 WP 必须以 pytest 验收场景收尾；下个会话靠测试 + 交接单恢复，不靠通读代码。
4. **每包收尾必写交接单**：在 [handoff.md](file:///Users/test/Documents/python_project/testerAgent/docs/plan/handoff.md) 追加记录（完成内容 / 与设计偏离 / 下个包起步点，各不超过 10 行），并更新状态表。
5. **未经用户明确要求不 commit**；不做本 WP 之外的重构与文件创建。
6. 体量超预期时：先完成一个可测试的垂直切片并交付，剩余部分登记为新 WP（编号 WP-xxb），不硬撑。
7. 每包开始前对照状态表确认依赖 WP 均为 done；依赖未完成不得开工（除非该依赖已被 mock，按"替身"列约定执行）。

## 2. 四条工作线

| 线 | 范围 | 启动条件 |
|---|---|---|
| α 后端主线 | WP-01~29（关键路径） | 立即 |
| β Spike | SP-1 ReMe 能力探测；SP-2 LangGraph 中断/恢复验证 | 立即，分别阻塞 WP-09 / WP-15 |
| γ 前端 | WP-F0~F5，MSW mock 先行，后端就绪后联调 | 契约已冻结（dd §10），可随时启动 |
| δ 质量/收尾 | WP-13（eval）、WP-X1（E2E）、WP-X2（收尾发布） | 跟随 α 线 |

体量标记：**S** 小（半天内）｜**M** 中（一天内）｜**L** 大（一天半，注意拆切片）。

---

## 3. α 线工作包

### 3.1 地基（Wave 0~1）

| WP | 任务 | 体量 | 依赖 | 输入锚点 | 验收 |
|---|---|---|---|---|---|
| 01 | 工程骨架：pyproject、包结构、settings（§13 环境变量）、结构化日志、cli 框架、/healthz、.env.example、单 worker 守卫 | S | — | dd §1.2 §13 §19.3 | uvicorn 启动 healthz 200；多 worker 启动被拒 |
| 02 | DB：001_init.sql、db.py（WAL/busy_timeout=5000/aexecute/aquery/immediate_tx）、schema_meta 迁移执行器 | S | 01 | dd §3.1 §3.2 | 全新库迁移成功；重复执行幂等；PRAGMA 验证 |
| 03 | DAO 核心：task / stage_artifact / testcase + Row dataclass 与 Row↔Pydantic 转换 | M | 02 | dd §3.3 §2.9 | CRUD、状态更新、next_version、put_batch(INSERT OR IGNORE) 单测 |
| 04 | DAO 其余：workspace/agent/conversation/message/trace/snapshot/event/review/proposal/config + Page 游标分页 | M | 02 | dd §3.1 §3.3 | 分页无重复无遗漏；workspace 强制过滤 |
| 05 | FileStore：tmp→fsync→rename 原子写、用例 MD 渲染/解析/content_hash、SnapshotWriter 偏移读写、路径越界防护 | M | 01 | dd §4 §5 | 提交崩溃文件侧行为；hash 对拍；单行偏移读 |
| 06 | errors.py 异常体系 + trace_id contextvar 中间件 + FastAPI 错误信封 handler | S | 01 | dd §17 §14 | 每类 AppError 的 HTTP 映射单测；5xx 带 trace_id |
| 07 | LLMClient：超时/指数退避重试/并发信号量/json-repair+带校验错误重请 1 次/流式 usage、异常翻译 | M | 01、06 | dd §9.3 §17.2 | fake server 覆盖 429/5xx/超时/4xx/坏 JSON 五场景 |
| 08 | ReMe 契约层：Reader Protocol + FakeReader（测试夹具级）+ ReMeCaps + IndexMirror + Factory | M | 01 | dd §8.4 §9.1 | caps 三态降级分支；镜像 TTL 刷新单测 |

### 3.2 接入与检索（Wave 2）

| WP | 任务 | 体量 | 依赖 | 输入锚点 | 验收 |
|---|---|---|---|---|---|
| 09 | ReMe 真实适配（SDK 或 HTTP，按 SP-1 结论）+ kb 探活能力位回填 | M | 08、**SP-1** | dd §9.1；SP-1 报告 | 连接测试通过；不支持的能力按预案降级 |
| 10 | 检索算子 A：multi_query / parallel_recall（并发+并集去重+单路容错）/ meta_filter | M | 07、08 | dd §8.1 §8.2 §2.8 | 单路失败不阻他路；filtered_* 裁剪留痕 |
| 11 | 检索算子 B：rerank（>30 分桶）/ passage_extract（结构化切分为主）/ assemble（预算+硬上限+锚点排序+去重）+ run 内 LRU | M | 10 | dd §8.2 §8.5 | rerank_cutoff/budget_cut 正确；锚点位置断言 |
| 12 | retrieve_pipeline 编排：trace 两写回填（referenced/hallucinated/weak）、snapshot 三档（off/meta/full 偏移）、latencies/degraded | M | 04、05、11 | dd §8.3 §8.6 | full 档按偏移读单行；三档行为表测试 |
| 13 | eval smoke：fixtures（假知识库+构造需求）+ run.py + 5 项指标表 + config-diff | S | 12 | dd §15.3 | CLI 跑冒烟集输出指标对比 |
| 14 | prompts 包：6 个模板 v1 正文 + loader（自定义覆盖）+ 文件头 version 读取 | S | 01 | dd §20 | 覆盖顺序与版本号读取单测 |

### 3.3 图主干（Wave 3）

| WP | 任务 | 体量 | 依赖 | 输入锚点 | 验收 |
|---|---|---|---|---|---|
| 15 | constants/state + build_graph（含 checkpointer、两个 interrupt gate）+ wrap()（注入 ctx/计时/取消/错误映射） | M | **SP-2** | dd §7.1 §1.3 | 图编译通过；断点位置断言；SP-2 失败则按 run_from_stage 预留口实现 |
| 16 | intake：ATX 标题确定性切分（h2-1-h3-2 规则）+ clauses 缓存文件 + 歧义 interrupt/answer 恢复 | M | 05、14、15 | dd §7.5① §4.3 | 重算 clause_id 稳定；无标题文档 root 兜底 |
| 17 | link_identify：检索配置接入 + 白名单 entry_id 校验 + 临时 ID 服务端改写 + artifact 落库 | S | 12、16 | dd §7.4① §7.5② | FakeLLM 脚本驱动产物断言；幻觉 entry 降 hit=false |
| 18 | 批次执行器 run_in_batches + point_write（游标/progress/幂等 nonce/取消边界/事件） | M | 17、03 | dd §7.3 §7.5③ | started 重做、done 跳过、cancel 边界三测 |
| 19 | case_generate worker + 用例批次提交协议（uuid5 确定性 ID、先文件后 DB、sweep_stale_idem） | M | 18、05 | dd §7.4③ §11.1 §7.3 | **测试场景 1/2**：四崩溃点位重放产物等价、无重复行/文件 |
| 20 | coverage_check：程序化矩阵 + ≤2 轮补充生成 + degraded 告警 | S | 19 | dd §7.5④ §2.6 | 全覆盖/部分覆盖/达上限三态；告警事件 |

### 3.4 运行时与 API（Wave 4）

| WP | 任务 | 体量 | 依赖 | 输入锚点 | 验收 |
|---|---|---|---|---|---|
| 21 | EventBus（先落库后广播+订阅闭窗）+ SSE 端点 + Last-Event-ID/after_event_id 回放 + ping | M | 04 | dd §6.2 §10.4 | **场景 7**：断连补发无重无漏；多标签页 |
| 22 | TaskRegistry（owner token/心跳判活）+ Runner（心跳循环/配置冻结/EventBridge/取消）+ 状态迁移表 | M | 15、21 | dd §6.3 §6.4 | 重复 run→409；心跳 0 行更新自杀 |
| 23 | lifespan 启停序列 + Reaper（陈旧 running/cancelling→failed）+ 优雅关闭 30s | S | 22 | dd §6.5 | **场景 6**：重启改判→/run 游标恢复 |
| 24 | 回退协议：结构化 impact diff + BEGIN IMMEDIATE 落账（supersede/obsolete/继承/新 run+派生 thread）+ start_run_from_stage | **L** | 19、22 | dd §11.2 §18.3 §2.9 | **场景 3**：改摘要全继承零 LLM；删 story 精确作废 |
| 25 | API-A：workspaces（CRUD/软删/kb test）、agents、conversations、messages、model config/test | M | 04、06、09 | dd §10.2 §5.1~5.2 | 跨工作区 404 不暴露存在性；探活错误码 |
| 26 | API-B：tasks create/run/cancel/confirm(含 modify)/answer + 状态守卫 + regenerate 入口接线 | M | 22、24 | dd §10.2 §10.3 §7.6 | **场景 4/5**：迟到版本 confirm→409；waiting 取消即生效 |
| 27 | API-C：cases list/get/edit(If-Match)/review 批量校验/regenerate + ExportService（同步/异步阈值/hash 跳过） | M | 19、05 | dd §10.2 §9.4 | **场景 11**：乐观锁；非法评审转换整体 422 |
| 28 | API-D：traces/snapshots（含 items/{position}）/playground + kb 提案两阶段（令牌/幂等/fail_count/needs_manual_check） | M | 12、09 | dd §5.5 §5.6 §8.7 §11.4 | **场景 12**：import-linter 门禁；重放同结果 |
| 29 | Reconciler（缺文件/改文件/孤儿三态+消解动作）+ 幂等存储（一期内存 TTL）+ 保留期惰性清理 | S | 23、05 | dd §11.3 §6.6 §3.3 | **场景 10**：三态检测与恢复；.tmp 清理 |

## 4. γ 线工作包（前端，MSW 先行）

| WP | 任务 | 体量 | Mock 依赖 | 联调依赖 | 验收 |
|---|---|---|---|---|---|
| F0 | Vite+TS 脚手架、api client（信封/分页/幂等头）、sse.ts（Last-Event-ID/退避）、路由与布局 | S | dd §10 §14 | — | MSW 下错误码展示与断线重连黄条 |
| F1 | ChatPage：需求输入/建任务自动 run/澄清卡/change_request 二次确认 | M | dd §10.2 | WP-26 | 脚本事件驱动到 checkpoint_waiting |
| F2 | StageConfirmPage：Link/Point 清单编辑器（仅开放修订契约字段）+ ImpactPreview | M | dd §2.3 §2.4 §10.2 | WP-24、26 | modify 带 expected_version；VERSION_CONFLICT 提示 |
| F3 | WorkbenchPage：三栏列表/MD 渲染编辑/评审条/版本过滤/采纳率摘要/文件冲突红条 | L | dd §4.2 §10.2 | WP-27 | If-Match 冲突 diff；批量评审后摘要刷新 |
| F4 | RetrievalDebugPage：漏斗图/轨迹树/快照列表与偏移全文弹窗/Playground 侧栏 | M | dd §8 §5.5 | WP-28 | 三档快照与 degraded 黄标渲染 |
| F5 | Workspaces/Settings（kb/test 能力位只读、model/test）+ 全链路抛光 | S | dd §5.1 §5.6 | WP-25 | 主场景浏览器走通 |

## 5. β Spike 与 δ 收尾

| 编号 | 任务 | 阻塞 | 产出 |
|---|---|---|---|
| SP-1 | ReMe 三能力探测：metadata_filter、entry_version（updated_at/hash）、passage_api；确认 SDK/HTTP 接入模式 | WP-09 | 一页结论写入交接单；不通过项明确启用 dd §8.4 镜像/本地 hash/本地切分预案 |
| SP-2 | 锁定 langgraph 版本下验证：`interrupt()` 任意节点挂起、`Command(resume=)` 恢复、modify 后 update_state、派生 thread 续跑 | WP-15 | 最小可运行验证脚本 + go/no-go 结论；no-go 则确认 run_from_stage 编排器为正式路径 |
| WP-X1 | Playwright E2E：主场景/崩溃恢复/回退继承；补测试场景 8（降级链）、9（引用闭环） | WP-26/27、F3 | dd §15.2 清单对应自动化 |
| WP-X2 | 导出与提案全链验收、backup cli、保留期实测、Q1/Q2/S3/S4/S5/S6/S7 结论回灌三份文档、发布检查 | 全部 | 候选发布；文档版本同步 |

## 6. 波次与里程碑

```
Wave 0  SP-1 ┐  WP-01                         F0（可任意早启动）
        SP-2 ┘
Wave 1  WP-02 → WP-03 / WP-04
        （WP-05/06/07/08 仅依赖 01，可与 02~04 并行铺开）
Wave 2  WP-09 ├ WP-10 → WP-11 → WP-12 → WP-13
Wave 3  WP-14 → WP-15 → WP-16 → WP-17 → WP-18 → WP-19 → WP-20
Wave 4  WP-21 → WP-22 → WP-23 ├ WP-24 → WP-25 / WP-26 → WP-27 / WP-28 → WP-29
Wave 5  F1 → F2 → F3 → F4 → F5（后端依赖就绪即插入联调）
Wave 6  WP-X1 → WP-X2
```

关键路径：**01 → 02 → 03 → 15 → 16 → 17 → 18 → 19 → 22 → 24 → 26 → X1**。

| 里程碑 | 达成标志 |
|---|---|
| M0 地基可用 | WP-01~06 done；SP-1/SP-2 有结论 |
| M1 检索可演示 | WP-09/12/13 done；playground 后端可调，漏斗指标可见 |
| M2 无头全链路 | WP-20/24/26 done；API 脚本跑通主场景、回退继承、崩溃恢复 |
| M3 产品主流程 | F1/F2/F3 联调通过 |
| M4 候选发布 | F4/F5、WP-28/29、X1/X2 done；开放问题结论回灌 |

## 7. 会话执行协议（每个 WP 固定流程）

1. 用户发起：**"执行 WP-xx，先读交接单"**（可加范围说明）。
2. Agent：读 [handoff.md](file:///Users/test/Documents/python_project/testerAgent/docs/plan/handoff.md) 状态表与依赖包交接单 → 只读本 WP"输入锚点"章节 → TodoWrite 拆步 → 实现+测试。
3. 收尾：跑本包验收命令 → 更新 handoff.md（状态 + 交接记录）→ 停止并汇报，不自动开下一包。
4. 遇阻塞：保留工作现场、交接单写明阻塞点与最小复现，状态置 blocked。
5. 契约缺口：状态置 needs-design，列出问题与建议方案，等待用户裁决（可能产生文档升版）。

## 8. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-09-26 | 初版：29 个后端 WP + 6 个前端 WP + 2 spike + 2 收尾包 |
