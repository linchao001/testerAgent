"""WP-06 验收测试：异常体系 HTTP 映射、错误信封、trace_id 中间件。

依据：dd §17（类层次/边界/trace_id）、§14（错误码目录）、§10.1（错误信封）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent import errors
from tester_agent.api.error_handling import TRACE_ID_HEADER, install_error_handling
from tester_agent.errors import (
    AppError,
    ArtifactRevisionError,
    FileConflict,
    KbTokenInvalid,
    KbUnreachable,
    LLMBadOutput,
    LLMBadRequest,
    LLMTimeoutError,
    LLMUpstreamError,
    NotFoundError,
    PathEscapeError,
    ProposalExpired,
    RateLimitedError,
    ReviewTransitionError,
    TaskBusyError,
    TaskCancelled,
    TaskStateConflict,
    ValidationError,
    VersionConflict,
    current_trace_id,
)
from tester_agent.main import create_app
from tester_agent.settings import Settings

_TRACE_RE = re.compile(r"^[0-9a-f]{16}$")

# dd §17.1 冻结类属性表（code, http_status, retryable）
APP_ERROR_TABLE = [
    (ValidationError, "VALIDATION_BODY", 400, False),
    (ArtifactRevisionError, "VALIDATION_ARTIFACT_REVISION", 422, False),
    (ReviewTransitionError, "VALIDATION_REVIEW_TRANSITION", 422, False),
    (NotFoundError, "NOT_FOUND", 404, False),
    (TaskStateConflict, "TASK_STATE_CONFLICT", 409, False),
    (VersionConflict, "VERSION_CONFLICT", 409, False),
    (FileConflict, "FILE_CONFLICT", 409, False),
    (KbTokenInvalid, "KB_TOKEN_INVALID", 400, False),
    (ProposalExpired, "PROPOSAL_EXPIRED", 409, False),
    (TaskBusyError, "TASK_BUSY", 409, True),
    (LLMUpstreamError, "LLM_UPSTREAM", 502, True),
    (LLMTimeoutError, "LLM_TIMEOUT", 504, True),
    (RateLimitedError, "RATE_LIMITED", 429, True),
    (LLMBadRequest, "LLM_BAD_REQUEST", 400, False),
    (LLMBadOutput, "LLM_BAD_OUTPUT", 502, True),
    (KbUnreachable, "KB_UNREACHABLE", 502, True),
]


# ---------- 类层次与冻结属性（dd §17.1 §14） ----------


class TestErrorHierarchy:
    @pytest.mark.parametrize(
        "cls,code,http_status,retryable", APP_ERROR_TABLE
    )
    def test_class_attributes(self, cls, code, http_status, retryable):
        assert cls.code == code
        assert cls.http_status == http_status
        assert cls.retryable is retryable
        assert issubclass(cls, AppError)

    @pytest.mark.parametrize("cls", [TaskCancelled, PathEscapeError])
    def test_internal_control_flow_not_app_error(self, cls):
        assert issubclass(cls, Exception)
        assert not issubclass(cls, AppError)

    def test_app_error_defaults_and_details(self):
        err = AppError("msg")
        assert err.message == "msg"
        assert err.details == {}

        err2 = NotFoundError("缺资源", details={"id": "x"})
        assert err2.details == {"id": "x"}
        assert str(err2) == "缺资源"

    def test_all_catalog_codes_present(self):
        # §14 中映射 HTTP 的每个 code 都必须有对应类（事件/标记类除外）
        codes = {cls.code for cls, *_ in APP_ERROR_TABLE} | {"INTERNAL"}
        for code in (
            "VALIDATION_BODY", "VALIDATION_ARTIFACT_REVISION",
            "VALIDATION_REVIEW_TRANSITION", "NOT_FOUND", "TASK_STATE_CONFLICT",
            "VERSION_CONFLICT", "TASK_BUSY", "LLM_UPSTREAM", "LLM_TIMEOUT",
            "RATE_LIMITED", "LLM_BAD_REQUEST", "LLM_BAD_OUTPUT",
            "KB_UNREACHABLE", "KB_TOKEN_INVALID", "PROPOSAL_EXPIRED",
            "FILE_CONFLICT", "INTERNAL",
        ):
            assert code in codes


# ---------- 测试用应用 ----------


_RAISE_REGISTRY = {cls.__name__: cls for cls, *_ in APP_ERROR_TABLE}


class BodyIn(BaseModel):
    """模块级定义：测试模块启用了 future annotations，FastAPI 需从模块全局
    解析注解字符串（定义在工厂函数内部会被误判为 query 参数）。"""

    name: str


def _build_app() -> FastAPI:
    app = FastAPI()
    install_error_handling(app)

    @app.get("/boom/{error_name}")
    async def boom(error_name: str):
        raise _RAISE_REGISTRY[error_name]("炸了", details={"k": "v"})

    @app.post("/validate")
    async def validate(body: BodyIn):
        return {"ok": True}

    @app.get("/trace")
    async def trace():
        return {"trace_id": current_trace_id()}

    @app.get("/failure")
    async def failure():
        raise RuntimeError("意外故障")

    return app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(_build_app())


# ---------- AppError → §10.1 错误信封的 HTTP 映射 ----------


class TestErrorEnvelopeMapping:
    @pytest.mark.parametrize(
        "cls,code,http_status,retryable", APP_ERROR_TABLE
    )
    def test_each_app_error_maps_to_envelope(
        self, client, cls, code, http_status, retryable
    ):
        resp = client.get(f"/boom/{cls.__name__}")
        assert resp.status_code == http_status
        assert resp.headers[TRACE_ID_HEADER]
        body = resp.json()
        err = body["error"]
        assert err["code"] == code
        assert err["message"] == "炸了"
        assert err["retryable"] is retryable
        assert err["details"]["k"] == "v"

    @pytest.mark.parametrize("cls", [LLMUpstreamError, LLMTimeoutError,
                                    LLMBadOutput, KbUnreachable])
    def test_5xx_envelope_carries_trace_id(self, client, cls):
        resp = client.get(f"/boom/{cls.__name__}")
        assert resp.status_code >= 500
        tid = resp.json()["error"]["details"]["trace_id"]
        assert _TRACE_RE.match(tid)
        assert tid == resp.headers[TRACE_ID_HEADER]

    @pytest.mark.parametrize("cls", [ValidationError, NotFoundError,
                                    VersionConflict, RateLimitedError])
    def test_4xx_envelope_has_no_trace_id_in_details(self, client, cls):
        # dd §17.3：5xx 必须返回 trace_id，4xx 可选——本实现 4xx 不放进信封
        resp = client.get(f"/boom/{cls.__name__}")
        assert resp.status_code < 500
        assert "trace_id" not in resp.json()["error"]["details"]


# ---------- Pydantic 入参错误 → VALIDATION_BODY 400 ----------


class TestRequestValidation:
    def test_missing_field_is_validation_body(self, client):
        resp = client.post("/validate", json={})
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert err["code"] == "VALIDATION_BODY"
        assert err["retryable"] is False
        errors_list = err["details"]["errors"]
        assert isinstance(errors_list, list) and errors_list
        assert errors_list[0]["loc"][-1] == "name"

    def test_malformed_json_is_validation_body(self, client):
        resp = client.post(
            "/validate",
            content=b"{not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_BODY"


# ---------- 兜底异常 → INTERNAL 500 ----------


class TestUnhandledException:
    def test_unexpected_error_is_internal_500(self):
        # ServerErrorMiddleware 送达兜底响应后会重抛异常（Starlette 固定行为，
        # 真实 uvicorn 正常返回响应）；TestClient 关闭重抛以验收信封。
        client = TestClient(_build_app(), raise_server_exceptions=False)
        resp = client.get("/failure")
        assert resp.status_code == 500
        err = resp.json()["error"]
        assert err["code"] == "INTERNAL"
        assert err["retryable"] is False
        assert _TRACE_RE.match(err["details"]["trace_id"])


# ---------- trace_id 中间件（dd §17.3） ----------


class TestTraceIdMiddleware:
    def test_trace_id_visible_inside_request_and_returned(self, client):
        resp = client.get("/trace")
        tid = resp.json()["trace_id"]
        assert _TRACE_RE.match(tid)
        assert resp.headers[TRACE_ID_HEADER] == tid

    def test_trace_id_unique_per_request(self, client):
        t1 = client.get("/trace").headers[TRACE_ID_HEADER]
        t2 = client.get("/trace").headers[TRACE_ID_HEADER]
        assert t1 != t2

    def test_contextvar_reset_after_request(self, client):
        assert current_trace_id() == ""
        client.get("/trace")
        assert current_trace_id() == ""

    def test_install_is_idempotent(self):
        app = FastAPI()
        install_error_handling(app)
        n = len(app.user_middleware)
        install_error_handling(app)
        assert len(app.user_middleware) == n
        assert app.state.error_handling_installed is True


# ---------- PathEscapeError 收拢到 errors.py ----------


class TestPathEscapeConsolidation:
    def test_path_escape_defined_once_and_reexported(self):
        from tester_agent.store import workspace_files as wf

        assert wf.PathEscapeError is errors.PathEscapeError
        assert wf.PathEscapeError is PathEscapeError
        assert "PathEscapeError" in wf.__all__


# ---------- 组合根集成 ----------


def _settings(tmp_path: Path) -> Settings:
    return Settings.from_env({
        "TESTER_AGENT_DATA_DIR": str(tmp_path / "data"),
        "TESTER_AGENT_SINGLE_WORKER": "1",
    })


class TestAppIntegration:
    def test_healthz_carries_trace_id_header(self, tmp_path):
        with TestClient(create_app(_settings(tmp_path))) as client:
            resp = client.get("/healthz")
        assert resp.status_code == 200
        assert _TRACE_RE.match(resp.headers[TRACE_ID_HEADER])
