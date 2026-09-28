# 候选发布检查清单（WP-X2 / M4）

| 项 | 内容 |
|---|---|
| 版本 | v1.0 |
| 日期 | 2026-09-28 |
| 配套 | [handoff.md](handoff.md)、[work-breakdown.md](work-breakdown.md) |

发布前逐项勾选。真实 ReMe/LLM 联调与 Playwright 全主场景不阻塞候选发布（见遗留）。

## 1. 自动化门禁

- [ ] `cd server && pytest -p no:zframe` 全绿
- [ ] `cd server && pytest -p no:zframe tests/test_backup_cli.py tests/test_release_x2.py tests/test_maintenance_cleanup.py`（WP-X2 收口）
- [ ] `cd web && npm test` 全绿
- [ ] `cd web && npm run build` 成功
- [ ] （可选）`cd web && npm run e2e` UI smoke

## 2. 功能收口（场景）

| 场景 | 覆盖 | 自动化锚点 |
|---|---|---|
| 主路径 CP1→CP2→评审→导出 | API E2E | `tests/test_e2e_main.py` |
| 回退继承 / failed 续跑 | API E2E | 同上 |
| 降级链 / 引用闭环 | 场景 8/9 | `tests/test_scenario_8_9.py` |
| 对账三态 | 场景 10 | `tests/test_reconciler.py` |
| 评审乐观锁 / 导出 zip | 场景 11 | `tests/test_api_c.py` |
| 提案两阶段 + import-linter | 场景 12 | `tests/test_api_d.py` / `test_release_x2.py` |
| 保留期清理 | events/exports/.tmp | `tests/test_reaper.py` / `test_maintenance_cleanup.py` / `test_release_x2.py` |
| 备份 CLI | dd §19.5 | `tests/test_backup_cli.py` |

## 3. 运维检查

- [ ] `TESTER_AGENT_SINGLE_WORKER=1`（其他取值启动拒绝）
- [ ] `python -m tester_agent.cli init-db` 幂等成功
- [ ] `python -m tester_agent.cli check` → `ok: true`
- [ ] `python -m tester_agent.cli backup <out_dir>` 产出 app.db（+ 可选 checkpoints/workspaces）
- [ ] 模型配置已在设置页填写并可 `model/test`（或接受空 key 首启引导）
- [ ] 工作区已配置嵌入式 ReMe（`kb_id` + 可选 `options`）且 `kb/test` 可读（无真实 `reme-ai` 时可跳过联调）

## 4. 文档同步

- [ ] PRD ≥ v0.7（Q1/Q2/Q6/Q7/Q11/Q12 结论）
- [ ] tech-design ≥ v0.3（S1~S7 结论）
- [ ] detailed-design ≥ v0.3（Q1/Q2 定稿、backup 接线、exports 路径修正）
- [ ] handoff.md：WP-X2 = done，状态总表全绿（或遗留已登记）

## 5. 已知遗留（不阻塞候选发布）

| 项 | 说明 |
|---|---|
| 真实 ReMe/LLM 联调 | 需本机 ReMe service + DeepSeek key |
| Playwright 全主场景 | 仅 UI smoke；主路径在 ASGI API E2E |
| S3/S4/S5 真实标定 | deferred/partial；初值发布，运维期固化 |
| runtime_config UI | 设置页仅 model_config |
| cli `reap` | 仍占位；启动 lifespan 已跑 Reaper/对账/清理 |
| Q3 折算 / Q8 合规 / Q9 SLA / Q10 品牌 | 仍 open |

## 6. 发布判定

全部 §1~§4 勾选且无 P0 缺陷 → **候选发布（M4）**。
