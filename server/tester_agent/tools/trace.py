"""Tool call audit helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, TypedDict


class ToolTraceEntry(TypedDict, total=False):
    tool: str
    ok: bool
    latency_ms: int
    error_code: str
    args_digest: str


def args_digest(args: dict[str, Any]) -> str:
    payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
