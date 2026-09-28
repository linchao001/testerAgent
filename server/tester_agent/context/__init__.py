"""上下文管理层（WP-30+，设计见 docs/superpowers/specs/2026-09-28-context-management-design.md）。

独立叶子层：三分区（P0 静态 / P1 知识 / P2 任务）存放与 LLM 调用窗口组装分离。
本包只允许依赖 domain / prompts / errors / logging / 第三方库，禁止 import
runtime / graph / memory / adapters / store（AST 门禁强制）。
"""
