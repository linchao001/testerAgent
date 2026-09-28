"""P0 冻结引导段（spec §5 防线 4 / I2 确定性）。

按调用方给定的模板顺序拼接静态方法论与通用规则，输出 SystemMessage、内容
版本号与 token 估算。版本号对模板集合（name:version，排序后）+ extra_static
取 sha256[:12]：模板未变 → 字节级一致；任何模板升版/额外静态段变更 → 版本
立即漂移，eval smoke 可感知。
"""

from __future__ import annotations

import hashlib

from langchain_core.messages import BaseMessage, SystemMessage

from ..prompts.loader import PromptLoader
from .tokens import estimate_tokens


def bootstrap_p0(
    *,
    loader: PromptLoader,
    template_names: list[str],
    extra_static: str = "",
) -> tuple[list[BaseMessage], str, int]:
    """引导 P0：固定顺序拼模板内容；返回 (messages, p0_version, tokens)。"""
    tpls = [loader.load(name) for name in template_names]
    body = "\n\n".join([t.content for t in tpls] + ([extra_static] if extra_static else []))

    basis = "\n".join(sorted(f"{t.name}:{t.version}" for t in tpls))
    p0_version = hashlib.sha256((basis + extra_static).encode("utf-8")).hexdigest()[:12]

    messages: list[BaseMessage] = [SystemMessage(content=body)] if body else []
    return messages, p0_version, estimate_tokens(body)
