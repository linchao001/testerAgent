# -*- coding: utf-8 -*-
"""Memory guidance prompts for chat system messages."""

from __future__ import annotations


def build_memory_guidance_prompt(
    *,
    memory_search_enabled: bool = True,
    knowledge_enabled: bool = False,
    daily_dir: str = "daily",
    digest_dir: str = "digest",
) -> str:
    if not memory_search_enabled:
        return ""
    lines = [
        "你可以使用 memory_search 检索个人记忆与（若已配置）共享知识库。",
        f"个人记忆目录：{daily_dir}/、{digest_dir}/。",
        "仅在需要先前结论、偏好、决策或库内事实时再搜索；不要默认每轮搜索。",
    ]
    if knowledge_enabled:
        lines.append(
            "默认优先检索共享知识库；scope=agent 仅个人记忆，scope=all 混合。"
        )
        lines.append(
            "不要尝试直接写入共享知识库；沉淀知识须经用户确认的提案流程。"
        )
    return "\n".join(lines)
