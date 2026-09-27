"""Prompt 模板包（dd §20）。

内置模板目录：``server/tester_agent/prompts/``；
自定义目录覆盖：``agent.config.prompts_dir`` 指定。

模板清单：
- ``system.shared``       —— 所有生成类节点拼接的系统约束
- ``intake.ambiguity``    —— intake 歧义检测（可配置关闭）
- ``link_identify.main``  —— 链路识别
- ``point_write.main``    —— 测试点编写
- ``case_generate.main``  —— 用例生成
- ``retrieve/multi_query`` —— 多路召回查询生成
- ``retrieve/rerank``      —— 检索结果重排
"""

from .loader import PromptLoader, PromptTemplate, default_loader, load_prompt, prompt_version

__all__ = [
    "PromptLoader",
    "PromptTemplate",
    "default_loader",
    "load_prompt",
    "prompt_version",
]
