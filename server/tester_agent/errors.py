"""可预期异常体系（dd §17.1 唯一来源）与 trace_id contextvar 基础设施（dd §17.3）。

- WP-03 提前落地 AppError/NotFoundError/VersionConflict；WP-05 追加 FileConflict；
  WP-06 补齐 §17.1 全部类，并把 PathEscapeError 从 store 收拢到本文件（类唯一定义点）。
- trace_id contextvar 同时服务 HTTP 请求中间件（本 WP）与 Runner run 级 trace_id（WP-22）。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any


class AppError(Exception):
    """所有可预期异常的基类（dd §17.1）。"""

    code: str = "INTERNAL"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str = "", *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details: dict[Any, Any] = details or {}


# ---- 客户端类（不重试）----


class ValidationError(AppError):
    code, http_status = "VALIDATION_BODY", 400


class ArtifactRevisionError(AppError):
    code, http_status = "VALIDATION_ARTIFACT_REVISION", 422


class ReviewTransitionError(AppError):
    code, http_status = "VALIDATION_REVIEW_TRANSITION", 422


class NotFoundError(AppError):
    code, http_status = "NOT_FOUND", 404


class TaskStateConflict(AppError):
    code, http_status = "TASK_STATE_CONFLICT", 409


class VersionConflict(AppError):
    code, http_status = "VERSION_CONFLICT", 409


class FileConflict(AppError):
    """手工编辑用例时 expected_hash 与盘上现状不一致（dd §17.1 → 409）。"""

    code, http_status = "FILE_CONFLICT", 409


class KbTokenInvalid(AppError):
    code, http_status = "KB_TOKEN_INVALID", 400


class ProposalExpired(AppError):
    code, http_status = "PROPOSAL_EXPIRED", 409


# ---- 可重试/依赖类 ----


class TaskBusyError(AppError):
    code, http_status, retryable = "TASK_BUSY", 409, True


class LLMUpstreamError(AppError):
    code, http_status, retryable = "LLM_UPSTREAM", 502, True


class LLMTimeoutError(AppError):
    code, http_status, retryable = "LLM_TIMEOUT", 504, True


class RateLimitedError(AppError):
    code, http_status, retryable = "RATE_LIMITED", 429, True


class LLMBadRequest(AppError):
    code, http_status = "LLM_BAD_REQUEST", 400


class LLMBadOutput(AppError):
    code, http_status, retryable = "LLM_BAD_OUTPUT", 502, True


class KbUnreachable(AppError):
    code, http_status, retryable = "KB_UNREACHABLE", 502, True


# ---- 内部控制流（不映射 HTTP，不出 Runner 边界）----


class TaskCancelled(Exception):
    """批次边界捕获 → aborted（dd §17.1）。"""


# GraphInterrupt / interrupt() 直接使用 langgraph.types 原生类型，不另包装。


class PathEscapeError(Exception):
    """编程错误：路径越界。wrap() 兜底按 INTERNAL 处理（dd §17.1）。"""


# ---- trace_id（dd §17.3）----

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    """生成 uuid 短码（HTTP 请求 / 每次 Runner._run 各一个）。"""
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str:
    """读取当前上下文 trace_id；未设置时返回空串。"""
    return trace_id_var.get()
