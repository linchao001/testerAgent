# -*- coding: utf-8 -*-
"""Embedded ReMe SDK adapters (ReMeReader / ReMeWriter via WorkspaceMemoryPool)."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import PurePosixPath
from typing import Any

import yaml

from ..domain import EntryType
from ..errors import KbUnreachable, NotFoundError, ValidationError
from ..memory.pool import WorkspaceMemoryPool
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

SP1_CAPS = ReMeCaps(
    metadata_filter=False,
    entry_version=False,
    passage_api=True,
)

_CHAIN_PREFIX = "chain:"

_BUCKET_TYPE: dict[str, EntryType] = {
    "business/wiki": EntryType.BUSINESS,
    "business/procedure": EntryType.BUSINESS,
    "business/personal": EntryType.BUSINESS,
    "business/openapi": EntryType.API,
    "business/dbinfo": EntryType.DB,
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
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if len(parts) >= 3 and parts[0] in {"knowledge", "Knowledge"}:
        return f"{parts[1]}/{parts[2]}"
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def _title_from_path(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).stem


def _parse_chain_signals(signals: Any) -> list[str]:
    if signals is None:
        items: list = []
    elif isinstance(signals, str):
        try:
            parsed = yaml.safe_load(signals)
            items = [str(x) for x in parsed] if isinstance(parsed, list) else [signals]
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


def _response_dict(resp: Any) -> dict[str, Any]:
    if resp is None:
        return {}
    if isinstance(resp, dict):
        return resp
    success = bool(getattr(resp, "success", True))
    answer = getattr(resp, "answer", None)
    metadata = getattr(resp, "metadata", None)
    return {
        "success": success,
        "answer": answer if answer is not None else "",
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


class SdkReMeReader:
    """ReMeReader over embedded ReMe jobs via ReMeMemoryManager."""

    def __init__(self, manager: Any, *, kb_id: str = "") -> None:
        self.caps = SP1_CAPS
        self._manager = manager
        self.kb_id = kb_id or str(
            (getattr(manager, "kb_config", {}) or {}).get("kb_id") or ""
        )
        opts = (getattr(manager, "kb_config", {}) or {}).get("options") or {}
        self.knowledge_dir = str(
            opts.get("knowledge_dir")
            or (getattr(manager, "kb_config", {}) or {}).get("knowledge_dir")
            or "knowledge"
        )
        self._default_bucket = str(opts.get("bucket") or "all")

    async def _job(self, name: str, **kwargs: Any) -> dict[str, Any]:
        if not getattr(self._manager, "is_started", False):
            raise KbUnreachable(
                "ReMe 未启动",
                details={
                    "action": name,
                    "workspace_id": getattr(self._manager, "workspace_id", None),
                },
            )
        resp = await self._manager.run_job(name, raise_on_error=False, **kwargs)
        if resp is None:
            raise KbUnreachable(
                f"ReMe job 失败或未启动: {name}",
                details={"action": name},
            )
        return _response_dict(resp)

    async def search(
        self,
        query: str,
        *,
        top_k: int,
        types: list[str] | None = None,
        scope: dict | None = None,
    ) -> list[Entry]:
        del types, scope
        data = await self._job(
            "knowledge_search",
            query=query,
            limit=top_k,
            bucket=self._default_bucket,
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
                    entry_version="",
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
        data = await self._job("read", path=entry_id)
        if not data.get("success", True):
            raise NotFoundError(
                str(data.get("answer") or f"entry not found: {entry_id}"),
                details={"entry_id": entry_id},
            )
        text = str(data.get("answer") or "")
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
        data = await self._job(
            "list",
            path=self.knowledge_dir,
            recursive=True,
            limit=10_000,
        )
        if not data.get("success", True):
            logger.warning("list_index_tree list failed: %s", data.get("answer"))
            return IndexTree(links=[])
        items = (data.get("metadata") or {}).get("items") or []
        paths = [str(p).replace("\\", "/") for p in items if str(p).endswith(".md")]

        async def _fm(path: str) -> tuple[str, dict]:
            try:
                resp = await self._job("frontmatter_read", path=path)
            except KbUnreachable:
                return path, {}
            if not resp.get("success", True):
                return path, {}
            meta = (resp.get("metadata") or {}).get("frontmatter") or {}
            return path, meta if isinstance(meta, dict) else {}

        sem = asyncio.Semaphore(16)

        async def _bounded(path: str) -> tuple[str, dict]:
            async with sem:
                return await _fm(path)

        pairs = await asyncio.gather(*[_bounded(p) for p in paths])
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


class SdkReMeWriter:
    """ReMeWriter via embedded ``save_to_knowledge`` (token checked by L2)."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        opts = (getattr(manager, "kb_config", {}) or {}).get("options") or {}
        self._default_bucket = str(opts.get("bucket") or "business/wiki")

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
        del token
        if not isinstance(proposal, dict):
            return WriteResult(
                ok=False, remote_ref=None, verified=False, error_code="invalid_payload"
            )
        title, content, bucket = self._extract_entry(proposal)
        if not title or not content:
            return WriteResult(
                ok=False,
                remote_ref=None,
                verified=False,
                error_code="missing_title_or_content",
            )
        payload: dict[str, Any] = {
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

        if not getattr(self._manager, "is_started", False):
            raise KbUnreachable(
                "ReMe 未启动，无法写入",
                details={"reason": "reme_not_started"},
            )
        resp = await self._manager.run_job(
            "save_to_knowledge", raise_on_error=False, **payload
        )
        data = _response_dict(resp)
        if resp is None or not data.get("success", True):
            return WriteResult(
                ok=False,
                remote_ref=None,
                verified=False,
                error_code="save_rejected",
            )
        remote_ref = str(
            (data.get("metadata") or {}).get("path")
            or (data.get("metadata") or {}).get("entry_id")
            or title
        )
        verified = False
        try:
            read = await self._manager.run_job(
                "read", raise_on_error=False, path=remote_ref
            )
            verified = read is not None and bool(getattr(read, "success", True))
        except Exception:
            verified = False
        return WriteResult(
            ok=True, remote_ref=remote_ref, verified=verified, error_code=None
        )


class PoolRoutingWriter:
    """Confirm-path writer: resolve workspace → pool manager → SdkReMeWriter."""

    def __init__(self, db, pool: WorkspaceMemoryPool) -> None:
        self._db = db
        self._pool = pool

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
        mgr = await self._pool.get_or_start(ws_id, cfg)
        if not mgr.is_started:
            raise KbUnreachable(
                "ReMe 嵌入实例未能启动",
                details={"workspace_id": ws_id},
            )
        return await SdkReMeWriter(mgr).write_proposal(token, proposal)


def register_sdk_builder(
    factory: ReMeReaderFactory, pool: WorkspaceMemoryPool
) -> None:
    """Register embedded-sdk builder on the factory (mode key ``sdk``)."""

    async def build(kb_config: dict) -> SdkReMeReader:
        if not isinstance(kb_config, dict):
            raise ValidationError("kb_config 必须为对象")
        ws_id = str(kb_config.get("_workspace_id") or "").strip()
        if not ws_id:
            raise ValidationError(
                "sdk builder 需要 _workspace_id",
                details={"missing": ["_workspace_id"]},
            )
        clean = {k: v for k, v in kb_config.items() if not str(k).startswith("_")}
        mgr = await pool.get_or_start(ws_id, clean)
        if not mgr.is_started:
            raise KbUnreachable(
                "ReMe 嵌入实例未能启动",
                details={"workspace_id": ws_id},
            )
        return SdkReMeReader(mgr, kb_id=str(clean.get("kb_id") or ""))

    factory.register("sdk", build)
