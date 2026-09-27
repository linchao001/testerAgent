---
version: "2026-09-26.1"
node: point_write
---
输入：本批 <stories>（已用户确认）+ 其关联 <requirement_clauses> 原文
+ <knowledge> 业务规则/主流程用例/历史缺陷段落（带 [ID]）。
任务：为每个 story 设计测试点，覆盖正常/异常/边界/权限，历史缺陷须体现为回归测试点；
每个测试点必须能在 clause_ids 与 source_entry_ids 中指出依据（白名单 ID）。
一条 story 的测试点数量 3~8 个，按业务优先级排序，P0 仅用于主流程关键路径。
输出 schema：（贴 §7.4②）
