---
version: "2026-09-26.1"
node: case_generate
---
输入：本批 <points> + 每点相关 <requirement_clauses> 原文
+ <knowledge> 接口/DB/缺陷回归/相关规则段落（带 [ID]）。
任务：将每个测试点展开为可执行测试用例：
- 步骤具体到操作动作与数据，预期可判定、与步骤对应；
- 接口/DB 类知识要落到步骤或测试数据中，不得只在标题体现；
- trace_refs.entry_ids 只填 <knowledge> 白名单 ID；clause_ids 填实际覆盖条款。
每个 point 默认产出 1~3 条用例，按 <point id="…"> 分段输出。
输出 schema：（贴 §7.4③）
