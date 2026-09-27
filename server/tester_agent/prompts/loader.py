"""Prompt 模板加载器（dd §20.7）。

- 内置模板目录：``server/tester_agent/prompts/``（随包分发）；
- 自定义目录：``agent.config.prompts_dir`` 指定，同名文件覆盖内置；
- 文件头 YAML 声明 ``version``，加载时解析；
- 模板变更必须升 version；eval smoke 对版本敏感。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# 内置模板目录（随包分发）：本文件所在目录即内置模板目录
_BUILTIN_DIR = Path(__file__).resolve().parent

# 模板文件头 YAML 解析
_YAML_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


class PromptTemplate:
    """单个 Prompt 模板：版本 + 正文。"""

    __slots__ = ("name", "version", "content", "source")

    def __init__(self, name: str, version: str, content: str, source: str) -> None:
        self.name = name
        self.version = version
        self.content = content
        self.source = source  # "builtin" | 自定义目录路径

    def render(self, **kwargs: Any) -> str:
        """模板变量替换（str.format）。"""
        return self.content.format(**kwargs)


class PromptLoader:
    """按名称加载 Prompt 模板，支持自定义目录覆盖内置。"""

    def __init__(self, custom_dir: str | Path | None = None) -> None:
        self._custom_dir = Path(custom_dir) if custom_dir else None
        self._cache: dict[str, PromptTemplate] = {}

    def _find_file(self, name: str) -> Path | None:
        """查找模板文件：先自定义目录，后内置目录。"""
        # 支持子目录，如 "retrieve/multi_query" 或 "retrieve/multi_query.md"
        rel = name if name.endswith(".md") else f"{name}.md"
        if self._custom_dir:
            custom_path = self._custom_dir / rel
            if custom_path.is_file():
                return custom_path
        builtin_path = _BUILTIN_DIR / rel
        if builtin_path.is_file():
            return builtin_path
        return None

    def load(self, name: str) -> PromptTemplate:
        """加载模板（带缓存）。name 形如 "retrieve/multi_query" 或 "system.shared"。
        
        未找到时抛 FileNotFoundError；文件头解析失败时抛 ValueError。
        """
        if name in self._cache:
            return self._cache[name]

        path = self._find_file(name)
        if path is None:
            raise FileNotFoundError(f"prompt 模板不存在: {name}")

        text = path.read_text(encoding="utf-8")
        version, content = _parse_header(text, path)
        source = "builtin" if _BUILTIN_DIR in path.parents else str(path.parent)
        tpl = PromptTemplate(name=name, version=version, content=content, source=source)
        self._cache[name] = tpl
        return tpl

    def load_many(self, names: list[str]) -> dict[str, PromptTemplate]:
        """批量加载。"""
        return {name: self.load(name) for name in names}

    def version(self, name: str) -> str:
        """读取模板版本（不加载正文）。"""
        return self.load(name).version

    def clear_cache(self) -> None:
        """清空缓存（测试/热更新用）。"""
        self._cache.clear()


def _parse_header(text: str, path: Path) -> tuple[str, str]:
    """解析文件头 YAML，返回 (version, content)。"""
    m = _YAML_RE.match(text)
    if not m:
        raise ValueError(f"prompt 模板缺少 YAML 文件头: {path}")
    yaml_block, content = m.group(1), m.group(2)
    version = _extract_yaml_field(yaml_block, "version")
    if not version:
        raise ValueError(f"prompt 模板文件头缺少 version 字段: {path}")
    return version, content


def _extract_yaml_field(yaml_text: str, field: str) -> str | None:
    """从 YAML 文本中提取简单字段值（不依赖外部 YAML 库）。"""
    for line in yaml_text.splitlines():
        line = line.strip()
        if line.startswith(f"{field}:"):
            val = line[len(field) + 1 :].strip()
            # 去除引号
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            return val
    return None


# ---------- 全局默认 loader（内置目录） ----------

_default_loader: PromptLoader | None = None


def default_loader() -> PromptLoader:
    """返回全局默认 loader（仅内置目录）。"""
    global _default_loader
    if _default_loader is None:
        _default_loader = PromptLoader()
    return _default_loader


def load_prompt(name: str) -> PromptTemplate:
    """快捷加载（默认 loader）。"""
    return default_loader().load(name)


def prompt_version(name: str) -> str:
    """快捷读取版本（默认 loader）。"""
    return default_loader().version(name)
