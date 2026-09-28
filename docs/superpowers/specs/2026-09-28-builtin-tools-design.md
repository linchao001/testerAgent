# 一期内置工具能力设计（bash + str_replace_editor）

| 项 | 内容 |
|---|---|
| 日期 | 2026-09-28 |
| 状态 | 已实现（2026-09-28）；计划见 `docs/superpowers/plans/2026-09-28-builtin-tools.md` |
| 对应 | [PRD](../../PRD.md) / [tech-design](../../tech-design.md) |
| 参考 | `D:\code\typescript\deepseek-harness` 的 `tool-bash-persistent`、`tool-str-replace-editor` |

## 1. 目标与原则

为一期**用例智能体**（`case_designer`）补齐内置基本工具：

1. **产线**：阶段节点可按需调用工具搜集上下文，再走既有结构化（json_schema）生成。
2. **对话**：`kind=chat` 的会话可调用同一套工具。

原则：

- **LangChain / LangGraph 优先**：Tool、运行时 Message、Agent 循环优先基于该技术栈；不另造裸 OpenAI tool_calls 解析器作为主路径。
- **共享运行时、双入口**：一套工具与 tool-agent 子图；对话与产线共用，owner 隔离。
- **控制面主图 + 工具子图**：用例主图为 Plan-Execute 控制环（见 plan-execute-reflexion 设计）；工具能力挂接为可复用子图 / capability tools，不用整棵 `create_react_agent` 取代控制环。
- **ReMe 读写分离不变**：工具层不可达 `ReMeWriter`。

## 2. 架构

```
web ChatPage / Stage workbench
        │
        ▼
api: conversations.messages | graph nodes
        │
        ▼
┌───────────────────────────────────────┐
│ Tool Runtime (LangChain + LangGraph)  │
│  build_case_designer_tools()          │
│  tool_agent_graph (model ↔ ToolNode)  │
│  WorkspaceSandbox                     │
│  bash | str_replace_editor            │
└───────────────────────────────────────┘
        │
        ▼
ChatOpenAI (langchain-openai → DeepSeek)
```

| 组件 | 职责 |
|---|---|
| `tools/registry.py` | 产出 `list[BaseTool]`（LC StructuredTool / `@tool`） |
| `WorkspaceSandbox` | 根目录 `data/workspaces/{workspace_id}/`；路径解析与越界拒绝 |
| `tool_agent_graph` | LangGraph：`ChatOpenAI.bind_tools` ↔ `ToolNode`，直至无 tool_calls |
| 对话入口 | `POST /conversations/{id}/messages`（`kind=chat`）→ 子图 → 落库 assistant |
| 产线入口 | 选定节点先跑子图搜集上下文，再跑无 tools 的结构化生成 |
| Owner | 对话 `conversation:{id}`；任务 `task:{id}`；各自持久 bash 会话 |

进程重启后持久 shell 丢失，下次冷启动（与现有「重启不保证内存态」一致）。

## 3. 工具契约

语义对齐 deepseek-harness；实现为 Python，经 LangChain Tool 暴露。

### 3.1 `bash`

| 项 | 约定 |
|---|---|
| 参数 | `command: string`（非空） |
| 语义 | 同一 owner 下 cwd / 导出环境变量跨调用保留；同 owner 串行 |
| 实现 | 长驻 shell + start/end marker 收 exit code（非完整 PTY UI） |
| 超时 | 默认 300s（`runtime_config.tool_bash_timeout_ms`） |
| 输出 | 默认最多 16000 字符；超长追加 harness 同款 `<response clipped>...` |
| 失败 | 超时/进程死/初始化失败 → reset 该 owner shell；返回可读错误 |
| cwd 初值 | 工作区根 |

Windows 探测（`tool_shell_backend=auto` 时写死顺序）：

1. 可用 `bash`（环境/`Git\bin\bash.exe` / 配置的 WSL）
2. 否则 `pwsh` / `powershell`（工具名仍为 `bash`，description 注明后端）
3. 都不可用 → `cli check` 报错；调用返回明确错误

### 3.2 `str_replace_editor`

| command | 必填 | 行为 |
|---|---|---|
| `view` | `path`；可选 `view_range` | 文件带行号；目录最多 2 层（排除 `.` / `node_modules` / `__pycache__`） |
| `create` | `path`, `file_text` | 已存在则失败 |
| `str_replace` | `path`, `old_str`；`new_str` 可空 | `old_str` 必须恰好一处字面匹配 |
| `insert` | `path`, `insert_line`, `new_str` | 在 `insert_line` 行之后插入（`0` = 文件开头前） |

路径：工作区相对路径或沙箱内绝对路径；解析后必须在 workspace 根内。

写保护：`**/snapshots/**` 禁止 create/str_replace/insert（view 允许）。

`cases/**/*.md` 允许编辑；写成功后触发既有 content_hash 对账；工具不写 testcase DB 行、不绕过产线「先文件后 DB」协议。

一期不做 `undo_edit`。

## 4. LangChain / LangGraph 接入

### 4.1 硬约定

| 能力 | 技术 |
|---|---|
| Tool | `langchain_core.tools`（`@tool` / `StructuredTool`） |
| Message | `HumanMessage` / `AIMessage` / `ToolMessage` / `SystemMessage` |
| Agent 循环 | LangGraph 子图 + `ToolNode` |
| LLM（tool 路径） | `ChatOpenAI`（`langchain-openai`）+ 超时/重试/信号量包装 |

### 4.2 包结构

```
server/tester_agent/tools/
  sandbox.py
  bash_persistent.py
  str_replace_editor.py
  registry.py
server/tester_agent/graph/tool_agent.py   # tool_agent_graph
```

工具闭包注入：`owner_id`、`workspace_root`、`runtime_config`、取消信号。

### 4.3 Message 持久化映射

| LC 类型 | DB `role` | 说明 |
|---|---|---|
| HumanMessage | user | `kind=chat` 等 |
| AIMessage（最终） | assistant | `payload.tool_trace` 存精简审计 |
| 中间 AIMessage+tool_calls / ToolMessage | 默认不逐条落库 | 摘要进 `tool_trace` |

历史加载：DB → 重建 LC messages（默认仅 human/assistant 交替）。

### 4.4 tool_agent_graph

```
START → model(bind_tools) ─┬─ tool_calls → ToolNode → model
                           └─ 无 tool_calls → END
```

- `max_steps`：`runtime_config.tool_agent_max_steps`（默认 12）+ recursion_limit
- 取消：请求断开 / task `cancel_requested` 传到 tools 与模型
- 对话：同 conversation 同时只跑一个子图（互斥锁）
- 产线：`intake` / `link_identify` / `point_write` / `case_generate` 可先 tool 子图再 json_schema；`coverage_check` 一期不开工具
- `kind=change_request`：一期不进 tool loop

### 4.5 LLM 收敛

- 新增 `get_chat_model()` → 配置好的 `ChatOpenAI`（对齐 tech-design D7）
- 既有 `LLMClient.chat()` 可暂留 json_schema / 部分流式；**tool 路径只走 LC**
- 全量迁到 LC 不在本特性强制一次完成

## 5. 安全

| 规则 | 约定 |
|---|---|
| 路径沙箱 | 仅 workspace 根内 |
| snapshots | 写禁止 |
| ReMe | 工具零 Writer 依赖 |
| Shell | 无命令白名单（对齐 harness）；依赖工作区沙箱 + 单机单用户；超时 kill + reset |
| 密钥 | 不在 tool_trace 记 env；文档警告勿在工作区放生产密钥 |
| 并发 | conversation/task loop 互斥；同 owner bash 串行 |

## 6. 配置与可观测

`runtime_config` 增补：

- `tool_bash_timeout_ms`（默认 300000）
- `tool_max_output_chars`（默认 16000）
- `tool_agent_max_steps`（默认 12）
- `tool_shell_backend`（`auto` \| `bash` \| `pwsh`）

`cli check` 探测 shell 后端。

任务侧 SSE 增加 `tool_call`（前端忽略未知类型保持兼容）。

审计字段：`{tool, args_digest, ok, latency_ms, error_code?}`；不落完整文件正文 / 超长 stdout。

## 7. 测试

| 层 | 内容 |
|---|---|
| 单测 | sandbox；editor 四命令与歧义匹配；bash 持久 cwd；截断文案 |
| 子图 | mock ChatOpenAI tool_calls → ToolNode → 最终文本；触顶 |
| API | chat 落库 user+assistant + `tool_trace` |
| 回归 | 默认关闭 tools 时现有产线测试通过；开启路径有少量集成夹具 |
| 平台 | Windows auto → pwsh fallback 可本地/CI 验证 |

## 8. 文档回灌（实现阶段）

- 本规格：`docs/superpowers/specs/2026-09-28-builtin-tools-design.md`
- `docs/tech-design.md`：增补内置工具 / tool-agent 子图
- `docs/PRD.md`：一期范围补对话与产线可调内置工具
- `docs/detailed-design.md`：tools 包与 API 行为
- `README` / `cli check`：Windows shell 说明

## 9. 一期不做

- `undo_edit`、完整 PTY UI、命令白名单人工审批
- `change_request` 默认 tool loop
- 用 `create_react_agent` 替换整棵用例主图
- 多租户 / 容器级 OS 沙箱
- 一次性废除全部非 LC 的 `LLMClient` 路径

## 10. 实现切片

1. Sandbox + editor + bash（无 LLM）
2. LangChain Tool 包装 + `tool_agent_graph` + `ChatOpenAI`
3. 对话 API + 前端最小 `tool_trace`
4. 产线节点可选挂接（tool 搜集 → 结构化生成）
5. 文档回灌 + check / CI

## 11. 决策记录

| 决策 | 结论 |
|---|---|
| 范围入口 | 用例智能体：产线 + 对话（共享运行时） |
| 工具清单 | harness 对齐：`bash` + `str_replace_editor` |
| 技术栈 | LangChain Tool + LangGraph ToolNode 子图优先 |
| Shell 后端 | auto：bash → pwsh；工具名保持 `bash` |
| cases 写入 | 允许 + hash 对账；不绕过 DB 协议 |
