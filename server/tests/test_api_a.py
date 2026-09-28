"""WP-25 API-A 验收测试：workspaces（CRUD/软删/kb test）、agents、
conversations/messages、model config/test（tech-design §5.1 §5.2 §5.6）。

验收口径（WBS）：
- 跨工作区 404 不暴露存在性（父资源缺失与子资源缺失同形态 404 NOT_FOUND）；
- 探活错误码：kb test / model test 失败按 dd §17.1 类属性映射 HTTP 与 code。

夹具：TestClient 走完整 lifespan（与 test_reaper 同模式）；播种经独立
Database 连接（WAL 多连接）+ asyncio.run。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.adapters.reme import ReMeCaps
from tester_agent.errors import KbUnreachable, LLMTimeoutError
from tester_agent.main import create_app
from tester_agent.settings import Settings
from tester_agent.store.db import Database
from tester_agent.store.models import (
    TaskDAO,
    TaskRow,
    WorkspaceDAO,
    WorkspaceRow,
)
from tests.fakes import FakeLLM, FakeReMeReader

WS = "ws-1"
CONV = "conv-1"
TASK = "task-1"
KB_CONFIG = {
    "kb_id": "kb-1",
    "knowledge_bases_dir": "",
    "knowledge_dir": "knowledge",
    "create_knowledge_base": False,
    "options": {},
}


def _settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        {
            "TESTER_AGENT_DATA_DIR": str(tmp_path / "data"),
            "TESTER_AGENT_SINGLE_WORKER": "1",
        }
    )


@pytest.fixture()
def env(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        db = Database(settings.app_db_path)
        yield client, db, settings
        db.close()


def _run(coro):
    return asyncio.run(coro)


def _seed_workspace(db, ws_id=WS, **kwargs):
    async def _go():
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=ws_id, name="工作区一", kb_config=dict(KB_CONFIG), **kwargs)
        )

    _run(_go())


def _seed_task(db, status="running", task_id=TASK, conv_id=CONV, ws_id=WS):
    async def _go():
        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=ws_id, name="ws", kb_config=dict(KB_CONFIG))
        )
        await db.aexecute(
            "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (conv_id, ws_id, "c", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
        )
        await TaskDAO(db).create(
            TaskRow.create(
                id=task_id,
                conversation_id=conv_id,
                workspace_id=ws_id,
                status=status,
                current_stage="intake",
                langgraph_thread_id=f"th-{task_id}",
                graph_run_id=f"run-{task_id}",
            )
        )

    _run(_go())


# ---------- workspaces CRUD / 软删 ----------


class TestWorkspacesCrud:
    def test_create_201_with_location_and_roundtrip(self, env):
        client, db, _ = env
        resp = client.post(
            "/api/v1/workspaces",
            json={"name": "电商", "description": "d", "kb_config": KB_CONFIG},
        )
        assert resp.status_code == 201
        assert resp.headers["location"].startswith("/api/v1/workspaces/")
        body = resp.json()
        assert body["kb_config"] == KB_CONFIG
        got = client.get(f"/api/v1/workspaces/{body['id']}")
        assert got.status_code == 200
        assert got.json()["name"] == "电商"

    def test_create_validation(self, env):
        client, _, _ = env
        bad_name = client.post(
            "/api/v1/workspaces", json={"name": "", "kb_config": KB_CONFIG}
        )
        assert bad_name.status_code == 400
        assert bad_name.json()["error"]["code"] == "VALIDATION_BODY"
        bad_mode = client.post(
            "/api/v1/workspaces",
            json={"name": "x", "kb_config": {**KB_CONFIG, "mode": "service"}},
        )
        assert bad_mode.status_code == 400

    def test_update_partial(self, env):
        client, _, _ = env
        wid = client.post(
            "/api/v1/workspaces", json={"name": "a", "kb_config": KB_CONFIG}
        ).json()["id"]
        resp = client.put(f"/api/v1/workspaces/{wid}", json={"name": "b"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "b"
        assert body["description"] == ""  # 未提及字段不变
        assert body["kb_config"] == KB_CONFIG

    def test_list_pagination_and_cursor(self, env):
        client, _, _ = env
        for i in range(3):
            client.post("/api/v1/workspaces", json={"name": f"w{i}", "kb_config": KB_CONFIG})
        page1 = client.get("/api/v1/workspaces?limit=2").json()
        assert len(page1["items"]) == 2
        assert page1["next_cursor"]
        page2 = client.get(
            f"/api/v1/workspaces?limit=2&cursor={page1['next_cursor']}"
        ).json()
        assert len(page2["items"]) == 1
        assert page2["next_cursor"] is None
        ids = {w["name"] for w in page1["items"] + page2["items"]}
        assert ids == {"w0", "w1", "w2"}  # 无重复无遗漏

    def test_invalid_cursor_400(self, env):
        client, _, _ = env
        resp = client.get("/api/v1/workspaces?cursor=%2A%2A%2A")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_BODY"

    def test_soft_delete_then_404_same_shape_as_missing(self, env):
        client, db, _ = env
        wid = client.post(
            "/api/v1/workspaces", json={"name": "gone", "kb_config": KB_CONFIG}
        ).json()["id"]
        assert client.delete(f"/api/v1/workspaces/{wid}").json() == {"ok": True}
        deleted = client.get(f"/api/v1/workspaces/{wid}")
        missing = client.get("/api/v1/workspaces/no-such")
        assert deleted.status_code == missing.status_code == 404
        # 不暴露存在性：code/retryable/details 同形态（message 仅回显请求 id）
        for key in ("code", "retryable", "details"):
            assert deleted.json()["error"][key] == missing.json()["error"][key]
        # 列表不再出现
        names = [w["name"] for w in client.get("/api/v1/workspaces").json()["items"]]
        assert "gone" not in names

    def test_delete_with_active_task_409(self, env):
        client, db, _ = env
        _seed_task(db, status="running", ws_id=WS, conv_id=CONV, task_id=TASK)
        resp = client.delete(f"/api/v1/workspaces/{WS}")
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "TASK_STATE_CONFLICT"
        # 终态后可删
        _run(TaskDAO(db).update_status(TASK, status="completed"))
        assert client.delete(f"/api/v1/workspaces/{WS}").json() == {"ok": True}

    @pytest.mark.parametrize("status", ["waiting_confirm", "waiting_input", "cancelling"])
    def test_delete_blocked_by_each_active_status(self, env, status):
        client, db, _ = env
        _seed_task(db, status=status, ws_id=WS, conv_id=CONV, task_id=TASK)
        assert client.delete(f"/api/v1/workspaces/{WS}").status_code == 409


# ---------- kb/test 探活（错误码口径） ----------


class TestKbTest:
    def _register_ok_builder(self, client, caps=None):
        reader = FakeReMeReader(caps=caps)

        async def builder(kb_config):
            return reader

        client.app.state.reme_factory.register("sdk", builder)
        return reader

    def test_probe_ok_reports_caps_and_latency(self, env):
        client, db, _ = env
        _seed_workspace(db)
        self._register_ok_builder(
            client,
            caps=ReMeCaps(metadata_filter=True, entry_version=False, passage_api=True),
        )
        resp = client.post(f"/api/v1/workspaces/{WS}/kb/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["error_code"] is None
        assert body["latency_ms"] >= 0
        assert body["capabilities"] == {
            "metadata_filter": True,
            "entry_version": False,  # 能力缺失=降级标记，仍 200
            "passage_api": True,
        }

    def test_probe_unreachable_maps_kb_unreachable(self, env):
        client, db, _ = env
        _seed_workspace(db)

        async def builder(kb_config):
            raise KbUnreachable("连接失败")

        client.app.state.reme_factory.register("sdk", builder)
        resp = client.post(f"/api/v1/workspaces/{WS}/kb/test")
        assert resp.status_code == 502
        err = resp.json()["error"]
        assert err["code"] == "KB_UNREACHABLE"
        assert err["retryable"] is True

    def test_probe_unregistered_sdk_builder_400(self, env):
        client, db, _ = env
        _seed_workspace(db)
        client.app.state.reme_factory._builders.clear()
        resp = client.post(f"/api/v1/workspaces/{WS}/kb/test")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_BODY"

    def test_probe_missing_workspace_404(self, env):
        client, _, _ = env
        resp = client.post("/api/v1/workspaces/no-such/kb/test")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"


# ---------- agents ----------


class TestAgents:
    def test_agent_crud(self, env):
        client, _, _ = env
        created = client.post(
            "/api/v1/agents",
            json={"name": "用例智能体", "config": {"k": "v"}},
        )
        assert created.status_code == 201
        aid = created.json()["id"]
        assert created.json()["builtin"] is False
        got = client.get(f"/api/v1/agents/{aid}").json()
        assert got["config"] == {"k": "v"}
        updated = client.put(f"/api/v1/agents/{aid}", json={"config": {"k": "v2"}})
        assert updated.json()["config"] == {"k": "v2"}
        assert updated.json()["name"] == "用例智能体"
        assert client.delete(f"/api/v1/agents/{aid}").json() == {"ok": True}
        assert client.get(f"/api/v1/agents/{aid}").status_code == 404

    def test_bind_and_list_for_workspace(self, env):
        client, db, _ = env
        _seed_workspace(db)
        aid = client.post("/api/v1/agents", json={"name": "a1"}).json()["id"]
        bound = client.post(
            f"/api/v1/workspaces/{WS}/agents", json={"agent_id": aid}
        )
        assert bound.status_code == 200
        assert bound.json()["id"] == aid
        # 绑定幂等
        client.post(f"/api/v1/workspaces/{WS}/agents", json={"agent_id": aid})
        items = client.get(f"/api/v1/workspaces/{WS}/agents").json()["items"]
        assert [a["id"] for a in items] == [aid]

    def test_bind_404_does_not_expose_existence(self, env):
        client, db, _ = env
        _seed_workspace(db)
        aid = client.post("/api/v1/agents", json={"name": "a1"}).json()["id"]
        # 工作区缺失（智能体存在）与智能体缺失（工作区存在）同形态
        missing_ws = client.post(
            "/api/v1/workspaces/no-such/agents", json={"agent_id": aid}
        )
        missing_agent = client.post(
            f"/api/v1/workspaces/{WS}/agents", json={"agent_id": "no-such"}
        )
        assert missing_ws.status_code == missing_agent.status_code == 404
        assert missing_ws.json()["error"]["code"] == "NOT_FOUND"
        assert missing_agent.json()["error"]["code"] == "NOT_FOUND"
        # 软删后的工作区同等待遇
        wid = client.post(
            "/api/v1/workspaces", json={"name": "w", "kb_config": KB_CONFIG}
        ).json()["id"]
        client.delete(f"/api/v1/workspaces/{wid}")
        assert (
            client.post(
                f"/api/v1/workspaces/{wid}/agents", json={"agent_id": aid}
            ).status_code
            == 404
        )

    def test_list_workspace_agents_missing_ws_404(self, env):
        client, _, _ = env
        assert (
            client.get("/api/v1/workspaces/no-such/agents").status_code == 404
        )

    def test_delete_agent_cascades_binding(self, env):
        client, db, _ = env
        _seed_workspace(db)
        aid = client.post("/api/v1/agents", json={"name": "a1"}).json()["id"]
        client.post(f"/api/v1/workspaces/{WS}/agents", json={"agent_id": aid})
        client.delete(f"/api/v1/agents/{aid}")
        items = client.get(f"/api/v1/workspaces/{WS}/agents").json()["items"]
        assert items == []


# ---------- conversations / messages ----------


class TestConversations:
    def test_create_and_list_by_workspace(self, env):
        client, db, _ = env
        _seed_workspace(db)
        created = client.post(
            "/api/v1/conversations", json={"workspace_id": WS, "title": "会话一"}
        )
        assert created.status_code == 201
        cid = created.json()["id"]
        items = client.get(f"/api/v1/workspaces/{WS}/conversations").json()["items"]
        assert [c["id"] for c in items] == [cid]
        assert items[0]["title"] == "会话一"
        # 未提供 title → 空串
        c2 = client.post("/api/v1/conversations", json={"workspace_id": WS}).json()
        assert c2["title"] == ""

    def test_create_missing_workspace_404(self, env):
        client, _, _ = env
        resp = client.post(
            "/api/v1/conversations", json={"workspace_id": "no-such"}
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_list_workspace_conversations_missing_ws_404(self, env):
        client, _, _ = env
        assert (
            client.get("/api/v1/workspaces/no-such/conversations").status_code == 404
        )

    def test_send_message_updates_detail_and_touch(self, env, monkeypatch):
        from unittest.mock import AsyncMock

        from tester_agent.graph.tool_agent import ToolAgentResult

        monkeypatch.setattr(
            "tester_agent.runtime.chat_agent.get_chat_model",
            lambda **_kwargs: object(),
        )
        monkeypatch.setattr(
            "tester_agent.runtime.chat_agent.run_tool_agent",
            AsyncMock(
                return_value=ToolAgentResult(final_text="ok", tool_trace=[])
            ),
        )
        client, db, _ = env
        _seed_workspace(db)
        cid = client.post(
            "/api/v1/conversations", json={"workspace_id": WS}
        ).json()["id"]
        before = client.get(f"/api/v1/conversations/{cid}").json()
        sent = client.post(
            f"/api/v1/conversations/{cid}/messages",
            json={"content": "帮我退款场景出用例", "kind": "chat"},
        )
        assert sent.status_code == 201
        body = sent.json()
        msg = body["user"]
        assert msg["role"] == "user"
        assert msg["author"] == "用户"
        assert msg["kind"] == "chat"
        assert body["assistant"]["role"] == "assistant"
        assert body["assistant"]["content"] == "ok"
        detail = client.get(f"/api/v1/conversations/{cid}").json()
        assert detail["updated_at"] >= before["updated_at"]  # touch 生效
        assert [m["id"] for m in detail["messages"]["items"]] == [
            body["assistant"]["id"],
            msg["id"],
        ]
        assert detail["tasks"] == []
        # change_request + context 落 payload（无 tool loop）
        cr = client.post(
            f"/api/v1/conversations/{cid}/messages",
            json={
                "content": "改一下摘要",
                "kind": "change_request",
                "context": {"stage": "link_identify"},
            },
        ).json()
        assert cr["assistant"] is None
        assert cr["user"]["kind"] == "change_request"
        assert cr["user"]["payload"] == {"stage": "link_identify"}

    def test_detail_contains_task_summary(self, env):
        client, db, _ = env
        _seed_task(db)  # 预种链：WS/CONV/TASK
        cid = client.post(
            "/api/v1/conversations", json={"workspace_id": WS}
        ).json()["id"]
        _run(db.aexecute("UPDATE task SET conversation_id = ? WHERE id = ?", (cid, TASK)))
        detail = client.get(f"/api/v1/conversations/{cid}").json()
        assert len(detail["tasks"]) == 1
        t = detail["tasks"][0]
        assert t["id"] == TASK
        assert t["status"] == "running"
        assert t["current_stage"] == "intake"

    def test_messages_pagination(self, env):
        client, db, _ = env
        _seed_workspace(db)
        cid = client.post(
            "/api/v1/conversations", json={"workspace_id": WS}
        ).json()["id"]
        # change_request：无 assistant，便于断言纯用户消息分页
        ids = [
            client.post(
                f"/api/v1/conversations/{cid}/messages",
                json={"content": f"m{i}", "kind": "change_request"},
            ).json()["user"]["id"]
            for i in range(3)
        ]
        page1 = client.get(
            f"/api/v1/conversations/{cid}/messages?limit=2"
        ).json()
        assert [m["id"] for m in page1["items"]] == list(reversed(ids))[:2]  # 倒序，无重复
        page2 = client.get(
            f"/api/v1/conversations/{cid}/messages?limit=2&cursor={page1['next_cursor']}"
        ).json()
        assert [m["id"] for m in page2["items"]] == [ids[0]]
        assert page2["next_cursor"] is None

    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/v1/conversations/no-such"),
            ("get", "/api/v1/conversations/no-such/messages"),
            ("post", "/api/v1/conversations/no-such/messages"),
        ],
    )
    def test_missing_conversation_404(self, env, method, path):
        client, _, _ = env
        kwargs = {"json": {"content": "x"}} if method == "post" else {}
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_send_message_validation(self, env):
        client, db, _ = env
        _seed_workspace(db)
        cid = client.post(
            "/api/v1/conversations", json={"workspace_id": WS}
        ).json()["id"]
        resp = client.post(
            f"/api/v1/conversations/{cid}/messages", json={"content": ""}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_BODY"


# ---------- model config / test ----------


class TestModelConfig:
    def test_get_default_empty(self, env):
        client, _, _ = env
        body = client.get("/api/v1/config/model").json()
        assert body == {
            "base_url": "",
            "api_key": "",
            "model": "",
            "temperature": 0.2,
            "top_p": 1.0,
            "timeout": 120,
        }

    def test_put_roundtrip(self, env):
        client, _, _ = env
        cfg = {
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-x",
            "model": "deepseek-chat",
            "temperature": 0.5,
            "top_p": 0.9,
            "timeout": 60,
        }
        resp = client.put("/api/v1/config/model", json=cfg)
        assert resp.status_code == 200
        assert resp.json() == cfg
        assert client.get("/api/v1/config/model").json() == cfg

    def test_put_validation_missing_required(self, env):
        client, _, _ = env
        resp = client.put("/api/v1/config/model", json={"base_url": "x"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_BODY"

    def test_probe_ok_uses_saved_config(self, env, monkeypatch):
        client, _, _ = env
        client.put("/api/v1/config/model", json={
            "base_url": "https://x/v1", "api_key": "sk", "model": "m1",
        })
        seen = {}

        def fake_build(model_config, runtime_config):
            seen["model_config"] = model_config
            return FakeLLM(["pong"], model="saved-model")

        monkeypatch.setattr(
            "tester_agent.api.config._build_client", fake_build
        )
        resp = client.post("/api/v1/config/model/test")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["model"] == "saved-model"
        assert body["error_code"] is None
        assert body["latency_ms"] >= 0
        # 探活读取的是已保存配置（PUT 后生效），而非进程启动配置
        assert seen["model_config"]["model"] == "m1"

    def test_probe_minimal_call_shape(self, env, monkeypatch):
        client, _, _ = env
        llm = FakeLLM(["pong"])
        monkeypatch.setattr(
            "tester_agent.api.config._build_client", lambda mc, rc: llm
        )
        client.post("/api/v1/config/model/test")
        assert llm.chat_calls == 1
        assert llm.calls[0]["messages"] == [{"role": "user", "content": "ping"}]

    def test_probe_timeout_maps_llm_timeout(self, env, monkeypatch):
        client, _, _ = env
        monkeypatch.setattr(
            "tester_agent.api.config._build_client",
            lambda mc, rc: FakeLLM([LLMTimeoutError("超时")]),
        )
        resp = client.post("/api/v1/config/model/test")
        assert resp.status_code == 504
        err = resp.json()["error"]
        assert err["code"] == "LLM_TIMEOUT"
        assert err["retryable"] is True

    def test_probe_unconfigured_400(self, env):
        client, _, _ = env
        # model_config={}（引导行未配置）→ 真实 from_configs 抛 LLMBadRequest
        resp = client.post("/api/v1/config/model/test")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "LLM_BAD_REQUEST"
