# TesterAgent 记忆模块：嵌入式 ReMe（个人记忆 + KB 长期记忆）

| 项 | 内容 |
|---|---|
| 日期 | 2026-09-28 |
| 状态 | 设计已定稿（待用户审阅后写实现计划） |
| 对应 | [PRD](../../PRD.md) / [tech-design](../../tech-design.md) / [detailed-design](../../detailed-design.md) |
| 参考实现 | QwenPaw `agents/memory/`（`ReMeLightMemoryManager` + `reme_config`） |
| 取代 | SP-1「优先 HTTP / 默认不嵌入」；删除 `adapters/reme_http.py` 及 service 模式 |

## 0. 决策摘要

| 决策点 | 选择 |
|---|---|
| 范围 | **C**：Agent 个人记忆 + 工作区共享 KB 长期记忆 |
| ReMe 部署 | **同进程嵌入** `reme.ReMe`（与 QwenPaw 一致）；**不是**独立进程 |
| HTTP | **废除**；不保留 `mode=service` 回退；相关代码可删 |
| testerAgent 并发 | **继续单 worker**（编排/EventBus/Registry/幂等仍为进程内） |
| 隔离粒度 | **一工作区一嵌入实例**（vault 在工作区目录下） |
| KB 写门禁 | **不变**：L3/对话不可达 `ReMeWriter`；仅 `api/kb.py` confirm |
| 对话工具 | 可挂 `memory_search`；**不**挂 `save_to_knowledge` |
| 产线工具 | 一期 **不**给 capability 路径挂 memory 工具（检索仍走 pipeline） |
| 调度 | 一期 **无** dream/daily_paper cron（仍无 APScheduler） |
| 依赖 | `reme-ai>=0.4.1.5,<0.5`（对齐 QwenPaw） |

### 0.1 为何仍保留单 worker

单 worker 的价值是 **testerAgent 自身运行时**（`EventBus`、`TaskRegistry` 互斥、内存 `IdempotencyStore`、Export job 字典、Reaper）不做跨进程协调——与是否嵌入 ReMe **正交**。嵌入后索引/文件监听绑在同一进程生命周期上，更需要单 worker，避免多 worker 各持一份嵌入实例。

废除的仅是「默认不嵌入 `reme-ai`」；**不**废除单 worker。

## 1. 目标

1. 建设 `memory/` 模块：进程内嵌入 ReMe，覆盖 **daily/digest 个人记忆** 与可选 **共享 KB**（`knowledge_base_id`）。
2. 用同一嵌入实例适配现有冻结契约 `ReMeReader` / `ReMeWriter`，替换 HTTP 适配。
3. 对话侧可检索个人记忆；产线检索管线继续只读 KB；共享 KB 写入仍经提案 + 一次性令牌。

## 2. 架构

```
┌──────────── uvicorn 单 worker 进程 ─────────────────────┐
│  L2 API (chat / kb confirm / kb test)                     │
│  L3 graph (retrieve_pipeline → ReMeReader only)           │
│                                                           │
│  memory/                                                  │
│    WorkspaceMemoryPool ──► ReMeMemoryManager (per ws)     │
│         │                        │                        │
│         │                        ▼                        │
│         │                 reme.ReMe (embedded)            │
│         │                  run_job(...)                   │
│         ▼                                                 │
│  adapters/reme_sdk.py                                     │
│    SdkReMeReader / SdkReMeWriter                          │
└───────────────────────────────────────────────────────────┘
```

| 组件 | 职责 | 禁区 |
|---|---|---|
| `memory/` | 嵌入生命周期、个人记忆 jobs、对话工具、`auto_memory` | 不把 `save_to_knowledge` 暴露给 graph / tool_agent |
| `adapters/reme_sdk.py` | 将嵌入实例适配为现有 Protocol | Writer 仅被 `api/kb.py` 引用 |
| `adapters/reme.py` | 保留 Protocol / Caps / IndexMirror / Factory 骨架 | — |
| `adapters/reme_http.py` | **删除** | — |

### 2.1 目录布局

```
data/workspaces/{workspace_id}/
├── reme/                    # ReMe workspace_dir（vault）
│   ├── daily/               # 个人记忆（可配置目录名）
│   ├── digest/
│   ├── knowledge/           # 挂载/创建共享 KB 时由 ReMe 管理
│   └── …                    # metadata/session 等由 ReMe 管理
└── {task_id}/               # 既有用例产物（不变）
    ├── requirement.md
    ├── cases/
    └── snapshots/
```

## 3. 配置

### 3.1 `workspace.kb_config`（废除 service）

原：`{mode: sdk|service, target, kb_id, options}`  
新：

```json
{
  "kb_id": "zhb_kb",
  "knowledge_bases_dir": "",
  "knowledge_dir": "knowledge",
  "create_knowledge_base": false,
  "options": {
    "daily_dir": "daily",
    "digest_dir": "digest",
    "auto_memory_interval": 0,
    "memory_search_enabled": true,
    "embedding": {
      "backend": "openai",
      "model_name": "",
      "api_key": "",
      "base_url": "",
      "dimensions": 1024
    }
  }
}
```

| 字段 | 语义 |
|---|---|
| `kb_id` 空 | 仅个人记忆；不挂共享 KB |
| `kb_id` 非空 | 挂载共享长期记忆（`knowledge_search` / confirm→`save_to_knowledge`） |
| `mode` / `target` | **废弃**；API 遇 `mode=service` → 400 |

平台 LLM 仍来自全局 `config.model_config`；嵌入 ReMe 的 `as_llm` 在 `start` 时 `update_component` 注入（对齐 QwenPaw）。

### 3.2 包结构

```
server/tester_agent/memory/
  __init__.py
  reme_config.py     # build_reme_app_config（从 QwenPaw 精简移植）
  manager.py         # ReMeMemoryManager：start/close/run_job/lease
  pool.py            # WorkspaceMemoryPool
  prompts.py         # memory guidance
  tools.py           # memory_search（无 save_to_knowledge）

server/tester_agent/adapters/
  reme.py            # Protocol / Caps / Factory / Mirror（保留并改校验）
  reme_sdk.py        # NEW：SdkReMeReader / SdkReMeWriter
  reme_http.py       # DELETE
```

依赖：`server/pyproject.toml` 增加 `reme-ai>=0.4.1.5,<0.5`。

## 4. 生命周期

```
FastAPI lifespan
  → construct WorkspaceMemoryPool (empty)
  → … existing startup …
  → on demand: pool.get_or_start(workspace_id, kb_config, model_config)
       → build config → ReMe(**cfg) → inject LLM → await start()
  → graceful shutdown (after Reaper): pool.shutdown_all()
```

| 规则 | 说明 |
|---|---|
| 懒启动 | 首次 `for_workspace` / chat 需记忆 / `kb/test` 时启动 |
| 启失败 | 记日志；个人记忆工具降级为空；检索抛 `KbUnreachable` |
| 配置变更 | `kb_config` 更新 → `pool.invalidate(ws)` → close 旧实例再懒启 |
| job 并发 | 复制 QwenPaw：`_reme_job_lease` + `_exclusive_reme_lifecycle` |
| dream cron | 一期不做 |

## 5. 读写路径与门禁

| 调用方 | 能力 | 实现 |
|---|---|---|
| L3 检索管线 | `knowledge_search` / `read` / `list` | `SdkReMeReader` ← Pool |
| 对话 tool_agent | `memory_search`（scope=`agent\|knowledge\|all`） | `memory/tools.py` |
| 对话生命周期 | `auto_memory`（`interval>0`） | chat 回合后异步；失败不挡回复 |
| L2 confirm | `save_to_knowledge` | `SdkReMeWriter` 仅 `api/kb.py` |

**硬门禁（PRD 7）**：import-linter 继续断言 `graph/` / `runtime/`（除 kb 接线）/ `store/` 不 import Writer；对话 **不**注册直写 KB 工具。

### 5.1 对话接线

`chat_agent.run_chat_turn`：

1. `memory_pool.get_or_start(workspace_id, …)`
2. `build_case_designer_tools` 可选注入 `memory_manager`；`memory_search_enabled` 时注册 `memory_search`
3. system prompt 追加 memory guidance
4. 成功后按间隔触发 `auto_memory`（fire-and-forget）

产线 capability 路径一期 **不**挂 memory 工具。

### 5.2 Factory / Reader / Writer

- Factory 默认只注册 `sdk`；builder 经 Pool 包 `SdkReMeReader`
- 缓存键改为 **`workspace_id`**（废除 `(target, kb_id)`）；`for_workspace(workspace_id, kb_config)` 显式传工作区 id
- `SdkReMeReader.search` → `knowledge_search`；`get_entry` → `read`(+frontmatter)；`list_index_tree` 复用 chain 派生逻辑
- caps 一期固定 SP-1 口径 `(metadata_filter=False, entry_version=False, passage_api=True)`，或探活后缓存；IndexMirror / local hash 降级链不变
- `SdkReMeWriter` → `save_to_knowledge`；挂 `app.state.kb_writer`
- `POST .../kb/test` → pool 冷启或 `version`/`list` 测延迟与 caps

## 6. 删除与文档回灌

| 删除 | 变更 |
|---|---|
| `adapters/reme_http.py` | `kb_config` 校验新 schema |
| `tests/test_reme_http.py` | web `KbConfig` 去掉 `service` |
| `register_service_builder` / `WorkspaceRoutingWriter` | main lifespan 接 Pool + SdkWriter |
| handoff「优先 HTTP」 | tech-design / detailed-design / handoff 回灌本设计结论 |

## 7. 实现切片与验收

| 切片 | 内容 | 验收 |
|---|---|---|
| **M1** | `reme_config` + `manager` + `pool`；mock `run_job` | 启停、lease、invalidate、shutdown 单测 |
| **M2** | `reme_sdk` + Factory；删 HTTP；接 kb/test、confirm、retrieve | 既有 retrieval/API 测试改 sdk；场景 12 import-linter 绿 |
| **M3** | chat 挂 `memory_search` + guidance；可选 auto_memory | 工具注册单测；mock 片段路径 |
| **M4** | 文档回灌 + 依赖；可选真实 reme 冒烟（非 CI 必跑） | handoff + README |

测试策略：CI **默认 mock 嵌入层**；真实 ReMe 冒烟标 `@pytest.mark.integration`。

## 8. 一期非目标

- dream / daily_paper / knowledge_dream cron
- Agent 直写共享 KB（含「创建提案」快捷工具——可二期）
- 多 worker / 跨进程 ReMe / HTTP sidecar
- 前端记忆浏览器 UI（工作区配置字段即可）

## 9. 与既有文档的关系

- **tech-design §2.1**：单 worker 保留；补充「允许同进程嵌入 ReMe」；删除「优先 service」表述。
- **SP-1 handoff**：结论修订为「一期默认嵌入；HTTP 适配删除」。
- **detailed-design §9**：SDK 适配为唯一实现；`KbConfigIn` 去掉 mode/target。
- **PRD 7 / US1.3**：读写分离不变。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| `reme-ai` 重依赖 / 体积 | 版本钉死；CI mock；文档写清安装 |
| 嵌入实例拖垮 API 进程 | 单 worker + job lease；启失败可降级；graceful close |
| LLM/embedding 注入差异 | 对齐 QwenPaw `update_component`；embedding 可空走 BM25-only |
| 多工作区内存 | 懒启 + invalidate 上限可后续加；一期按访问工作区数量自然增长 |
