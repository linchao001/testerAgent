"""WP-01 验收测试：healthz / 单 worker 守卫 / settings / 结构化日志 / cli。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent import __version__
from tester_agent.logging_config import JsonFormatter, RedactFilter, setup_logging
from tester_agent.main import create_app
from tester_agent.settings import Settings, validate_single_worker


def _make_settings(tmp_path: Path, **env: str) -> Settings:
    base = {"TESTER_AGENT_DATA_DIR": str(tmp_path / "data"), "TESTER_AGENT_SINGLE_WORKER": "1"}
    return Settings.from_env({**base, **env})


# ---------- settings（dd §13.1） ----------


class TestSettings:
    def test_defaults(self):
        s = Settings.from_env({})
        assert s.host == "127.0.0.1"
        assert s.port == 8080
        assert s.log_level == "INFO"
        assert s.single_worker is True
        assert s.data_dir.name == "data"

    def test_overrides(self, tmp_path):
        s = _make_settings(
            tmp_path,
            TESTER_AGENT_PORT="9000",
            TESTER_AGENT_HOST="0.0.0.0",
            TESTER_AGENT_LOG_LEVEL="debug",  # 应归一为大写
        )
        assert s.port == 9000
        assert s.host == "0.0.0.0"
        assert s.log_level == "DEBUG"
        assert s.data_dir == (tmp_path / "data").resolve()

    def test_db_paths(self, tmp_path):
        s = _make_settings(tmp_path)
        assert s.app_db_path == s.data_dir / "app.db"
        assert s.checkpoints_db_path == s.data_dir / "checkpoints.db"
        assert s.workspaces_dir == s.data_dir / "workspaces"


# ---------- 单 worker 守卫（dd §13.1） ----------


class TestSingleWorkerGuard:
    @pytest.mark.parametrize("value", ["0", "2", "true", ""])
    def test_non_1_rejected(self, value):
        s = Settings.from_env({"TESTER_AGENT_SINGLE_WORKER": value})
        assert s.single_worker is False
        with pytest.raises(RuntimeError, match="TESTER_AGENT_SINGLE_WORKER"):
            validate_single_worker(s)

    def test_whitespace_tolerated(self):
        # "1 " 视为合法取值 1（env 解析宽容空白）
        assert Settings.from_env({"TESTER_AGENT_SINGLE_WORKER": "1 "}).single_worker is True

    def test_app_construction_rejected(self, tmp_path):
        s = _make_settings(tmp_path, TESTER_AGENT_SINGLE_WORKER="4")
        with pytest.raises(RuntimeError):
            create_app(s)

    def test_app_construction_ok(self, tmp_path):
        create_app(_make_settings(tmp_path))  # 不抛即通过


# ---------- /healthz ----------


class TestHealthz:
    def test_healthz_200(self, tmp_path):
        client = TestClient(create_app(_make_settings(tmp_path)))
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["version"] == __version__


# ---------- 结构化日志（dd §15.4） ----------


def _record_with(extra: dict) -> logging.LogRecord:
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    for k, v in extra.items():
        setattr(record, k, v)
    return record


class TestStructuredLogging:
    def test_json_line_shape(self):
        out = JsonFormatter().format(_record_with({"task_id": "t1", "duration_ms": 12}))
        import json

        payload = json.loads(out)
        assert payload["message"] == "hello world"
        assert payload["level"] == "INFO"
        assert "ts" in payload and "T" in payload["ts"]
        assert payload["task_id"] == "t1"
        assert payload["duration_ms"] == 12

    def test_redact_filter_drops_sensitive_keys(self):
        record = _record_with({"api_key": "sk-secret", "token": "abc", "task_id": "t1"})
        assert RedactFilter().filter(record) is True
        assert record.api_key == "***REDACTED***"
        assert record.token == "***REDACTED***"
        assert record.task_id == "t1"  # 业务字段不受影响

    def test_setup_logging_idempotent(self):
        setup_logging("INFO")
        setup_logging("INFO")
        assert len(logging.getLogger().handlers) == 1


# ---------- cli 框架 ----------


class TestCli:
    def test_check_ok(self, tmp_path, capsys):
        from tester_agent.cli import main

        env = {
            "TESTER_AGENT_DATA_DIR": str(tmp_path / "data"),
            "TESTER_AGENT_SINGLE_WORKER": "1",
        }
        import os

        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            assert main(["check"]) == 0
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        assert '"ok": true' in capsys.readouterr().out

    def test_reap_still_placeholder(self, capsys):
        from tester_agent.cli import main

        # backup 已由 WP-X2 接线；reap 仍为排障占位
        assert main(["reap"]) == 2
        assert "WP-23" in capsys.readouterr().err
