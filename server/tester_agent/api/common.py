"""API-A 共享助手（WP-25）：分页游标校验与 Page 信封序列化。

- 游标合法性在 API 边界前置校验（dd §3.3："非法游标由 API 层翻译为 400"），
  DAO 侧 decode_cursor 抛 ValueError，这里转成 ValidationError(VALIDATION_BODY)；
- Page 信封（dd §10.1）：{"items": [...], "next_cursor": str|null}，
  行对象经 mapper 转响应模型后交由 FastAPI 序列化。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Query

from ..errors import ValidationError
from ..store.models import Page, decode_cursor

DEFAULT_LIMIT = 50  # tech-design §5.0：默认 50、最大 200


def checked_cursor(cursor: str | None) -> str | None:
    """非法游标 → 400 VALIDATION_BODY（dd §3.3 API 层翻译约定）。"""
    if cursor is None:
        return None
    try:
        decode_cursor(cursor)
    except ValueError as exc:
        raise ValidationError("非法分页游标", details={"cursor": cursor}) from exc
    return cursor


def page_response(page: Page, mapper: Callable[[Any], Any]) -> dict:
    """Page[Row] → §10.1 分页信封（items 经 mapper 映射为响应模型）。"""
    return {
        "items": [mapper(item) for item in page.items],
        "next_cursor": page.next_cursor,
    }


def pagination_params(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=200),
    cursor: str | None = Query(None),
) -> tuple[int, str | None]:
    """统一 ?limit=&cursor= 解析（tech-design §5.0），返回 (limit, cursor)。"""
    return limit, checked_cursor(cursor)
