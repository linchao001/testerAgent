"""LLM 接入层（dd §9.3 / §17.2）：OpenAI 兼容客户端指向 DeepSeek。

韧性契约（dd §9.3 表 + tech-design §4.5 R20）：
- 超时：连接 10s、非流式读 120s、流式相邻 chunk 60s（均可配）；信号量排队不计超时。
- 重试：429（尊重 Retry-After）/ 5xx / 超时 / 连接错误 → 指数退避 1s/2s/4s ±25% 抖动，
  最多重试 3 次；其余 4xx 不重试。
- 限流：实例级 asyncio 信号量（runtime_config.llm_concurrency，默认 4）。
- JSON 模式：json_schema 非空时 response_format=json_object 且在末条 user 消息尾部贴 schema；
  响应先 json-repair 解析再做结构校验，失败携带错误文本重请 1 次，仍失败抛 LLMBadOutput。
- 流式：stream_writer 非空即走 stream；chunk 原文逐次回调 stream_writer（事件包装由节点负责）；
  usage 经 stream_options.include_usage 从流尾 chunk 取（DeepSeek 支持）。
- 异常翻译（dd §17.2）：openai 异常 → LLM* 系列，重试耗尽后按最后错误类型抛出；
  asyncio.CancelledError 不重试、不上卷，原样传播（批次边界由 Runner 控制）。

偏离说明：tech-design D7 写"langchain-openai 兼容客户端"，实现改用 openai 官方 AsyncOpenAI
直连 OpenAI 兼容协议（langchain-openai 底层同库）。理由：dd §17.2 的翻译示例即 openai 原生
异常，且 langchain 包装层的内置重试会与 §9.3 的显式退避表冲突。openai 已是直接依赖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Awaitable, Callable, Protocol, TypedDict, runtime_checkable

import httpx
import json_repair
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel, Field

from ..errors import (
    LLMBadOutput,
    LLMBadRequest,
    LLMTimeoutError,
    LLMUpstreamError,
    RateLimitedError,
)

logger = logging.getLogger(__name__)


class Msg(TypedDict):
    """dd §9.3 的消息契约（role 取 system/user/assistant 等，透传给上游）。"""

    role: str
    content: str


class LLMResult(BaseModel):
    """dd §9.3 冻结字段。"""

    content: str
    usage: dict[str, int] = Field(default_factory=dict)  # {prompt_tokens, completion_tokens, total_tokens}
    model: str
    finish_reason: str
    retries: int
    latency_ms: int


@runtime_checkable
class LLMClient(Protocol):
    """dd §9.3 冻结 Protocol。stream_writer 非空即流式（LangGraph stream_writer 透传）。"""

    async def chat(
        self,
        messages: list[Msg],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        timeout: float | None = None,
        stream_writer: Callable[[str], None] | None = None,
    ) -> LLMResult: ...


# JSON 校验失败后重请时追加的用户消息（dd §9.3 "携带 ValidationError.errors() 文本重请 1 次"）
_REPAIR_PROMPT = (
    "你上一次输出的 JSON 未通过校验：{error}\n"
    "请仅输出一个符合给定 JSON Schema 的 JSON 对象，不要输出任何解释或代码块标记。"
)

_SCHEMA_HINT = (
    "\n\n请严格按以下 JSON Schema 输出，仅输出一个 JSON 对象，不要输出任何额外内容：\n"
    "```json\n{schema}\n```"
)


def _usage_dict(usage: Any) -> dict[str, int]:
    """openai CompletionUsage → dd 约定三字段 dict；缺字段/None 时尽量保留。"""
    if usage is None:
        return {}
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        val = getattr(usage, key, None)
        if val is not None:
            out[key] = int(val)
    return out


def _try_parse_json(content: str, schema: dict) -> tuple[str | None, str | None]:
    """json-repair 解析 + 结构校验。

    返回 (规范化 JSON 文本, None) 或 (None, 错误描述)。结构校验覆盖：
    结果必须是 JSON 对象；schema 顶层 required 列出的键必须存在（类型级校验由
    调用方节点把 content 解析进各自 Pydantic 模型完成，不在本层职责内）。
    """
    try:
        obj = json_repair.loads(content)
    except Exception as e:  # json_repair 对极端输入可能抛错
        return None, f"JSON 解析失败: {e}"
    if not isinstance(obj, dict):
        return None, f"输出不是 JSON 对象（解析结果为 {type(obj).__name__}）"
    required = schema.get("required")
    if isinstance(required, list):
        missing = [k for k in required if isinstance(k, str) and k not in obj]
        if missing:
            return None, f"缺少必需字段: {missing}"
    return json.dumps(obj, ensure_ascii=False), None


class OpenAICompatLLMClient:
    """OpenAI 兼容协议客户端（默认指向 DeepSeek）。实现 dd §9.3 LLMClient Protocol。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        top_p: float = 1.0,
        timeout: float = 120.0,
        connect_timeout: float = 10.0,
        stream_chunk_timeout: float = 60.0,
        concurrency: int = 4,
        max_retries: int = 3,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not base_url or not api_key or not model:
            raise LLMBadRequest(
                "LLM 客户端配置不完整：base_url/api_key/model 均不可为空",
                details={"has_base_url": bool(base_url), "has_api_key": bool(api_key), "model": model},
            )
        self._model = model
        self._temperature = temperature
        self._top_p = top_p
        self._read_timeout = float(timeout)
        self._connect_timeout = float(connect_timeout)
        self._stream_chunk_timeout = float(stream_chunk_timeout)
        self._max_retries = max_retries
        self._semaphore = asyncio.Semaphore(concurrency)
        self._sleep = sleep
        # 禁用 SDK 内置重试：退避/Retry-After 由本类按 §9.3 表显式控制
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,
            http_client=http_client,
        )

    @classmethod
    def from_configs(
        cls,
        model_config: dict,
        runtime_config: dict,
        **overrides: Any,
    ) -> OpenAICompatLLMClient:
        """按 config 表两行 JSON 构造（dd §2.9 ModelConfigIn / §13.2 runtime_config）。

        读取超时优先取 model_config.timeout（引导页可配），缺省回落
        runtime_config.llm_timeout_read_sec，再缺省 120s。
        """
        read_timeout = model_config.get("timeout") or runtime_config.get("llm_timeout_read_sec", 120)
        kwargs: dict[str, Any] = {
            "base_url": model_config.get("base_url", ""),
            "api_key": model_config.get("api_key", ""),
            "model": model_config.get("model", ""),
            "temperature": model_config.get("temperature", 0.2),
            "top_p": model_config.get("top_p", 1.0),
            "timeout": read_timeout,
            "connect_timeout": runtime_config.get("llm_timeout_connect_sec", 10),
            "concurrency": runtime_config.get("llm_concurrency", 4),
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    async def aclose(self) -> None:
        """关闭底层连接（lifespan 关停序列调用，dd §6.5）。"""
        await self._client.close()

    # ---- 对外主入口 ----

    async def chat(
        self,
        messages: list[Msg],
        *,
        model: str | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        timeout: float | None = None,
        stream_writer: Callable[[str], None] | None = None,
    ) -> LLMResult:
        req_messages = self._build_messages(messages, json_schema)
        use_model = model or self._model
        read_timeout = float(timeout) if timeout is not None else self._read_timeout
        use_temp = self._temperature if temperature is None else temperature
        json_mode = json_schema is not None

        # 信号量排队不计超时（dd §9.3）：超时只作用于 acquire 之后的实际请求
        async with self._semaphore:
            t0 = time.monotonic()
            retries = 0
            content, usage, resp_model, finish_reason, used = await self._call_with_retries(
                req_messages,
                model=use_model,
                temperature=use_temp,
                read_timeout=read_timeout,
                json_mode=json_mode,
                stream_writer=stream_writer,
            )
            retries += used

            if json_mode:
                repaired, err = _try_parse_json(content, json_schema)  # type: ignore[arg-type]
                if repaired is None:
                    retries += 1  # JSON 重请也计入 retries（额外请求次数）
                    content, usage, resp_model, finish_reason, used = await self._call_with_retries(
                        req_messages + [{"role": "user", "content": _REPAIR_PROMPT.format(error=err)}],
                        model=use_model,
                        temperature=use_temp,
                        read_timeout=read_timeout,
                        json_mode=json_mode,
                        stream_writer=stream_writer,
                    )
                    retries += used
                    repaired, err = _try_parse_json(content, json_schema)  # type: ignore[arg-type]
                    if repaired is None:
                        raise LLMBadOutput(
                            f"LLM 输出未通过 JSON 校验（重请 1 次后仍失败）: {err}",
                            details={"error": err},
                        )
                content = repaired

            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.debug(
                "llm.chat done",
                extra={"model": resp_model, "retries": retries, "latency_ms": latency_ms,
                       "stream": stream_writer is not None, "json_mode": json_mode},
            )
            return LLMResult(
                content=content,
                usage=usage,
                model=resp_model,
                finish_reason=finish_reason,
                retries=retries,
                latency_ms=latency_ms,
            )

    # ---- 内部：请求与重试 ----

    def _build_messages(self, messages: list[Msg], json_schema: dict | None) -> list[dict[str, str]]:
        """JSON 模式时在末条 user 消息尾部贴 schema（dd §9.3）；无 user 消息则追加一条。"""
        out = [dict(m) for m in messages]
        if json_schema is None:
            return out
        hint = _SCHEMA_HINT.format(schema=json.dumps(json_schema, ensure_ascii=False))
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("role") == "user":
                out[i]["content"] = out[i].get("content", "") + hint
                return out
        out.append({"role": "user", "content": hint.lstrip()})
        return out

    def _request_kwargs(
        self,
        *,
        model: str,
        temperature: float,
        read_timeout: float,
        json_mode: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "top_p": self._top_p,
            "timeout": httpx.Timeout(
                read=read_timeout,
                connect=self._connect_timeout,
                write=read_timeout,
                pool=read_timeout,
            ),
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    async def _call_with_retries(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        temperature: float,
        read_timeout: float,
        json_mode: bool,
        stream_writer: Callable[[str], None] | None,
    ) -> tuple[str, dict[str, int], str, str, int]:
        """返回 (content, usage, model, finish_reason, 传输重试次数)。重试耗尽抛 LLM* 系列。"""
        kwargs = self._request_kwargs(
            model=model, temperature=temperature, read_timeout=read_timeout, json_mode=json_mode
        )
        retries = 0
        last: tuple[str, Exception, float | None] | None = None
        for attempt in range(self._max_retries + 1):
            try:
                if stream_writer is None:
                    return (*await self._non_stream(messages, kwargs), retries)
                return (*await self._stream(messages, kwargs, stream_writer), retries)
            except asyncio.CancelledError:
                raise  # 取消不重试不包装（dd §9.3"不重试的取消"）
            except (APITimeoutError, APIConnectionError, APIStatusError, asyncio.TimeoutError) as e:
                kind, retry_after = self._classify(e)
                if kind == "bad_request":
                    raise LLMBadRequest(str(e)) from e
                last = (kind, e, retry_after)
                if attempt >= self._max_retries:
                    break
                retries += 1
                delay = retry_after if retry_after is not None else self._backoff(attempt)
                logger.debug(
                    "llm.chat retry",
                    extra={"kind": kind, "attempt": attempt + 1, "delay": delay, "model": model},
                )
                await self._sleep(delay)
        assert last is not None
        kind, e, retry_after = last
        raise self._to_app_error(kind, e, retry_after)

    async def _non_stream(
        self, messages: list[dict[str, str]], kwargs: dict[str, Any]
    ) -> tuple[str, dict[str, int], str, str]:
        resp = await self._client.chat.completions.create(messages=messages, stream=False, **kwargs)  # type: ignore[arg-type]
        choice = resp.choices[0] if resp.choices else None
        return (
            (choice.message.content if choice and choice.message and choice.message.content else "") or "",
            _usage_dict(getattr(resp, "usage", None)),
            resp.model or kwargs["model"],
            (choice.finish_reason if choice else "") or "",
        )

    async def _stream(
        self,
        messages: list[dict[str, str]],
        kwargs: dict[str, Any],
        stream_writer: Callable[[str], None],
    ) -> tuple[str, dict[str, int], str, str]:
        stream = await self._client.chat.completions.create(
            messages=messages,  # type: ignore[arg-type]
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
        parts: list[str] = []
        usage: dict[str, int] = {}
        model = kwargs["model"]
        finish_reason = ""
        aiter = stream.__aiter__()
        try:
            while True:
                try:
                    # 相邻 chunk 间隔超时（dd §9.3）；取消在等待点即时生效
                    chunk = await asyncio.wait_for(aiter.__anext__(), timeout=self._stream_chunk_timeout)
                except StopAsyncIteration:
                    break
                if getattr(chunk, "usage", None) is not None:
                    usage = _usage_dict(chunk.usage)
                if getattr(chunk, "model", None):
                    model = chunk.model
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta.content if choice.delta else None
                if delta:
                    parts.append(delta)
                    stream_writer(delta)
        finally:
            # 取消/超时路径下显式关闭流，避免底层 httpx 响应悬挂
            await stream.close()
        return "".join(parts), usage, model, finish_reason

    # ---- 内部：错误分类与翻译（dd §17.2）----

    @staticmethod
    def _classify(exc: Exception) -> tuple[str, float | None]:
        """→ (kind, retry_after)。kind ∈ timeout/rate_limit/upstream/bad_request。"""
        if isinstance(exc, (asyncio.TimeoutError, APITimeoutError)):
            return "timeout", None
        if isinstance(exc, RateLimitError):
            return "rate_limit", _parse_retry_after(exc)
        if isinstance(exc, APIStatusError):
            if exc.status_code >= 500:
                return "upstream", None
            return "bad_request", None
        # APIConnectionError（非超时）：DNS/连接重置等网络故障
        return "upstream", None

    def _backoff(self, attempt: int) -> float:
        """指数退避 1s/2s/4s ±25% 抖动（dd §9.3）。"""
        return (2.0**attempt) * random.uniform(0.75, 1.25)

    @staticmethod
    def _to_app_error(kind: str, exc: Exception, retry_after: float | None) -> Exception:
        if kind == "timeout":
            return LLMTimeoutError(f"LLM 请求超时（重试耗尽）: {exc}")
        if kind == "rate_limit":
            return RateLimitedError(
                f"LLM 触发限流（重试耗尽）: {exc}", details={"retry_after": retry_after}
            )
        return LLMUpstreamError(f"LLM 上游错误（重试耗尽）: {exc}")


def _parse_retry_after(exc: RateLimitError) -> float | None:
    """尊重 Retry-After 响应头（dd §9.3）；缺失/非数值时返回 None 走退避。"""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None
