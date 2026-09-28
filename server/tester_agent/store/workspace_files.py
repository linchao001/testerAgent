"""文件存储（dd §4 §5）：原子写、偏移读、路径安全、哈希。

- 目录布局（dd §4.1）：
  ``data/workspaces/{workspace_id}/{task_id}/{requirement.md,cases/,snapshots/}``
- 所有业务路径必须由本模块拼出（禁止业务代码自行 join）；ws/task/stage/node/batch
  走安全段校验，外部传入的 rel_path 经 resolve 后做 task 目录包含校验，越界抛
  ``PathEscapeError``（dd §17.1：编程错误，wrap() 兜底按 INTERNAL 处理）。
- 用例提交：渲染 MD（front-matter 先占位 hash）→ 规范化文本算 sha256 →
  tmp 同目录写盘 fsync → rename（dd §4.2 规则 3、§5 实现约束、§11.1 崩溃矩阵）。
- 快照：同步 ``SnapshotWriter`` 追加 JSONL，append 返回字节偏移/长度供
  context_snapshot.items 记录（dd §4.4）。

文件阻塞调用经 ``asyncio.to_thread`` 落到默认 executor；SnapshotWriter 按冻结签名
保持同步（dd §5：append 很快，节点内顺序使用）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mistune
import yaml

from ..domain import CaseFileContent, CaseStep, FileRef, TraceRefs
from ..errors import FileConflict, NotFoundError, PathEscapeError

HASH_PREFIX = "sha256:"
FOOTER_NOTE = (
    "_本文件由用例智能体生成，评审状态以平台为准；手工编辑后平台将提示 hash 变化。_"
)
_PLACEHOLDER_HASH = HASH_PREFIX + "pending"
# intake 条款缓存文件名（dd §4.3：与 requirement.md 同目录，含字节偏移，可再生）
_CLAUSES_CACHE_NAME = "requirement.clauses.json"
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FM_HASH_LINE_RE = re.compile(r"(?m)^content_hash:[^\n]*\n?")
_FM_RE = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_EXPECT_RE = re.compile(r"^\s*[-*]?\s*预期\s*[：:]\s*(.*)$")

# renderer=None：mistune 返回 block token 树（3.x AST renderer 已移除，token 即 AST）
_md_tokens = mistune.create_markdown(renderer=None)


# ---------- 对外数据形态 ----------


@dataclass(frozen=True)
class WrittenCase:
    # file_path 相对任务目录（cases/v{n}/...），落 testcase.file_path，
    # 与 list_case_files/hash_of/read_case 的 rel_path 同一基准（dd §11.3 对账可比）
    file_path: str
    content_hash: str


@dataclass(frozen=True)
class PathInfo:
    """list_case_files 项：rel_path 相对任务目录；仅元数据，不做内容 hash
    （对账另调 hash_of，dd §11.3）。"""

    rel_path: str
    size_bytes: int


@dataclass(frozen=True)
class CleanupReport:
    tmp_files_removed: int
    bytes_freed: int


# PathEscapeError 的唯一定义点在 errors.py（dd §17.1）；上方已导入，
# 经 __all__ 再导出以兼容既有 ``from store.workspace_files import PathEscapeError``。


# ---------- 纯函数：slug / hash / MD 渲染与解析 ----------


def slugify(title: str) -> str:
    """标题 → slug（dd §4.1）：NFKC → 小写 → 非字母数字转 - → 折叠 → 截断 40。

    保留 Unicode 字母数字（中文标题生成中文 slug）；无可用字符时回退 "case"。
    """
    normalized = unicodedata.normalize("NFKC", title).lower()
    slug = "".join(ch if ch.isalnum() else "-" for ch in normalized)
    slug = re.sub(r"-+", "-", slug).strip("-")
    slug = slug[:40].rstrip("-")
    return slug or "case"


def case_rel_path(
    case_id: str, point_id: str, title: str, version: int
) -> str:
    """用例相对路径（dd §4.1）：cases/v{n}/{case_id8}--{point_id8}--{slug}.md。

    唯一性由 case_id8 前缀保证；slug 仅为可读性，冲突不特殊处理（dd §4.1）。
    """
    return (
        f"cases/v{int(version)}/"
        f"{case_id[:8]}--{point_id[:8]}--{slugify(title)}.md"
    )


def canonical_case_text(md_text: str) -> str:
    """hash 规范化文本（dd §4.2 规则 3）：CRLF→LF、剔除 front-matter 中
    content_hash 字段行、末尾去空行。"""
    text = md_text.replace("\r\n", "\n").replace("\r", "\n")
    match = _FM_RE.match(text)
    if match:
        fm = _FM_HASH_LINE_RE.sub("", match.group(1))
        tail = text[match.end():]
        if not tail.startswith("\n"):
            tail = "\n" + tail
        text = "---\n" + fm + "\n---" + tail
    return text.strip("\n")


def hash_case_text(md_text: str) -> str:
    """规范化文本 → ``sha256:<hex>``（读写两侧同一算法，dd §4.2 规则 3）。"""
    digest = hashlib.sha256(canonical_case_text(md_text).encode("utf-8")).hexdigest()
    return HASH_PREFIX + digest


def render_case_markdown(content: CaseFileContent, *, content_hash: str) -> str:
    """渲染 v1 模板（dd §4.2）：YAML front-matter + 固定段标题正文。

    渲染两遍（占位 hash → 真值 hash）由调用方完成；本函数只负责单次渲染。
    """
    fm_obj: dict[str, Any] = {
        "case_id": content.case_id,
        "point_id": content.point_id,
        "stage_version": content.stage_version,
        "priority": content.priority,
        "content_hash": content_hash,
        "trace_refs": content.trace_refs.model_dump(),
    }
    fm_yaml = yaml.safe_dump(
        fm_obj, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).rstrip("\n")
    lines: list[str] = ["---", fm_yaml, "---", "", f"# {_one_line(content.title)}", ""]

    if content.preconditions:
        lines.append("## 前置条件")
        for item in content.preconditions:
            lines.append(f"- {_one_line(item)}")
        lines.append("")

    lines.append("## 步骤")
    for i, step in enumerate(content.steps, start=1):
        lines.append(f"{i}. {_one_line(step.action)}")
        expect = step.expect.strip()
        if expect:
            # 缩进子项形态；解析器同时兼容软换行内联形态（dd §4.2 规则 2）
            lines.append(f"   - 预期：{_one_line(expect)}")
    lines.append("")

    if content.test_data and content.test_data.strip():
        lines.append("## 测试数据")
        lines.append(content.test_data.strip())
        lines.append("")

    lines.append("## 说明")
    lines.append(FOOTER_NOTE)
    return "\n".join(lines) + "\n"


def parse_case_markdown(md_text: str) -> CaseFileContent:
    """严格但宽容地解析用例 MD（dd §4.2 规则 4）。

    front-matter 缺失/YAML 非法/缺 case_id/缺 H1 标题 → ValueError（调用方据此让
    结构化字段降级，原始 MD 仍可渲染编辑）；段缺失宽容（steps 可为空）。
    """
    text = md_text.replace("\r\n", "\n").replace("\r", "\n")
    match = _FM_RE.match(text)
    if not match:
        raise ValueError("用例 Markdown 缺少 YAML front-matter")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"front-matter YAML 非法：{exc}") from exc
    if not isinstance(meta, dict) or not meta.get("case_id"):
        raise ValueError("front-matter 缺少 case_id")

    body = text[match.end():].lstrip("\n")
    tokens, _state = _md_tokens.parse(body)
    sections = _split_sections(tokens)

    title = sections.pop("__h1__", None)
    if not title:
        raise ValueError("用例 Markdown 缺少一级标题")

    preconditions = _parse_bullets(sections.get("前置条件", []))
    steps = _parse_steps(sections.get("步骤", []))
    test_data = _parse_text_section(sections.get("测试数据", []))

    return CaseFileContent(
        case_id=str(meta["case_id"]),
        point_id=str(meta.get("point_id", "")),
        stage_version=int(meta.get("stage_version", 0)),
        title=title,
        priority=meta.get("priority", "P1"),
        preconditions=preconditions,
        steps=steps,
        test_data=test_data,
        trace_refs=TraceRefs.model_validate(meta.get("trace_refs") or {}),
    )


# ---------- MD 解析助手（mistune token 树） ----------


def _one_line(text: str) -> str:
    """列表项内不允许多行：折叠换行，保护模板结构。"""
    return re.sub(r"\s*\n\s*", " ", text).strip()


def _token_text(token: dict[str, Any]) -> str:
    """递归取 token 内联纯文本；linebreak/softbreak 输出换行（保留步骤边界）。"""
    ttype = token.get("type")
    if ttype in ("linebreak", "softbreak"):
        return "\n"
    children = token.get("children")
    if children:
        return "".join(_token_text(child) for child in children)
    return str(token.get("raw", ""))


def _split_sections(tokens: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按 H1/H2 切节：H1 文本入 __h1__，H2 之后到下个 H2 的 token 入对应段名。"""
    sections: dict[str, list[dict[str, Any]]] = {}
    current = "__preamble__"
    sections[current] = []
    for tok in tokens:
        if tok.get("type") == "heading":
            level = (tok.get("attrs") or {}).get("level")
            heading = _token_text(tok).strip()
            if level == 1:
                sections["__h1__"] = [{"type": "text", "raw": heading}]
                current = "__preamble__"
                sections.setdefault(current, [])
                continue
            if level == 2:
                current = heading
                sections.setdefault(current, [])
                continue
        sections.setdefault(current, []).append(tok)
    sections["__h1__"] = _token_text(
        {"children": sections.get("__h1__", [])}
    ).strip() if sections.get("__h1__") else ""
    return sections


def _first_list(tokens: list[dict[str, Any]], *, ordered: bool) -> dict[str, Any] | None:
    for tok in tokens:
        if tok.get("type") == "list":
            attrs = tok.get("attrs") or {}
            if bool(attrs.get("ordered")) == ordered:
                return tok
    return None


def _parse_bullets(tokens: list[dict[str, Any]]) -> list[str]:
    lst = _first_list(tokens, ordered=False)
    if not lst:
        return []
    items: list[str] = []
    for item in lst.get("children", []):
        blocks = item.get("children", [])
        text_parts = [
            _token_text(b).strip()
            for b in blocks
            if b.get("type") != "list"
        ]
        text = " ".join(p for p in text_parts if p)
        if text:
            items.append(text)
    return items


def _parse_steps(tokens: list[dict[str, Any]]) -> list[CaseStep]:
    lst = _first_list(tokens, ordered=True)
    if not lst:
        return []
    steps: list[CaseStep] = []
    for seq, item in enumerate(lst.get("children", []), start=1):
        action_lines: list[str] = []
        expect_parts: list[str] = []
        for block in item.get("children", []):
            if block.get("type") == "list":
                # 缩进子项形态：子列表项文本应为 "预期：..."
                for sub in block.get("children", []):
                    sub_text = " ".join(
                        _token_text(b).strip()
                        for b in sub.get("children", [])
                        if b.get("type") != "list"
                    ).strip()
                    match = _EXPECT_RE.match(sub_text)
                    expect_parts.append(match.group(1).strip() if match else sub_text)
            else:
                for line in _token_text(block).split("\n"):
                    match = _EXPECT_RE.match(line)
                    if match:
                        # 软换行内联形态：步骤段落里续了 "预期：..."（dd §4.2 规则 2）
                        expect_parts.append(match.group(1).strip())
                    elif line.strip():
                        action_lines.append(line.strip())
        steps.append(
            CaseStep(
                seq=seq,
                action=" ".join(action_lines),
                expect="\n".join(e for e in expect_parts if e),
            )
        )
    return steps


def _parse_text_section(tokens: list[dict[str, Any]]) -> str | None:
    parts: list[str] = []
    for tok in tokens:
        if tok.get("type") in ("blank_line",):
            continue
        text = _token_text(tok).strip()
        if text:
            parts.append(text)
    joined = "\n".join(parts).strip()
    return joined or None


# ---------- FileStore ----------


class FileStore:
    def __init__(self, data_dir: Path | str):
        self._root = Path(data_dir).expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    # ---- 路径拼装与安全 ----

    @staticmethod
    def _safe_segment(value: str, label: str) -> str:
        if not isinstance(value, str) or not _SAFE_SEGMENT_RE.match(value):
            raise PathEscapeError(f"非法路径段 {label}={value!r}")
        return value

    def _task_dir(self, ws: str, task: str) -> Path:
        self._safe_segment(ws, "workspace_id")
        self._safe_segment(task, "task_id")
        return self._root / "workspaces" / ws / task

    def _resolve_under_task(self, ws: str, task: str, rel_path: str) -> Path:
        """外部 rel_path：resolve 后必须仍在对应 task 目录内（dd §4.1）。"""
        base = self._task_dir(ws, task).resolve()
        target = (base / rel_path).resolve()
        if target != base and base not in target.parents:
            raise PathEscapeError(
                f"路径越界：{rel_path!r} 不在任务目录 {base} 之下"
            )
        return target

    # ---- 需求 ----

    async def save_requirement(self, ws: str, task: str, md: str) -> FileRef:
        return await asyncio.to_thread(self._save_requirement_sync, ws, task, md)

    def _save_requirement_sync(self, ws: str, task: str, md: str) -> FileRef:
        text = md.replace("\r\n", "\n").replace("\r", "\n")  # UTF-8, LF（dd §4.3）
        data = text.encode("utf-8")
        target = self._task_dir(ws, task) / "requirement.md"
        _atomic_write(target, data)
        return FileRef(
            path=self._posix_rel_root(target),
            content_hash=HASH_PREFIX + hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )

    async def read_requirement(self, ws: str, task: str) -> str:
        return await asyncio.to_thread(self._read_requirement_sync, ws, task)

    def _read_requirement_sync(self, ws: str, task: str) -> str:
        target = self._task_dir(ws, task) / "requirement.md"
        if not target.is_file():
            raise NotFoundError(f"需求原文不存在：ws={ws} task={task}")
        return target.read_text(encoding="utf-8")

    async def read_clause(
        self, ws: str, task: str, span: tuple[int, int]
    ) -> str:
        return await asyncio.to_thread(
            self._read_clause_sync, ws, task, int(span[0]), int(span[1])
        )

    def _read_clause_sync(self, ws: str, task: str, start: int, end: int) -> str:
        target = self._task_dir(ws, task) / "requirement.md"
        if not target.is_file():
            raise NotFoundError(f"需求原文不存在：ws={ws} task={task}")
        if start < 0 or end < start:
            raise PathEscapeError(f"非法字节偏移：[{start}, {end})")
        with target.open("rb") as fp:
            return fp.read()[start:end].decode("utf-8")

    # ---- 条款缓存（requirement.clauses.json，dd §4.3）----

    async def save_clauses_cache(self, ws: str, task: str, clauses: list[dict]) -> None:
        """intake 落条款缓存（含 start/end 字节偏移的运行期字段）。

        可再生缓存（dd §4.3）：同输入重写内容恒等，原子写保证无半文件。
        """
        return await asyncio.to_thread(self._save_clauses_cache_sync, ws, task, clauses)

    def _save_clauses_cache_sync(self, ws: str, task: str, clauses: list[dict]) -> None:
        target = self._task_dir(ws, task) / _CLAUSES_CACHE_NAME
        data = json.dumps(clauses, ensure_ascii=False, indent=1).encode("utf-8")
        _atomic_write(target, data)

    async def read_clauses_cache(self, ws: str, task: str) -> list[dict]:
        return await asyncio.to_thread(self._read_clauses_cache_sync, ws, task)

    def _read_clauses_cache_sync(self, ws: str, task: str) -> list[dict]:
        target = self._task_dir(ws, task) / _CLAUSES_CACHE_NAME
        if not target.is_file():
            raise NotFoundError(f"条款缓存不存在（可再生，由 intake 重算）：ws={ws} task={task}")
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"条款缓存格式非法（应为 JSON 数组）：{target}")
        return data

    # ---- 用例（原子提交）----

    async def write_case(
        self, ws: str, task: str, version: int, content: CaseFileContent
    ) -> WrittenCase:
        return await asyncio.to_thread(
            self._write_case_sync, ws, task, version, content
        )

    def _write_case_sync(
        self, ws: str, task: str, version: int, content: CaseFileContent
    ) -> WrittenCase:
        self._task_dir(ws, task)  # 仅触发安全段校验
        rel = case_rel_path(content.case_id, content.point_id, content.title, version)
        target = self._resolve_under_task(ws, task, rel)

        placeholder = render_case_markdown(content, content_hash=_PLACEHOLDER_HASH)
        new_hash = hash_case_text(placeholder)
        final_md = render_case_markdown(content, content_hash=new_hash)

        if target.is_file():
            existing = target.read_text(encoding="utf-8")
            if hash_case_text(existing) == new_hash:
                # 幂等重放：同内容已提交，直接返回（dd §5/§11.1 崩溃矩阵）
                return WrittenCase(rel, new_hash)

        _atomic_write(target, final_md.encode("utf-8"))
        return WrittenCase(rel, new_hash)

    async def read_case(self, ws: str, task: str, rel_path: str) -> str:
        return await asyncio.to_thread(
            self._read_text_sync, ws, task, rel_path
        )

    async def edit_case(
        self,
        ws: str,
        task: str,
        rel_path: str,
        content: CaseFileContent,
        *,
        expected_hash: str,
    ) -> WrittenCase:
        """编辑用例（WP-27 dd §10.3④）：服务端重算元数据重渲染 → 两遍 hash →
        带 expected_hash 乐观锁覆盖（If-Match）。

        与 write_case 的差异：不重算落盘路径（沿用 row.file_path，标题改名
        不迁移文件——slug 仅可读性，唯一性由 case_id 前缀保证，dd §4.1）。
        """
        return await asyncio.to_thread(
            self._edit_case_sync, ws, task, rel_path, content, expected_hash
        )

    def _edit_case_sync(
        self,
        ws: str,
        task: str,
        rel_path: str,
        content: CaseFileContent,
        expected_hash: str,
    ) -> WrittenCase:
        placeholder = render_case_markdown(content, content_hash=_PLACEHOLDER_HASH)
        new_hash = hash_case_text(placeholder)
        final_md = render_case_markdown(content, content_hash=new_hash)
        return self._overwrite_case_sync(
            ws, task, rel_path, final_md, expected_hash
        )

    async def overwrite_case(
        self,
        ws: str,
        task: str,
        rel_path: str,
        md: str,
        expected_hash: str | None,
    ) -> WrittenCase:
        return await asyncio.to_thread(
            self._overwrite_case_sync, ws, task, rel_path, md, expected_hash
        )

    def _overwrite_case_sync(
        self,
        ws: str,
        task: str,
        rel_path: str,
        md: str,
        expected_hash: str | None,
    ) -> WrittenCase:
        target = self._resolve_under_task(ws, task, rel_path)
        if not target.is_file():
            raise NotFoundError(f"用例文件不存在：{rel_path}")
        if expected_hash is not None:
            current = hash_case_text(target.read_text(encoding="utf-8"))
            if _normalize_hash(expected_hash) != _normalize_hash(current):
                raise FileConflict(
                    f"用例已被他人修改：{rel_path}",
                    details={"expected": expected_hash, "current": current},
                )

        text = md.replace("\r\n", "\n").replace("\r", "\n")
        new_hash = hash_case_text(text)
        match = _FM_RE.match(text)
        if not match:
            raise ValueError("编辑后的用例 Markdown 缺少 YAML front-matter")
        if _FM_HASH_LINE_RE.search(match.group(1)):
            updated_fm = _FM_HASH_LINE_RE.sub(
                f"content_hash: {new_hash}\n", match.group(1), count=1
            )
        else:
            updated_fm = f"content_hash: {new_hash}\n" + match.group(1)
        # 固定 FM/正文边界：正文前所有空行剥离后统一补一个 \n，避免逐次覆盖
        # 丢失换行导致 _FM_RE 失配（canonical 对 1 个与 2 个换行等价）
        body = text[match.end():].lstrip("\n")
        rewritten = "---\n" + updated_fm.strip("\n") + "\n---\n" + body
        # 自校验：落盘算法必须能算出同一 hash（防 FM 替换逻辑回归）
        assert hash_case_text(rewritten) == new_hash
        _atomic_write(target, rewritten.encode("utf-8"))
        return WrittenCase(self._posix_rel_task(ws, task, target), new_hash)

    def _read_text_sync(self, ws: str, task: str, rel_path: str) -> str:
        target = self._resolve_under_task(ws, task, rel_path)
        if not target.is_file():
            raise NotFoundError(f"文件不存在：{rel_path}")
        return target.read_text(encoding="utf-8")

    async def hash_of(self, ws: str, task: str, rel_path: str) -> str:
        """对盘上用例文件算 hash（dd §11.3 Reconciler 使用）。"""
        return await asyncio.to_thread(self._hash_of_sync, ws, task, rel_path)

    def _hash_of_sync(self, ws: str, task: str, rel_path: str) -> str:
        return hash_case_text(self._read_text_sync(ws, task, rel_path))

    # ---- 快照 ----

    def open_snapshot_writer(
        self,
        ws: str,
        task: str,
        stage: str,
        version: int,
        node: str,
        batch_id: str | None,
    ) -> "SnapshotWriter":
        """打开（或追加）一个 full 档快照 JSONL（dd §4.4）。

        文件名 ``{batch_id or 'na'}-{node}-{snapshot_id8}.jsonl``；同批次同节点
        由调用方复用同一 writer，本方法每次调用生成新 snapshot_id。
        """
        task_dir = self._task_dir(ws, task)
        self._safe_segment(stage, "stage")
        self._safe_segment(node, "node")
        if batch_id is not None:
            self._safe_segment(batch_id, "batch_id")
        snapshot_id = uuid.uuid4().hex
        rel = (
            f"snapshots/{stage}/v{int(version)}/"
            f"{batch_id or 'na'}-{node}-{snapshot_id[:8]}.jsonl"
        )
        target = self._resolve_under_task(ws, task, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        return SnapshotWriter(path=target, rel_path=rel, snapshot_id=snapshot_id)

    async def read_snapshot_line(
        self, ws: str, task: str, rel_path: str, offset: int, length: int
    ) -> str:
        return await asyncio.to_thread(
            self._read_snapshot_line_sync, ws, task, rel_path, offset, length
        )

    def _read_snapshot_line_sync(
        self, ws: str, task: str, rel_path: str, offset: int, length: int
    ) -> str:
        if offset < 0 or length < 0:
            raise PathEscapeError(f"非法快照偏移：offset={offset} length={length}")
        target = self._resolve_under_task(ws, task, rel_path)
        if not target.is_file():
            raise NotFoundError(f"快照文件不存在：{rel_path}")
        with target.open("rb") as fp:
            fp.seek(offset)
            return fp.read(length).decode("utf-8")

    # ---- 维护 ----

    async def export_zip_path(self, ws: str, task: str, job_id: str) -> Path:
        """导出 zip 的目标绝对路径（WP-27）：exports/{job_id}/export.zip。

        仅拼路径不建目录；调用方（ExportService）在后台线程写盘。安全段校验
        沿用 task 目录口径（exports/ 在任务目录下，dd §4.1 路径唯一来源）。
        """
        target = self._task_dir(ws, task) / "exports" / job_id / "export.zip"
        self._safe_segment(job_id, "job_id")
        return target

    async def list_case_files(self, ws: str, task: str) -> list[PathInfo]:
        return await asyncio.to_thread(self._list_case_files_sync, ws, task)

    def _list_case_files_sync(self, ws: str, task: str) -> list[PathInfo]:
        cases_dir = self._task_dir(ws, task) / "cases"
        if not cases_dir.is_dir():
            return []
        infos: list[PathInfo] = []
        for path in sorted(cases_dir.glob("v*/*.md")):
            if not path.is_file():
                continue
            infos.append(
                PathInfo(
                    rel_path=self._posix_rel_task(ws, task, path),
                    size_bytes=path.stat().st_size,
                )
            )
        return infos

    async def soft_cleanup(self, retention_days: int) -> CleanupReport:
        """清理崩溃遗留的 ``.tmp.*`` 临时文件（dd §11.1 崩溃矩阵、§6.5 惰性清理）。

        仅删 mtime 早于 cutoff 的临时文件；快照/用例的保留期清理由
        runtime/maintenance（WP-29）联动 DB 行执行，不在本方法范围。
        """
        return await asyncio.to_thread(self._soft_cleanup_sync, retention_days)

    def _soft_cleanup_sync(self, retention_days: int) -> CleanupReport:
        cutoff = time.time() - int(retention_days) * 86400
        workspaces = self._root / "workspaces"
        removed = 0
        freed = 0
        if not workspaces.is_dir():
            return CleanupReport(0, 0)
        for path in workspaces.rglob("*"):
            if not path.is_file() or ".tmp." not in path.name:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                continue
            size = stat.st_size
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed += 1
            freed += size
        return CleanupReport(removed, freed)

    async def cleanup_exports(self, retention_days: int) -> CleanupReport:
        """清理任务 exports/ 目录下超保留期的导出 zip（WP-29）。

        导出 zip 落在 ``exports/{job_id}/export.zip``；按 mtime 早于 cutoff
        整目录删除（含空 job 目录）。不碰 cases/snapshots。
        """
        return await asyncio.to_thread(self._cleanup_exports_sync, retention_days)

    def _cleanup_exports_sync(self, retention_days: int) -> CleanupReport:
        cutoff = time.time() - int(retention_days) * 86400
        workspaces = self._root / "workspaces"
        removed = 0
        freed = 0
        if not workspaces.is_dir():
            return CleanupReport(0, 0)
        for task_dir in workspaces.rglob("*/tasks/*"):
            exports = task_dir / "exports"
            if not exports.is_dir():
                continue
            for job_dir in exports.iterdir():
                if not job_dir.is_dir():
                    continue
                try:
                    stat = job_dir.stat()
                except OSError:
                    continue
                if stat.st_mtime >= cutoff:
                    continue
                # 统计目录内文件大小后删除
                try:
                    files = [p for p in job_dir.rglob("*") if p.is_file()]
                except OSError:
                    files = []
                for f in files:
                    try:
                        freed += f.stat().st_size
                        f.unlink()
                        removed += 1
                    except OSError:
                        continue
                try:
                    job_dir.rmdir()
                except OSError:
                    pass
        return CleanupReport(removed, freed)

    # ---- 内部工具 ----

    def _posix_rel_root(self, target: Path) -> str:
        """相对 data/（FileRef.path，dd §2.9）。"""
        return target.resolve().relative_to(self._root).as_posix()

    def _posix_rel_task(self, ws: str, task: str, target: Path) -> str:
        """相对任务目录（用例/快照 rel_path：cases/..、snapshots/..，dd §4.1）。"""
        return target.resolve().relative_to(self._task_dir(ws, task)).as_posix()


def _normalize_hash(value: str) -> str:
    """允许比对方带不带 sha256: 前缀。"""
    return value if value.startswith(HASH_PREFIX) else HASH_PREFIX + value


def _atomic_write(target: Path, data: bytes) -> None:
    """tmp 同目录写盘 → flush+fsync → rename（dd §5）。

    rename 失败时保留 .tmp 文件交 soft_cleanup 回收（dd §11.1 崩溃矩阵：
    "tmp 写盘中 → 仅留 .tmp 文件 → 下次维护清理"）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=f".tmp.{uuid.uuid4().hex}", dir=target.parent
    )
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, target)
        _fsync_dir(target.parent)
    except BaseException:
        # 崩溃/失败现场：不主动删除 tmp，由维护任务按保留期清理（与 kill -9 同构）
        raise


def _fsync_dir(directory: Path) -> None:
    """目录项 fsync，保证 rename 在崩溃后仍持久（POSIX）。"""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class SnapshotWriter:
    """full 档快照 JSONL 同步追加器（dd §4.4/§5）。"""

    def __init__(self, *, path: Path, rel_path: str, snapshot_id: str):
        self._path = path
        self._fp = path.open("ab")
        self.rel_path = rel_path
        self.snapshot_id = snapshot_id

    def append(self, line: dict[str, Any]) -> tuple[int, int]:
        """写一行，返回 (byte_offset, byte_length)；length 不含行尾 \\n。"""
        payload = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
        offset = self._fp.tell()
        self._fp.write(payload)
        self._fp.flush()
        os.fsync(self._fp.fileno())
        return offset, len(payload) - 1

    def tell(self) -> int:
        return self._fp.tell()

    def close(self) -> None:
        if self._fp is not None and not self._fp.closed:
            self._fp.flush()
            os.fsync(self._fp.fileno())
            self._fp.close()

    def __enter__(self) -> "SnapshotWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# 便于上层 from store.workspace_files import FileRef 等统一导入
__all__ = [
    "FileStore",
    "SnapshotWriter",
    "WrittenCase",
    "PathInfo",
    "CleanupReport",
    "PathEscapeError",
    "canonical_case_text",
    "hash_case_text",
    "render_case_markdown",
    "parse_case_markdown",
    "case_rel_path",
    "slugify",
    "FileRef",
]
