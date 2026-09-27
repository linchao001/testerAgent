"""WP-07 LLMClient 韧性测试（dd §9.3 / §17.2）。

fake server：httpx.MockTransport 注入 AsyncOpenAI，覆盖验收口径五场景
（429 / 5xx / 超时 / 4xx / 坏 JSON），另补流式 chunk 超时、并发信号量、取消传播等。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import httpx
import pytest

from tester_agent.adapters.llm import (
    LLMClient,
    LLMResult,
    OpenAICompatLLMClient,
)
from tester_agent.errors import (
    LLMBadOutput,
    LLMBadRequest,
    LLMTimeoutError,
    LLMUpstreamError,
    RateLimitedError,
)

BASE_URL = "https://api.deepseek.test/v1"


# ---- fake server 工具 ----


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def ok_body(content: str = "你好", model: str = "deepseek-chat", usage: dict | None = None) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def err_resp(status: int, msg: str = "boom", headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"message": msg, "type": "test_error", "code": "test"}},
        headers=headers,
    )


def sse_chunk(content: str | None = None, finish: str | None = None, model: str = "deepseek-chat") -> dict:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def sse_usage_chunk(model: str = "deepseek-chat") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def sse_body(chunks: list[dict]) -> bytes:
    parts = [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks]
    parts.append("data: [DONE]")
    return ("\n\n".join(parts) + "\n\n").encode()


def sse_resp(chunks: list[dict]) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=sse_body(chunks)
    )


class FakeServer:
    """按脚本依次返回响应/抛异常；记录所有请求体。"""

    def __init__(self) -> None:
        self.script: list[Callable[[], httpx.Response] | Exception] = []
        self.requests: list[dict] = []
        self.raw_requests: list[httpx.Request] = []

    def push(self, resp: httpx.Response) -> None:
        self.script.append(lambda: resp)

    def push_exc(self, exc: Exception) -> None:
        self.script.append(exc)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.raw_requests.append(request)
        try:
            self.requests.append(json.loads(request.content))
        except Exception:
            self.requests.append({})
        item = self.script.pop(0) if self.script else (lambda: ok_body_resp())
        if isinstance(item, Exception):
            raise item
        return item()


def ok_body_resp(content: str = "你好") -> httpx.Response:
    return httpx.Response(200, json=ok_body(content))


def make_client(
    server: FakeServer,
    sleep: SleepRecorder | None = None,
    **overrides: Any,
) -> OpenAICompatLLMClient:
    kwargs: dict[str, Any] = {
        "base_url": BASE_URL,
        "api_key": "test-key",
        "model": "deepseek-chat",
        "http_client": httpx.AsyncClient(transport=httpx.MockTransport(server.handler)),
        "sleep": sleep or SleepRecorder(),
    }
    kwargs.update(overrides)
    return OpenAICompatLLMClient(**kwargs)


MSGS = [{"role": "user", "content": "写个用例"}]
SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}


# ---- 基础成功路径 ----


async def test_non_stream_success() -> None:
    server = FakeServer()
    client = make_client(server)
    result = await client.chat(MSGS)
    assert isinstance(result, LLMResult)
    assert result.content == "你好"
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert result.model == "deepseek-chat"
    assert result.finish_reason == "stop"
    assert result.retries == 0
    assert result.latency_ms >= 0
    # 默认参数透传，无 response_format
    req = server.requests[0]
    assert req["model"] == "deepseek-chat"
    assert req["temperature"] == 0.2
    assert req["top_p"] == 1.0
    assert "response_format" not in req
    await client.aclose()


async def test_chat_param_overrides() -> None:
    server = FakeServer()
    client = make_client(server)
    await client.chat(MSGS, model="deepseek-reasoner", temperature=0.7, timeout=5)
    req = server.requests[0]
    assert req["model"] == "deepseek-reasoner"
    assert req["temperature"] == 0.7
    await client.aclose()


# ---- 场景①：429（尊重 Retry-After）----


async def test_429_retry_after_then_success() -> None:
    server = FakeServer()
    server.push(err_resp(429, "rate limited", headers={"retry-after": "3"}))
    server.push(ok_body_resp())
    sleep = SleepRecorder()
    client = make_client(server, sleep=sleep)
    result = await client.chat(MSGS)
    assert result.content == "你好"
    assert result.retries == 1
    assert sleep.delays == [3.0]  # 尊重 Retry-After，不走退避
    await client.aclose()


async def test_429_exhausted_raises_rate_limited() -> None:
    server = FakeServer()
    for _ in range(4):
        server.push(err_resp(429, "rate limited", headers={"retry-after": "7"}))
    sleep = SleepRecorder()
    client = make_client(server, sleep=sleep)
    with pytest.raises(RateLimitedError) as ei:
        await client.chat(MSGS)
    assert ei.value.code == "RATE_LIMITED"
    assert ei.value.http_status == 429
    assert ei.value.retryable is True
    assert ei.value.details["retry_after"] == 7.0
    assert len(server.requests) == 4  # 1 + 3 次重试
    assert sleep.delays == [7.0, 7.0, 7.0]
    await client.aclose()


async def test_429_without_retry_after_uses_backoff() -> None:
    server = FakeServer()
    server.push(err_resp(429, "rate limited"))
    server.push(ok_body_resp())
    sleep = SleepRecorder()
    client = make_client(server, sleep=sleep)
    await client.chat(MSGS)
    assert len(sleep.delays) == 1
    assert 0.75 <= sleep.delays[0] <= 1.25  # 1s ±25% 抖动
    await client.aclose()


# ---- 场景②：5xx ----


async def test_5xx_retry_then_success() -> None:
    server = FakeServer()
    server.push(err_resp(500))
    server.push(err_resp(502))
    server.push(ok_body_resp())
    sleep = SleepRecorder()
    client = make_client(server, sleep=sleep)
    result = await client.chat(MSGS)
    assert result.retries == 2
    assert 0.75 <= sleep.delays[0] <= 1.25
    assert 1.5 <= sleep.delays[1] <= 2.5  # 2s ±25%
    await client.aclose()


async def test_5xx_exhausted_raises_upstream() -> None:
    server = FakeServer()
    for _ in range(4):
        server.push(err_resp(503, "service unavailable"))
    client = make_client(server)
    with pytest.raises(LLMUpstreamError) as ei:
        await client.chat(MSGS)
    assert ei.value.code == "LLM_UPSTREAM"
    assert ei.value.http_status == 502
    assert ei.value.retryable is True
    assert len(server.requests) == 4
    await client.aclose()


async def test_connect_error_exhausted_raises_upstream() -> None:
    server = FakeServer()
    for _ in range(4):
        server.push_exc(httpx.ConnectError("connection refused"))
    client = make_client(server)
    with pytest.raises(LLMUpstreamError):
        await client.chat(MSGS)
    assert len(server.requests) == 4
    await client.aclose()


# ---- 场景③：超时 ----


async def test_read_timeout_retry_then_success() -> None:
    server = FakeServer()
    server.push_exc(httpx.ReadTimeout("read timed out"))
    server.push(ok_body_resp())
    client = make_client(server)
    result = await client.chat(MSGS)
    assert result.retries == 1
    await client.aclose()


async def test_timeout_exhausted_raises_llm_timeout() -> None:
    server = FakeServer()
    for _ in range(4):
        server.push_exc(httpx.ReadTimeout("read timed out"))
    client = make_client(server)
    with pytest.raises(LLMTimeoutError) as ei:
        await client.chat(MSGS)
    assert ei.value.code == "LLM_TIMEOUT"
    assert ei.value.http_status == 504
    assert ei.value.retryable is True
    await client.aclose()


# ---- 场景④：4xx 不重试 ----


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_4xx_no_retry_raises_bad_request(status: int) -> None:
    server = FakeServer()
    server.push(err_resp(status, "bad request"))
    sleep = SleepRecorder()
    client = make_client(server, sleep=sleep)
    with pytest.raises(LLMBadRequest) as ei:
        await client.chat(MSGS)
    assert ei.value.code == "LLM_BAD_REQUEST"
    assert ei.value.retryable is False
    assert len(server.requests) == 1  # 不重试
    assert sleep.delays == []
    await client.aclose()


# ---- 场景⑤：坏 JSON（json-repair + 校验重请）----


async def test_json_mode_repairs_trailing_comma_locally() -> None:
    """json-repair 本地修复成功：无额外请求，content 为规范化 JSON。"""
    server = FakeServer()
    server.push(ok_body_resp('{"title": "登录",}'))
    client = make_client(server)
    result = await client.chat(MSGS, json_schema=SCHEMA)
    assert result.retries == 0
    assert json.loads(result.content) == {"title": "登录"}
    # schema 已贴到末条 user 消息尾部，且声明了 json_object
    req = server.requests[0]
    assert req["response_format"] == {"type": "json_object"}
    assert '"required": ["title"]' in req["messages"][-1]["content"]
    assert req["messages"][-1]["role"] == "user"
    await client.aclose()


async def test_bad_json_triggers_one_repair_request() -> None:
    """首答完全不是 JSON → 携带错误文本重请 1 次，第二答合法即成功。"""
    server = FakeServer()
    server.push(ok_body_resp("完全不是 JSON"))
    server.push(ok_body_resp('{"title": "登录"}'))
    client = make_client(server)
    result = await client.chat(MSGS, json_schema=SCHEMA)
    assert result.retries == 1
    assert json.loads(result.content) == {"title": "登录"}
    assert len(server.requests) == 2
    repair_msg = server.requests[1]["messages"][-1]
    assert repair_msg["role"] == "user"
    assert "未通过校验" in repair_msg["content"]
    await client.aclose()


async def test_bad_json_twice_raises_llm_bad_output() -> None:
    server = FakeServer()
    server.push(ok_body_resp("完全不是 JSON"))
    server.push(ok_body_resp("依然不是"))
    client = make_client(server)
    with pytest.raises(LLMBadOutput) as ei:
        await client.chat(MSGS, json_schema=SCHEMA)
    assert ei.value.code == "LLM_BAD_OUTPUT"
    assert ei.value.retryable is True
    assert len(server.requests) == 2  # 只重请 1 次
    await client.aclose()


async def test_json_missing_required_key_triggers_repair() -> None:
    server = FakeServer()
    server.push(ok_body_resp('{"other": 1}'))
    server.push(ok_body_resp('{"title": "登录"}'))
    client = make_client(server)
    result = await client.chat(MSGS, json_schema=SCHEMA)
    assert json.loads(result.content) == {"title": "登录"}
    repair_msg = server.requests[1]["messages"][-1]["content"]
    assert "缺少必需字段" in repair_msg
    assert "title" in repair_msg
    await client.aclose()


async def test_json_mode_without_user_message_appends_one() -> None:
    server = FakeServer()
    server.push(ok_body_resp('{"title": "x"}'))
    client = make_client(server)
    await client.chat([{"role": "system", "content": "你是助手"}], json_schema=SCHEMA)
    req = server.requests[0]
    assert req["messages"][-1]["role"] == "user"
    assert "JSON Schema" in req["messages"][-1]["content"]
    await client.aclose()


async def test_json_mode_stream_repair_also_applies() -> None:
    """流式 + JSON：首答坏 JSON 走重请，重请仍可流式。"""
    server = FakeServer()
    server.push(sse_resp([sse_chunk("坏"), sse_chunk("掉", finish="stop"), sse_usage_chunk()]))
    server.push(sse_resp([sse_chunk('{"title": "x"}', finish="stop"), sse_usage_chunk()]))
    client = make_client(server)
    chunks: list[str] = []
    result = await client.chat(MSGS, json_schema=SCHEMA, stream_writer=chunks.append)
    assert json.loads(result.content) == {"title": "x"}
    assert chunks == ["坏", "掉", '{"title": "x"}']  # 两轮 chunk 均透传
    assert result.retries == 1
    await client.aclose()


# ---- 流式 ----


async def test_stream_collects_chunks_and_tail_usage() -> None:
    server = FakeServer()
    server.push(
        sse_resp(
            [
                sse_chunk("你"),
                sse_chunk("好"),
                sse_chunk(finish="stop"),
                sse_usage_chunk(),
            ]
        )
    )
    client = make_client(server)
    chunks: list[str] = []
    result = await client.chat(MSGS, stream_writer=chunks.append)
    assert result.content == "你好"
    assert chunks == ["你", "好"]  # stream_writer 逐 chunk 原文回调
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert result.finish_reason == "stop"
    assert result.model == "deepseek-chat"
    assert result.retries == 0
    # 流式必带 include_usage，否则流尾取不到 usage
    assert server.requests[0]["stream_options"] == {"include_usage": True}
    await client.aclose()


async def test_stream_without_tail_usage_returns_empty_dict() -> None:
    server = FakeServer()
    server.push(sse_resp([sse_chunk("你好", finish="stop")]))
    client = make_client(server)
    result = await client.chat(MSGS, stream_writer=lambda c: None)
    assert result.content == "你好"
    assert result.usage == {}
    await client.aclose()


class _SlowStream(httpx.AsyncByteStream):
    """逐段吐 SSE 字节并可控延迟的假流。"""

    def __init__(self, parts: list[bytes], delay: float) -> None:
        self._parts = parts
        self._delay = delay

    async def __aiter__(self):
        for p in self._parts:
            await asyncio.sleep(self._delay)
            yield p

    async def aclose(self) -> None:
        pass


def _slow_sse_resp(lines: list[str], delay: float) -> httpx.Response:
    parts = [(line + "\n\n").encode() for line in lines]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_SlowStream(parts, delay),
    )


async def test_stream_chunk_timeout_raises_llm_timeout() -> None:
    """相邻 chunk 间隔超过 stream_chunk_timeout → 超时且按重试策略耗尽。"""
    server = FakeServer()
    line = f"data: {json.dumps(sse_chunk('你'))}"
    for _ in range(4):
        server.push(_slow_sse_resp([line], delay=0.2))
    client = make_client(server, stream_chunk_timeout=0.05)
    with pytest.raises(LLMTimeoutError):
        await client.chat(MSGS, stream_writer=lambda c: None)
    assert len(server.requests) == 4
    await client.aclose()


async def test_stream_cancellation_propagates_without_retry() -> None:
    """取消在 chunk 等待点即时生效：不包装、不重试（dd §9.3"不重试的取消"）。"""
    server = FakeServer()
    line = f"data: {json.dumps(sse_chunk('你'))}"
    server.push(_slow_sse_resp([line], delay=5.0))
    sleep = SleepRecorder()
    client = make_client(server, sleep=sleep, stream_chunk_timeout=60.0)
    task = asyncio.create_task(client.chat(MSGS, stream_writer=lambda c: None))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sleep.delays == []  # 未触发任何重试
    assert len(server.requests) == 1
    await client.aclose()


# ---- 并发信号量 ----


async def test_concurrency_semaphore_limits_inflight() -> None:
    inflight = 0
    max_inflight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.05)
        inflight -= 1
        return ok_body_resp()

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = make_client(FakeServer(), concurrency=2, http_client=http_client)
    results = await asyncio.gather(*[client.chat(MSGS) for _ in range(5)])
    assert all(r.content == "你好" for r in results)
    assert max_inflight <= 2  # runtime_config.llm_concurrency 默认语义
    await client.aclose()


# ---- 构造与配置 ----


async def test_from_configs_mapping() -> None:
    client = OpenAICompatLLMClient.from_configs(
        {"base_url": BASE_URL, "api_key": "k", "model": "m", "temperature": 0.5, "timeout": 66},
        {"llm_concurrency": 7, "llm_timeout_connect_sec": 3},
        sleep=SleepRecorder(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: ok_body_resp())),
    )
    assert client._model == "m"
    assert client._temperature == 0.5
    assert client._read_timeout == 66.0  # model_config.timeout 优先
    assert client._connect_timeout == 3.0
    assert client._semaphore._value == 7
    await client.aclose()


async def test_from_configs_falls_back_to_runtime_read_timeout() -> None:
    client = OpenAICompatLLMClient.from_configs(
        {"base_url": BASE_URL, "api_key": "k", "model": "m"},
        {"llm_timeout_read_sec": 99},
        sleep=SleepRecorder(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: ok_body_resp())),
    )
    assert client._read_timeout == 99.0
    await client.aclose()


@pytest.mark.parametrize(
    "cfg", [{"base_url": "", "api_key": "k", "model": "m"}, {"base_url": BASE_URL, "api_key": "", "model": "m"}, {"base_url": BASE_URL, "api_key": "k", "model": ""}]
)
async def test_incomplete_config_raises_bad_request(cfg: dict) -> None:
    with pytest.raises(LLMBadRequest):
        OpenAICompatLLMClient(**cfg, sleep=SleepRecorder())  # type: ignore[arg-type]


async def test_protocol_shape() -> None:
    """结构型 Protocol 自检：实现类满足 dd §9.3 chat 签名。"""
    client = make_client(FakeServer())
    assert isinstance(client, LLMClient)
    await client.aclose()
