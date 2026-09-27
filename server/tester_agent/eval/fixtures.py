"""eval fixtures 加载（WP-13，dd §15.3）。

目录约定（相对 fixtures_root，默认 ``server/tests/fixtures/``）：

- ``knowledge/*.json``：假知识库条目（5 类知识 + link_index），多文件按文件名
  序合并为一份条目列表，整体注入 FakeReMeReader；
- ``requirements/*.json``：构造需求与期望产物（冒烟集），按 name 排序执行。

所有 fixture 文件 ``extra="forbid"``——写错字段名立即报错，不允许静默漂移。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..adapters.reme import Entry
from ..domain import EntryType
from ..errors import ValidationError

# server/tester_agent/eval/fixtures.py → parents[2] = server/
FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

KNOWLEDGE_DIR = "knowledge"
REQUIREMENTS_DIR = "requirements"

# 冒烟集 = requirements/ 全量用例；后续黄金集（S7）可在此加预设过滤规则
PRESETS: dict[str, None] = {"smoke": None}

# fixture 条目统一版本/时间戳（确定性；覆盖真实 ReMe 的版本字段形态）
_ENTRY_UPDATED_AT = "2026-09-27T00:00:00.000Z"


class EntrySpec(BaseModel):
    """knowledge fixture 单条知识（raw 自动携带 summary，对齐镜像派生口径）。"""

    model_config = ConfigDict(extra="forbid")

    entry_id: str
    entry_type: EntryType
    title: str
    content: str
    summary: str = ""
    link_id: str | None = None
    story_id: str | None = None

    def to_entry(self) -> Entry:
        return Entry(
            entry_id=self.entry_id,
            entry_version=f"ver-{self.entry_id}",
            title=self.title,
            content=self.content,
            entry_type=self.entry_type,
            link_id=self.link_id,
            story_id=self.story_id,
            updated_at=_ENTRY_UPDATED_AT,
            raw={"summary": self.summary},
        )


class KnowledgeFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[EntrySpec]


class MultiQueryScript(BaseModel):
    """multi_query 的 LLM 脚本响应（query_paths>=2 时必需，否则记 degraded）。"""

    model_config = ConfigDict(extra="forbid")

    keyword_queries: list[str] = []
    rewrite_queries: list[str] = []


class LLMScript(BaseModel):
    """管线辅助 LLM 调用脚本：multi_query 响应体 + rerank 打分表。

    rerank_scores 只需覆盖关心的 entry_id——未覆盖候选保留规则分（算子口径，
    部分成功不记降级）；不在实际候选集合内的 id 被 rerank 解析器丢弃。
    """

    model_config = ConfigDict(extra="forbid")

    multi_query: MultiQueryScript | None = None
    rerank_scores: dict[str, float] = {}


class Expected(BaseModel):
    """期望产物（dd §15.3）：期望条目 + 脚本化生成文本 + 条款映射。

    - entries：期望被召回/注入的 entry_id 集合（recall_hit/inject_hit 分子）；
    - generated_texts：期望产物文本，经 FakeLLM 回放为生成主调用产出，
      其中的 [ID:xxx] 标签驱动引用闭环（reference_rate/hallucinated）；
    - entry_clauses：entry → 支撑的条款 id，clause_coverage 以此计算
      （覆盖矩阵 WP-20 落地前的冒烟期代理口径，见交接单）。
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[str] = []
    generated_texts: list[str] = []
    entry_clauses: dict[str, list[str]] = {}


class EvalCase(BaseModel):
    """单条 eval 用例：一次管线调用 + 一次（或多次）脚本化生成。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    stage: str
    intent: str
    requirement: str = ""
    scope: dict | None = None
    clauses: list[str] = []
    llm: LLMScript = LLMScript()
    expected: Expected = Expected()


def load_knowledge(root: Path = FIXTURES_ROOT) -> list[Entry]:
    """加载 knowledge/ 全部条目（按文件名序合并），供 FakeReMeReader 注入。"""
    kdir = root / KNOWLEDGE_DIR
    if not kdir.is_dir():
        raise ValidationError("eval 知识库目录不存在", details={"path": str(kdir)})
    entries: list[Entry] = []
    for path in sorted(kdir.glob("*.json")):
        spec = KnowledgeFile.model_validate(_load_json(path))
        entries.extend(e.to_entry() for e in spec.entries)
    if not entries:
        raise ValidationError("eval 知识库为空", details={"path": str(kdir)})
    return entries


def load_cases(
    root: Path = FIXTURES_ROOT,
    *,
    preset: str = "smoke",
    stage: str | None = None,
) -> list[EvalCase]:
    """加载指定预设的用例（当前仅 smoke=全量），可按阶段过滤。"""
    if preset not in PRESETS:
        raise ValidationError(
            "未知 eval 预设", details={"got": preset, "allowed": sorted(PRESETS)}
        )
    rdir = root / REQUIREMENTS_DIR
    if not rdir.is_dir():
        raise ValidationError("eval 用例目录不存在", details={"path": str(rdir)})
    cases = [EvalCase.model_validate(_load_json(p)) for p in sorted(rdir.glob("*.json"))]
    cases.sort(key=lambda c: c.name)
    if stage is not None:
        cases = [c for c in cases if c.stage == stage]
    return cases


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValidationError("eval fixture 读取失败", details={"path": str(path), "error": str(e)}) from e
