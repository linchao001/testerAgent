"""WP-32 Task 16：context 包门禁（spec §10.4）。

1. 叶子模块（context/ 除 retrieval/）不得 import runtime/graph/memory/adapters/store；
2. context 包（含 retrieval）对 ReMeWriter/WriteResult/UnavailableWriter 零引用；
3. 既有场景 12a（graph/runtime/store 禁 Writer）不回退——本文件复测同规则并扩面。

相对 import 口径：
- ``from .store``（level=1）= context.store，合法；
- ``from ..store``（level=2）= tester_agent.store，违规；
- 绝对 ``tester_agent.store`` / ``import tester_agent.store`` 违规。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tester_agent

_PKG = Path(tester_agent.__file__).resolve().parent
_BANNED_WRITER = {"ReMeWriter", "WriteResult", "UnavailableWriter"}
_BANNED_LEAF_PARENTS = frozenset({"runtime", "graph", "memory", "adapters", "store"})


def _iter_py(root: Path):
    yield from sorted(root.rglob("*.py"))


def _banned_target(module: str) -> bool:
    return any(module == b or module.startswith(b + ".") for b in _BANNED_LEAF_PARENTS)


def _leaf_import_violations(node: ast.AST) -> list[str]:
    """解析单条 import 是否触达叶子禁入包；返回描述片段（无路径前缀）。"""
    hits: list[str] = []
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        names = {a.name for a in node.names}
        level = node.level
        if level == 0:
            if module.startswith("tester_agent."):
                hay = module[len("tester_agent.") :]
                if _banned_target(hay):
                    hits.append(f"from {module} import {sorted(names)}")
            elif _banned_target(module):
                # 裸顶层 from store import X —— 在包内亦视为外层 store
                hits.append(f"from {module} import {sorted(names)}")
        elif level == 1:
            # from .store / from .models —— 同包兄弟，合法
            pass
        else:
            # level>=2：上溯出 context → tester_agent.<module>
            if module and _banned_target(module):
                hits.append(f"from {'.' * level}{module} import {sorted(names)}")
            elif not module:
                banned_names = names & _BANNED_LEAF_PARENTS
                if banned_names:
                    hits.append(f"from {'.' * level} import {sorted(banned_names)}")
    elif isinstance(node, ast.Import):
        for alias in node.names:
            n = alias.name
            if n.startswith("tester_agent."):
                hay = n[len("tester_agent.") :]
                if _banned_target(hay):
                    hits.append(f"import {n}")
            elif n in _BANNED_LEAF_PARENTS or any(
                n.startswith(b + ".") for b in _BANNED_LEAF_PARENTS
            ):
                hits.append(f"import {n}")
    return hits


def test_context_leaf_no_reverse_deps():
    """context 叶子层反向依赖零命中（retrieval/ 除外，可依赖 adapters/store/runtime）。"""
    ctx_root = _PKG / "context"
    violations: list[str] = []
    for path in _iter_py(ctx_root):
        rel = path.relative_to(_PKG)
        parts = rel.parts
        if len(parts) >= 2 and parts[1] == "retrieval":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for frag in _leaf_import_violations(node):
                violations.append(f"{rel}: {frag}")
    assert violations == [], f"context 叶子反向依赖：{violations}"


def test_context_package_no_reme_writer():
    """context 全包（含 retrieval）零引用 ReMe 写接口。"""
    violations: list[str] = []
    for path in _iter_py(_PKG / "context"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                names = {a.name for a in node.names}
                hit = _BANNED_WRITER & names
                if hit and (
                    module.endswith("adapters.reme")
                    or "adapters" in module
                    or module == ""
                ):
                    rel = path.relative_to(_PKG)
                    violations.append(f"{rel}: {sorted(hit)}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _BANNED_WRITER:
                        rel = path.relative_to(_PKG)
                        violations.append(f"{rel}: [{alias.name}]")
    assert violations == [], f"context 禁入 ReMeWriter：{violations}"


def test_scenario_12a_graph_runtime_store_no_writer_still_holds():
    """场景 12a 不回退：graph/runtime/store 仍禁 Writer。"""
    violations: list[str] = []
    for sub in ("graph", "runtime", "store"):
        for path in _iter_py(_PKG / sub):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                names = {a.name for a in node.names}
                hit = _BANNED_WRITER & names
                if hit and (
                    module.endswith("adapters.reme") or "adapters" in module
                ):
                    violations.append(
                        f"{path.relative_to(_PKG)}: {sorted(hit)}"
                    )
    assert violations == [], f"L3 禁入 ReMe 写接口：{violations}"
