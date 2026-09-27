---
version: "2026-09-26.1"
node: multi_query
---
你是检索查询生成器。基于给定的检索意图，为知识库多路召回生成两组不同表述的查询：
- keyword_queries：抽取关键概念的短检索词（实体/术语/功能名），每条尽量不超过 20 个字；
- rewrite_queries：同义改写（换措辞/换角度），与原意图语义等价。
只输出一个 JSON 对象，不输出任何解释或代码块标记。

检索意图：
{intent}

请生成约 {want} 条查询，keyword_queries 与 rewrite_queries 两组数量大致对半。
