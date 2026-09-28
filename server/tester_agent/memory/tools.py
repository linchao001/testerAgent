# -*- coding: utf-8 -*-
"""LangChain tools for personal / KB memory search (no KB write)."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class MemorySearchArgs(BaseModel):
    query: str = Field(description="semantic search query")
    max_results: int = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.0)
    scope: str = Field(
        default="",
        description="knowledge | agent | all (empty = default by KB mount)",
    )
    bucket: str = Field(default="all")


def _resolve_scope(manager: Any, scope: str) -> str:
    raw = (scope or "").strip().lower()
    if raw in {"knowledge", "agent", "all"}:
        return raw
    return "knowledge" if manager.kb_enabled() else "agent"


def make_memory_search_tool(manager: Any) -> StructuredTool:
    async def _memory_search(
        query: str,
        max_results: int = 5,
        min_score: float = 0.0,
        scope: str = "",
        bucket: str = "all",
    ) -> str:
        q = (query or "").strip()
        if not q:
            return "Error: query cannot be empty"
        if not getattr(manager, "is_started", False):
            return "ReMe is not started."
        resolved = _resolve_scope(manager, scope)
        limit = max(1, int(max_results))
        kwargs = {
            "query": q,
            "limit": limit,
            "min_score": max(0.0, float(min_score)),
        }
        if resolved == "agent":
            resp = await manager.run_job("search", **kwargs)
        elif resolved == "knowledge":
            resp = await manager.run_job(
                "knowledge_search",
                bucket=(bucket or "all").strip() or "all",
                **kwargs,
            )
        else:
            k_resp, a_resp = await _gather_dual(manager, kwargs, bucket)
            return _format_dual(k_resp, a_resp)
        return _format_response(resp)

    return StructuredTool.from_function(
        coroutine=_memory_search,
        name="memory_search",
        description=(
            "Search personal memory and optional shared knowledge base. "
            "Use when prior decisions, preferences, or KB facts are needed."
        ),
        args_schema=MemorySearchArgs,
    )


async def _gather_dual(manager: Any, kwargs: dict, bucket: str):
    import asyncio

    return await asyncio.gather(
        manager.run_job(
            "knowledge_search",
            bucket=(bucket or "all").strip() or "all",
            **kwargs,
        ),
        manager.run_job("search", **kwargs),
    )


def _format_response(resp: Any) -> str:
    if resp is None:
        return "ReMe is not started."
    success = bool(getattr(resp, "success", True))
    answer = getattr(resp, "answer", None)
    metadata = getattr(resp, "metadata", None) or {}
    results = metadata.get("results") if isinstance(metadata, dict) else None
    payload = {
        "success": success,
        "answer": answer if answer is not None else "",
        "results": results or [],
    }
    return json.dumps(payload, ensure_ascii=False)


def _format_dual(k_resp: Any, a_resp: Any) -> str:
    return json.dumps(
        {
            "success": bool(
                (k_resp and getattr(k_resp, "success", True))
                or (a_resp and getattr(a_resp, "success", True))
            ),
            "knowledge": _format_response(k_resp),
            "agent": _format_response(a_resp),
        },
        ensure_ascii=False,
    )
