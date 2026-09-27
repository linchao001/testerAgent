"""trace_id 中间件与 FastAPI 错误信封 handlers（dd §17.2 §17.3 §10.1）。

- TraceIdMiddleware：每请求生成 uuid 短码 → contextvar + request.state，
  响应头回带 X-Trace-Id，请求结束复位 contextvar。
- AppError handler：HTTP 状态/ code/retryable 一律取异常类属性（dd §17.1）。
- RequestValidationError：Pydantic 入参错误 → VALIDATION_BODY 400。
- 兜底 Exception：未预期异常 → INTERNAL 500，details.trace_id 必填并服务端留日志。

信封（dd §10.1）：
    {"error": {"code", "message", "retryable", "details"}}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..errors import (
    AppError,
    ValidationError,
    current_trace_id,
    new_trace_id,
    trace_id_var,
)
from ..logging_config import get_logger

logger = get_logger(__name__)

TRACE_ID_HEADER = "X-Trace-Id"


def error_envelope(err: AppError, *, trace_id: str = "") -> dict[str, Any]:
    """构造 §10.1 错误信封；5xx 必须在 details 带 trace_id（dd §17.3）。"""
    details: dict[str, Any] = dict(err.details)
    if err.http_status >= 500:
        details["trace_id"] = trace_id
    return {
        "error": {
            "code": err.code,
            "message": err.message,
            "retryable": err.retryable,
            "details": details,
        }
    }


class TraceIdMiddleware(BaseHTTPMiddleware):
    """请求级 trace_id：进生成、出回带响应头、结束复位（dd §17.3）。"""

    async def dispatch(self, request: Request, call_next):
        tid = new_trace_id()
        request.state.trace_id = tid
        token = trace_id_var.set(tid)
        try:
            response = await call_next(request)
        finally:
            trace_id_var.reset(token)
        response.headers[TRACE_ID_HEADER] = tid
        return response


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    tid = current_trace_id() or getattr(request.state, "trace_id", "")
    if exc.http_status >= 500:
        # 依赖类 5xx（LLM_UPSTREAM/TIMEOUT 等）：可预期失败，warning 留痕便于对账
        logger.warning(
            "app_error",
            extra={"trace_id": tid, "code": exc.code},
        )
    return JSONResponse(
        status_code=exc.http_status,
        content=error_envelope(exc, trace_id=tid),
    )


async def request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    tid = current_trace_id() or getattr(request.state, "trace_id", "")
    err = ValidationError(
        "请求参数校验失败",
        details={"errors": jsonable_encoder(exc.errors())},
    )
    return JSONResponse(
        status_code=err.http_status,
        content=error_envelope(err, trace_id=tid),
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    # 本 handler 运行于 ServerErrorMiddleware（在 TraceIdMiddleware 外层），
    # contextvar 已复位，trace_id 从 request.state 取回（中间件入口已写入）。
    tid = getattr(request.state, "trace_id", "") or new_trace_id()
    logger.error(
        "unhandled_exception",
        extra={"trace_id": tid},
        exc_info=exc,
    )
    err = AppError("服务内部错误", details={"trace_id": tid})
    return JSONResponse(status_code=500, content=error_envelope(err, trace_id=tid))


def install_error_handling(app: FastAPI) -> None:
    """接线 trace_id 中间件与错误信封 handlers（幂等：重复安装不重复挂载）。"""
    if getattr(app.state, "error_handling_installed", False):
        return
    app.add_middleware(TraceIdMiddleware)
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.state.error_handling_installed = True
