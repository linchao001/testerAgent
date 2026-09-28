"""ReMe HTTP 真实适配（WP-09 / SP-1：优先 service 模式）。

caps 固定为 SP-1 结论：``metadata_filter=False``、``entry_version=False``、
``passage_api=True``。读路径走 ``knowledge_search`` / ``read`` /
``frontmatter_read`` / ``list``；写路径走 ``save_to_knowledge``（令牌门禁仍
在 api/kb.py，本模块不校验 token）。

与 dd §9.1「S1 前优先 SDK」的偏离：以 SP-1 交接单为准选 HTTP，避免嵌入
``reme-ai[core]`` 重依赖。sdk mode 本包不注册。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import PurePosixPath
from typing import Any

import httpx
import yaml

from ..domain import EntryType
from ..errors import KbUnreachable, NotFoundError, ValidationError
from .reme import (
    Entry,
    IndexLink,
    IndexStory,
    IndexTree,
    ReMeCaps,
    ReMeReaderFactory,
    WriteResult,
)

logger = logging.getLogger(__name__)

# SP-1 固定能力位（探测结论写入交接单；连接测试直接回填，不做运行时再探）
SP1_CAPS = ReMeCaps(
    metadata_filter=False,
    entry_version=False,
    passage_api=True,
)

_DEFAULT_TIMEOUT = 30.0
_CHAIN_PREFIX = "chain:"

# bucket → EntryType（SP-1 草案；未知默认 business）
_BUCKET_TYPE: dict[str, EntryType] = {
    "business/wiki": EntryType.BUSINESS,
    "business/procedure": EntryType.BUSINESS,
    "business/personal": EntryType.BUSINESS,
    "business/openapi": EntryType.API,
    "business/dbinfo": EntryType.DB,  # path 大小写不敏感
    "business/dbInfo": EntryType.DB,
    "test/defects": EntryType.DEFECT,
    "test/test_cases": EntryType.FLOW_CASE,
    "test/test_design": EntryType.FLOW_CASE,
    "test/test_data": EntryType.FLOW_CASE,
    "test/ui_pages": EntryType.FLOW_CASE,
    "test/ui_locators": EntryType.FLOW_CASE,
}


def bucket_to_entry_type(bucket: str) -> EntryType:
    raw = (bucket or "").strip().replace("\\", "/").strip("/")
    if not raw:
        return EntryType.BUSINESS
    if raw in _BUCKET_TYPE:
        return _BUCKET_TYPE[raw]
    lowered = raw.lower()
    for key, et in _BUCKET_TYPE.items():
        if key.lower() == lowered:
            return et
    # 域级前缀兜底
    if lowered.startswith("test/defect"):
        return EntryType.DEFECT
    if lowered.startswith("business/openapi"):
        return EntryType.API
    if lowered.startswith("business/db"):
        return EntryType.DB
    if lowered.startswith("test/"):
        return EntryType.FLOW_CASE
    return EntryType.BUSINESS


def _bucket_from_path(path: str) -> str:
    """``knowledge/business/wiki/foo.md`` → ``business/wiki``。"""
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if len(parts) >= 3 and parts[0] in {"knowledge", "Knowledge"}:
        return f"{parts[1]}/{parts[2]}"
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def _title_from_path(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).stem


def _parse_chain_signals(signals: Any) -> list[str]:
    """Extract ``chain:<name>`` titles from frontmatter signals list/string."""
    items: list[str]
    if signals is None:
        items = []
    elif isinstance(signals, str):
        # YAML may leave literal ``["a", "b"]`` or comma text
        try:
            parsed = yaml.safe_load(signals)
            if isinstance(parsed, list):
                items = [str(x) for x in parsed]
            else:
                items = [signals]
        except Exception:
            items = [signals]
    elif isinstance(signals, list):
        items = [str(x) for x in signals]
    else:
        items = [str(signals)]
    out: list[str] = []
    for item in items:
        s = item.strip().strip('"').strip("'")
        if s.startswith(_CHAIN_PREFIX):
            name = s[len(_CHAIN_PREFIX) :].strip()
            if name:
                out.append(name)
    return out


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    return meta, m.group(2)


class HttpReMeReader:
    """ReMeReader over ReMe HTTP jobs（结构对型，不继承 Protocol）。"""

    def __init__(
        self,
        *,
        base_url: str,
        kb_id: str,
        client: httpx.AsyncClient | None = None,
        options: dict | None = None,
        knowledge_dir: str = "knowledge",
    ) -> None:
        opts = dict(options or {})
        self.caps = SP1_CAPS
        self.base_url = base_url.rstrip("/")
        self.kb_id = kb_id
        self.knowledge_dir = (
            str(opts.get("knowledge_dir") or knowledge_dir).strip() or "knowledge"
        )
        timeout = float(opts.get("timeout", _DEFAULT_TIMEOUT))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout
        )
        self._default_bucket = str(opts.get("bucket") or "all")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, action: str, payload: dict) -> dict:
        path = f"/{action.lstrip('/')}"
        try:
            resp = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise KbUnreachable(
                f"ReMe HTTP 不可达: {action}",
                details={"action": action, "error": str(exc) or type(exc).__name__},
            ) from exc
        if resp.status_code >= 500:
            raise KbUnreachable(
                f"ReMe HTTP {resp.status_code}: {action}",
                details={"action": action, "status": resp.status_code},
            )
        if resp.status_code >= 400:
            raise KbUnreachable(
                f"ReMe HTTP 客户端错误 {resp.status_code}: {action}",
                details={"action": action, "status": resp.status_code},
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise KbUnreachable(
                f"ReMe HTTP 响应非 JSON: {action}",
                details={"action": action},
            ) from exc
        if not isinstance(data, dict):
            raise KbUnreachable(
                f"ReMe HTTP 响应形态非法: {action}",
                details={"action": action, "got": type(data).__name__},
            )
        return data

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        types: list[str] | None = None,
        scope: dict | None = None,
    ) -> list[Entry]:
        del types, scope  # caps.metadata_filter=False：不传服务端；本地由 IndexMirror 滤
        data = await self._post(
            "knowledge_search",
            {
                "query": query,
                "limit": top_k,
                "bucket": self._default_bucket,
            },
        )
        if not data.get("success", True):
            raise KbUnreachable(
                str(data.get("answer") or "knowledge_search failed"),
                details={"action": "knowledge_search"},
            )
        results = (data.get("metadata") or {}).get("results") or []
        entries: list[Entry] = []
        for raw in results:
            if not isinstance(raw, dict):
                continue
            path = str(raw.get("path") or "").replace("\\", "/")
            if not path:
                continue
            text = str(raw.get("text") or "")
            bucket = _bucket_from_path(path)
            entries.append(
                Entry(
                    entry_id=path,
                    entry_version="",  # 管线经 local_entry_version 补 h-
                    title=_title_from_path(path),
                    content=text,
                    entry_type=bucket_to_entry_type(bucket),
                    link_id=None,
                    story_id=None,
                    updated_at=None,
                    raw={
                        "path": path,
                        "start_line": raw.get("start_line"),
                        "end_line": raw.get("end_line"),
                        "scores": raw.get("scores") or {},
                        "chunk_id": raw.get("id"),
                        "bucket": bucket,
                        "kb_id": self.kb_id,
                    },
                )
            )
        return entries

    async def get_entry(self, entry_id: str) -> Entry:
        data = await self._post("read", {"path": entry_id})
        if not data.get("success", True):
            raise NotFoundError(
                str(data.get("answer") or f"entry not found: {entry_id}"),
                details={"entry_id": entry_id},
            )
        text = str(data.get("answer") or "")
        # read 的 answer 可能带邻居扩展；优先用 metadata 无扩展时的正文
        fm, body = _split_frontmatter(text)
        title = str(fm.get("name") or fm.get("title") or _title_from_path(entry_id))
        bucket = str(fm.get("bucket") or _bucket_from_path(entry_id))
        chains = _parse_chain_signals(fm.get("signals"))
        link_id = chains[0] if chains else None
        story_id = chains[1] if len(chains) > 1 else None
        updated_at = fm.get("updated_at")
        if updated_at is not None:
            if hasattr(updated_at, "isoformat"):
                updated_at = updated_at.isoformat()
            else:
                updated_at = str(updated_at).strip().strip('"') or None
            if isinstance(updated_at, str):
                updated_at = updated_at.strip() or None
        return Entry(
            entry_id=entry_id,
            entry_version="",
            title=title.strip().strip('"'),
            content=body if body.strip() else text,
            entry_type=bucket_to_entry_type(bucket),
            link_id=link_id,
            story_id=story_id,
            updated_at=updated_at,
            raw={"frontmatter": fm, "path": entry_id, "kb_id": self.kb_id},
        )

    async def list_index_tree(self) -> IndexTree:
        data = await self._post(
            "list",
            {
                "path": self.knowledge_dir,
                "recursive": True,
                "limit": 10_000,
                "extensions": ["md"],
            },
        )
        if not data.get("success", True):
            # 目录不存在 → 空树（镜像降级），不阻断
            logger.warning(
                "list_index_tree list failed: %s", data.get("answer")
            )
            return IndexTree(links=[])
        items = (data.get("metadata") or {}).get("items") or []
        paths = [str(p).replace("\\", "/") for p in items if str(p).endswith(".md")]

        async def _fm(path: str) -> tuple[str, dict]:
            try:
                resp = await self._post("frontmatter_read", {"path": path})
            except KbUnreachable:
                return path, {}
            if not resp.get("success", True):
                return path, {}
            meta = (resp.get("metadata") or {}).get("frontmatter") or {}
            return path, meta if isinstance(meta, dict) else {}

        # 并发拉 frontmatter；限制并发避免打爆远端
        sem = asyncio.Semaphore(16)

        async def _bounded(path: str) -> tuple[str, dict]:
            async with sem:
                return await _fm(path)

        pairs = await asyncio.gather(*[_bounded(p) for p in paths])

        # link_id → IndexLink 聚合；story 挂在首个一级 chain 下
        links: dict[str, IndexLink] = {}
        stories_seen: set[tuple[str, str]] = set()
        for _path, fm in pairs:
            chains = _parse_chain_signals(fm.get("signals"))
            if not chains:
                continue
            link_name = chains[0]
            desc = str(fm.get("description") or fm.get("name") or "")
            if link_name not in links:
                links[link_name] = IndexLink(
                    link_id=link_name,
                    entry_id=f"link_index/{link_name}",
                    entry_version="",
                    title=link_name,
                    summary=desc,
                    stories=[],
                )
            elif not links[link_name].summary and desc:
                links[link_name].summary = desc
            for story_name in chains[1:]:
                key = (link_name, story_name)
                if key in stories_seen:
                    continue
                stories_seen.add(key)
                links[link_name].stories.append(
                    IndexStory(
                        story_id=story_name,
                        entry_id=f"link_index/{link_name}/{story_name}",
                        entry_version="",
                        title=story_name,
                        summary=desc,
                    )
                )

        ordered = sorted(links.values(), key=lambda ln: ln.link_id)
        for ln in ordered:
            ln.stories.sort(key=lambda s: s.story_id)
        return IndexTree(links=ordered)


class HttpReMeWriter:
    """ReMeWriter over ``save_to_knowledge``；令牌由确认端校验，此处忽略 token。"""

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        options: dict | None = None,
    ) -> None:
        opts = dict(options or {})
        self.base_url = base_url.rstrip("/")
        timeout = float(opts.get("timeout", _DEFAULT_TIMEOUT))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout
        )
        self._default_bucket = str(opts.get("bucket") or "business/wiki")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, action: str, payload: dict) -> dict:
        path = f"/{action.lstrip('/')}"
        try:
            resp = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise KbUnreachable(
                f"ReMe HTTP 不可达: {action}",
                details={"action": action, "error": str(exc) or type(exc).__name__},
            ) from exc
        if resp.status_code >= 400:
            raise KbUnreachable(
                f"ReMe HTTP 写入失败 {resp.status_code}: {action}",
                details={"action": action, "status": resp.status_code},
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise KbUnreachable(
                f"ReMe HTTP 响应非 JSON: {action}",
                details={"action": action},
            ) from exc
        if not isinstance(data, dict):
            raise KbUnreachable(
                f"ReMe HTTP 响应形态非法: {action}",
                details={"action": action},
            )
        return data

    @staticmethod
    def _extract_entry(proposal: dict) -> tuple[str, str, str]:
        entry = proposal.get("entry") if isinstance(proposal.get("entry"), dict) else {}
        title = str(
            proposal.get("title") or entry.get("title") or entry.get("name") or ""
        ).strip()
        content = str(
            proposal.get("content")
            or entry.get("content")
            or entry.get("summary")
            or ""
        ).strip()
        bucket = str(
            proposal.get("bucket") or entry.get("bucket") or "business/wiki"
        ).strip()
        return title, content, bucket

    async def write_proposal(self, token: str, proposal: dict) -> WriteResult:
        del token  # 确认令牌由 L2 端点校验；远端无对应语义
        if not isinstance(proposal, dict):
            return WriteResult(
                ok=False, remote_ref=None, verified=False, error_code="invalid_payload"
            )
        # 路由写：允许 payload 携带目标 base_url（api/kb 注入）
        base = str(proposal.get("_base_url") or "").strip().rstrip("/")
        client = self._client
        owns = False
        if base and base != self.base_url:
            client = httpx.AsyncClient(base_url=base, timeout=_DEFAULT_TIMEOUT)
            owns = True
        try:
            title, content, bucket = self._extract_entry(proposal)
            if not title or not content:
                return WriteResult(
                    ok=False,
                    remote_ref=None,
                    verified=False,
                    error_code="missing_title_or_content",
                )
            payload = {
                "title": title,
                "content": content,
                "bucket": bucket or self._default_bucket,
            }
            for key in (
                "preconditions",
                "steps",
                "expected",
                "priority",
                "requirement_id",
                "links",
            ):
                src = proposal.get(key)
                if src is None and isinstance(proposal.get("entry"), dict):
                    src = proposal["entry"].get(key)
                if src is not None:
                    payload[key] = src

            async def _do_post(action: str, body: dict) -> dict:
                path = f"/{action.lstrip('/')}"
                try:
                    resp = await client.post(path, json=body)
                except httpx.HTTPError as exc:
                    raise KbUnreachable(
                        f"ReMe HTTP 不可达: {action}",
                        details={
                            "action": action,
                            "error": str(exc) or type(exc).__name__,
                        },
                    ) from exc
                if resp.status_code >= 400:
                    raise KbUnreachable(
                        f"ReMe HTTP 写入失败 {resp.status_code}: {action}",
                        details={"action": action, "status": resp.status_code},
                    )
                data = resp.json()
                if not isinstance(data, dict):
                    raise KbUnreachable(
                        f"ReMe HTTP 响应形态非法: {action}",
                        details={"action": action},
                    )
                return data

            data = await _do_post("save_to_knowledge", payload)
            if not data.get("success", True):
                return WriteResult(
                    ok=False,
                    remote_ref=None,
                    verified=False,
                    error_code="write_rejected",
                )
            written = (data.get("metadata") or {}).get("written") or []
            remote_ref = str(written[0]) if written else None
            verified = False
            if remote_ref:
                try:
                    check = await _do_post("read", {"path": remote_ref})
                    verified = bool(check.get("success", True)) and bool(
                        check.get("answer")
                    )
                except KbUnreachable:
                    verified = False
            return WriteResult(
                ok=True,
                remote_ref=remote_ref,
                verified=verified,
                error_code=None,
            )
        finally:
            if owns:
                await client.aclose()


class WorkspaceRoutingWriter:
    """按提案所属工作区的 kb_config.target 路由到 HttpReMeWriter。

    confirm 端点在调用前向 payload 注入 ``_workspace_id``；本类查库取
    ``mode=service`` 的 target。查不到 / 非 service → 502。
    """

    def __init__(self, db) -> None:
        self._db = db

    async def write_proposal(self, token: str, proposal: dict) -> WriteResult:
        from ..store.models import WorkspaceDAO

        if not isinstance(proposal, dict):
            return WriteResult(
                ok=False, remote_ref=None, verified=False, error_code="invalid_payload"
            )
        ws_id = str(proposal.get("_workspace_id") or "").strip()
        if not ws_id:
            raise KbUnreachable(
                "写入缺少工作区上下文",
                details={"reason": "missing_workspace_id"},
            )
        ws = await WorkspaceDAO(self._db).get(ws_id)
        cfg = ws.kb_config_obj()
        mode = str(cfg.get("mode") or "")
        target = str(cfg.get("target") or "").strip()
        if mode != "service" or not target:
            raise KbUnreachable(
                f"工作区知识库模式不可写: mode={mode!r}",
                details={"mode": mode, "workspace_id": ws_id},
            )
        options = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
        writer = HttpReMeWriter(base_url=target, options=options)
        try:
            return await writer.write_proposal(token, proposal)
        finally:
            await writer.aclose()


async def build_http_reader(kb_config: dict) -> HttpReMeReader:
    """Factory builder：仅接受 mode=service。"""
    if not isinstance(kb_config, dict):
        raise ValidationError("kb_config 必须为对象")
    mode = kb_config.get("mode")
    if mode != "service":
        raise ValidationError(
            f"HTTP 适配仅支持 mode=service，收到 {mode!r}",
            details={"mode": mode},
        )
    target = str(kb_config.get("target") or "").strip()
    kb_id = str(kb_config.get("kb_id") or "").strip()
    if not target or not kb_id:
        raise ValidationError(
            "kb_config 缺少 target/kb_id",
            details={"missing": [x for x in ("target", "kb_id") if not kb_config.get(x)]},
        )
    options = kb_config.get("options") if isinstance(kb_config.get("options"), dict) else {}
    reader = HttpReMeReader(base_url=target, kb_id=kb_id, options=options)
    # 轻量探活：list 知识目录（失败则抛，不落工厂缓存）
    await reader.list_index_tree()
    return reader


def register_service_builder(factory: ReMeReaderFactory) -> None:
    """将 HTTP builder 注册到工厂（main lifespan / 测试共用）。"""
    factory.register("service", build_http_reader)
