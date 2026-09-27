---
version: "2026-09-26.1"
node: link_identify
---
输入：
<requirement_clauses> 需求条款（标题路径+摘要）
<knowledge> 知识库中全部相关测试链路/用户故事索引摘要（每条带 [ID]、类型与归属链路）。
任务：
1. 判断需求涉及哪些已有链路与用户故事（hit=true，entry_id 必须取自 <knowledge> 的 ID）；
2. 知识库没有对应项但需求明显需要的，输出到 new_suggestions（仅建议，不写库）；
3. 给出每条 story 与需求关联的 rationale，并在 related_clause_ids 引用条款。
置信度规则：标题与摘要均明确对应≥0.8；主题相关但需推断 0.4~0.7；低于 0.4 不要输出为命中。
输出 schema：（贴 §7.4① 的 JSON Schema）
