# TesterAgent

基于知识库（ReMe）的测试用例生成 Agent。运行范式：**动态 Plan-Execute 为主干 + Reflexion 反思校验 + 工具/子任务**；人机确认可配置（默认开启链路/测试点/评审门禁）；前端以**对话会话（Session）**为主交互。

一期约束：**单机单用户、单进程单 worker**（SQLite + 文件系统双事实源）。

设计见 [`docs/superpowers/specs/2026-09-28-plan-execute-reflexion-design.md`](docs/superpowers/specs/2026-09-28-plan-execute-reflexion-design.md)。

## 快速启动

```bash
# Linux / macOS
./scripts/dev.sh
# 浏览器打开 http://127.0.0.1:8080
```

Windows（PowerShell）等价步骤：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .\server
if (-not (Test-Path server\.env)) { Copy-Item server\.env.example server\.env }
python -m tester_agent.cli init-db
cd web; npm install; npm run build; cd ..
uvicorn tester_agent.main:app --host 127.0.0.1 --port 8080 --workers 1
```

环境变量见 `server/.env.example`（`TESTER_AGENT_SINGLE_WORKER` 必须为 `1`）。

## CLI

```bash
python -m tester_agent.cli init-db          # 迁移 + 内置 agent / config 种子
python -m tester_agent.cli check           # 依赖与 data 目录体检（含 shell 后端探测）
python -m tester_agent.cli backup <out_dir>  # 在线备份 app.db + checkpoints.db + workspaces/
```

`cli check` 会探测内置 `bash` 工具的 shell 后端：`tool_shell_backend=auto` 时优先系统/Git bash，否则回退 `pwsh`/`powershell`（工具名仍为 `bash`）。Windows 上若两者都不可用，check 会报错——安装 Git for Windows 或确保 PowerShell 在 PATH 中。

**安全提示**：工具在工作区沙箱内执行命令/读写文件；请勿在 `data/workspaces/` 放置生产密钥。

备份说明（dd §19.5）：

- 使用 SQLite backup API，可在服务运行时热备；生产建议仍优先在低峰或停服后执行。
- 输出目录写入 `app.db`、`checkpoints.db`（若存在）、`workspaces/`。
- `app.db` 缺失返回非 0；不做自动调度。

## 文档

| 文档 | 说明 |
|---|---|
| [docs/PRD.md](docs/PRD.md) | 需求与开放问题结论 |
| [docs/tech-design.md](docs/tech-design.md) | 架构决策与 spike 结论 |
| [docs/detailed-design.md](docs/detailed-design.md) | 实现级契约 |
| [docs/plan/handoff.md](docs/plan/handoff.md) | WP 进度与交接 |
| [docs/plan/release-checklist.md](docs/plan/release-checklist.md) | 候选发布检查清单 |

## 测试

```bash
cd server && pytest -p no:zframe
cd web && npm test
# 可选 UI smoke（不入默认 CI）
cd web && npm run e2e
```
