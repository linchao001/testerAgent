"""结构化日志（dd §15.4）。

JSON 行输出到 stdout；每次 LLM/检索调用记 task_id/run_id/node/batch_id/duration/retry/degraded。
红线：日志不含 api_key 与需求全文正文（只记 hash 与长度）——由 RedactFilter 兜底过滤敏感键。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# 不允许出现在日志 extra 中的键（兜底防泄漏；业务侧仍应自觉只记 hash/长度）
_REDACT_KEYS = frozenset({"api_key", "authorization", "token", "password", "secret", "requirement_text"})


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(getattr(record, "__dict__", {}).keys()):
            if key.lower() in _REDACT_KEYS:
                setattr(record, key, "***REDACTED***")
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # stdlib 保留字段不作为业务字段输出
        std = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "taskName", "message",
        }
        for key, value in record.__dict__.items():
            if key not in std and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """初始化根 logger：JSON 行 → stdout。幂等，可重复调用。"""
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactFilter())
    root.handlers.clear()
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
