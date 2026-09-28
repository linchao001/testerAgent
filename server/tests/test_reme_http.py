"""WP-09：ReMe HTTP 真实适配（SP-1 结论：service 优先；caps F/F/T）。

覆盖：search/get_entry/list_index_tree 映射、caps 固定、网络失败→KbUnreachable、
Factory register("service")、Writer save_to_knowledge + 回查 verified。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.adapters.reme import (
    ReMeCaps,
    ReMeReaderFactory,
    local_entry_version,
)
from tester_agent.adapters.reme_http import (
    HttpReMeReader,
    HttpReMeWriter,
    build_http_reader,
    bucket_to_entry_type,
    register_service_builder,
)
from tester_agent.domain import EntryType
from tester_agent.errors import KbUnreachable, NotFoundError, ValidationError


def _ok(answer="", *, metadata=None):
    return {"success": True, "answer": answer, "metadata": metadata or {}}


def _fail(answer="Error: boom"):
    return {"success": False, "answer": answer, "metadata": {}}


def _transport(routes: dict):
    """routes: path -> Response dict | Exception | callable(request)->dict."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        spec = routes.get(path)
        if spec is None:
            return httpx.Response(404, json=_fail(f"unknown {path}"))
        if isinstance(spec, Exception):
            raise spec
        if callable(spec):
            body = spec(request)
        else:
            body = spec
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


async def _reader(routes: dict, **kw) -> HttpReMeReader:
    client = httpx.AsyncClient(
        transport=_transport(routes),
        base_url="http://reme.test",
    )
    return HttpReMeReader(
        base_url="http://reme.test",
        kb_id="zhb_kb",
        client=client,
        **kw,
    )


# ---------- mapping helpers ----------


class TestBucketMapping:
    def test_known_buckets(self):
        assert bucket_to_entry_type("business/wiki") == EntryType.BUSINESS
        assert bucket_to_entry_type("business/openapi") == EntryType.API
        assert bucket_to_entry_type("business/dbInfo") == EntryType.DB
        assert bucket_to_entry_type("test/defects") == EntryType.DEFECT
        assert bucket_to_entry_type("test/test_cases") == EntryType.FLOW_CASE

    def test_unknown_defaults_business(self):
        assert bucket_to_entry_type("mystery") == EntryType.BUSINESS
        assert bucket_to_entry_type("") == EntryType.BUSINESS


# ---------- reader ----------


class TestHttpReMeReader:
    async def test_caps_match_sp1(self):
        r = await _reader({})
        assert r.caps == ReMeCaps(
            metadata_filter=False, entry_version=False, passage_api=True
        )

    async def test_search_maps_chunks(self):
        routes = {
            "/knowledge_search": _ok(
                "hits",
                metadata={
                    "results": [
                        {
                            "id": "c1",
                            "path": "knowledge/business/wiki/gmv.md",
                            "text": "GMV 口径定义",
                            "start_line": 3,
                            "end_line": 8,
                            "scores": {"score": 0.9},
                        }
                    ]
                },
            )
        }
        r = await _reader(routes)
        hits = await r.search("GMV", top_k=5)
        assert len(hits) == 1
        e = hits[0]
        assert e.entry_id == "knowledge/business/wiki/gmv.md"
        assert e.title == "gmv"
        assert e.content == "GMV 口径定义"
        assert e.entry_type == EntryType.BUSINESS
        assert e.entry_version == ""  # caps.entry_version=False → 管线补 h-
        assert e.raw["path"] == "knowledge/business/wiki/gmv.md"
        assert e.raw["start_line"] == 3

    async def test_search_ignores_types_scope_server_side(self):
        """caps.metadata_filter=False：仍把请求发出，但不指望服务端按 types/scope 裁。"""
        seen = {}

        def capture(req: httpx.Request):
            seen["body"] = json.loads(req.content.decode())
            return _ok(metadata={"results": []})

        r = await _reader({"/knowledge_search": capture})
        await r.search(
            "x", top_k=3, types=["defect"], scope={"link_ids": ["L1"]}
        )
        assert seen["body"]["query"] == "x"
        assert seen["body"]["limit"] == 3
        assert "types" not in seen["body"]
        assert "scope" not in seen["body"]

    async def test_search_network_error(self):
        r = await _reader({"/knowledge_search": httpx.ConnectError("down")})
        with pytest.raises(KbUnreachable):
            await r.search("q", top_k=1)

    async def test_get_entry_happy(self):
        body = (
            "---\n"
            'name: "GMV口径"\n'
            "bucket: business/wiki\n"
            "updated_at: 2026-09-28T01:00:00+00:00\n"
            'signals: ["chain:交易域", "chain:下单支付"]\n'
            "---\n\n"
            "# GMV口径\n\n正文说明\n"
        )
        routes = {
            "/read": _ok(body, metadata={"path": "knowledge/business/wiki/gmv.md"}),
        }
        r = await _reader(routes)
        e = await r.get_entry("knowledge/business/wiki/gmv.md")
        assert e.entry_id == "knowledge/business/wiki/gmv.md"
        assert e.title == "GMV口径"
        assert "正文说明" in e.content
        assert e.entry_type == EntryType.BUSINESS
        assert e.updated_at == "2026-09-28T01:00:00+00:00"
        assert e.link_id == "交易域"
        assert e.story_id == "下单支付"
        assert e.entry_version == ""

    async def test_get_entry_missing(self):
        routes = {"/read": _fail("Error: file does not exist")}
        r = await _reader(routes)
        with pytest.raises(NotFoundError):
            await r.get_entry("knowledge/missing.md")

    async def test_list_index_tree_from_chain_signals(self):
        files = [
            "knowledge/business/wiki/a.md",
            "knowledge/business/wiki/b.md",
            "knowledge/test/test_design/td-链路树-业务主链总览.md",
        ]
        fm = {
            "knowledge/business/wiki/a.md": {
                "name": "规则A",
                "description": "下单一句话",
                "signals": ["chain:交易域", "chain:下单支付"],
            },
            "knowledge/business/wiki/b.md": {
                "name": "规则B",
                "description": "退款一句话",
                "signals": ["chain:交易域", "chain:退款逆向"],
            },
            "knowledge/test/test_design/td-链路树-业务主链总览.md": {
                "name": "链路树",
                "description": "总览",
                "signals": [],
            },
        }

        def list_job(_req):
            return _ok(metadata={"items": files, "count": len(files)})

        def fm_job(req: httpx.Request):
            payload = json.loads(req.content.decode())
            path = payload["path"]
            return _ok(
                metadata={
                    "path": path,
                    "exists": True,
                    "frontmatter": fm[path],
                }
            )

        r = await _reader({"/list": list_job, "/frontmatter_read": fm_job})
        tree = await r.list_index_tree()
        assert len(tree.links) == 1
        link = tree.links[0]
        assert link.link_id == "交易域"
        assert link.title == "交易域"
        assert link.entry_id == "link_index/交易域"
        story_ids = sorted(s.story_id for s in link.stories)
        assert story_ids == ["下单支付", "退款逆向"]
        assert all(s.entry_id.startswith("link_index/") for s in link.stories)


# ---------- factory / builder ----------


class TestServiceBuilder:
    async def test_register_and_probe(self):
        factory = ReMeReaderFactory()
        register_service_builder(factory)

        routes = {
            "/knowledge_search": _ok(metadata={"results": []}),
            "/list": _ok(metadata={"items": [], "count": 0}),
        }
        client = httpx.AsyncClient(
            transport=_transport(routes), base_url="http://reme.test"
        )

        async def builder(kb_config: dict):
            # inject shared client for mock
            return HttpReMeReader(
                base_url=kb_config["target"].rstrip("/"),
                kb_id=kb_config["kb_id"],
                client=client,
                options=kb_config.get("options") or {},
            )

        factory.register("service", builder)
        probe = await factory.probe(
            {"mode": "service", "target": "http://reme.test", "kb_id": "zhb_kb"}
        )
        assert probe.caps.passage_api is True
        assert probe.caps.metadata_filter is False
        assert probe.latency_ms >= 0

    async def test_build_http_reader_validates_mode(self):
        with pytest.raises(ValidationError):
            await build_http_reader(
                {"mode": "sdk", "target": "/x", "kb_id": "k"}
            )


# ---------- writer ----------


class TestHttpReMeWriter:
    async def test_write_and_verify(self):
        written = {}

        def save(req: httpx.Request):
            written.update(json.loads(req.content.decode()))
            return _ok(
                "Saved",
                metadata={
                    "knowledge_base_id": "zhb_kb",
                    "written": ["knowledge/business/wiki/新链路.md"],
                },
            )

        def read(req: httpx.Request):
            path = json.loads(req.content.decode())["path"]
            assert path == "knowledge/business/wiki/新链路.md"
            return _ok("# 新链路\n\nbody\n", metadata={"path": path})

        client = httpx.AsyncClient(
            transport=_transport({"/save_to_knowledge": save, "/read": read}),
            base_url="http://reme.test",
        )
        writer = HttpReMeWriter(base_url="http://reme.test", client=client)
        result = await writer.write_proposal(
            "token",
            {
                "op": "add",
                "entry": {
                    "title": "新链路",
                    "content": "一句话摘要",
                    "bucket": "business/wiki",
                },
            },
        )
        assert result.ok is True
        assert result.verified is True
        assert result.remote_ref == "knowledge/business/wiki/新链路.md"
        assert written["title"] == "新链路"
        assert written["content"] == "一句话摘要"
        assert written["bucket"] == "business/wiki"

    async def test_write_failure_ok_false(self):
        client = httpx.AsyncClient(
            transport=_transport(
                {"/save_to_knowledge": _fail("Knowledge base is locked")}
            ),
            base_url="http://reme.test",
        )
        writer = HttpReMeWriter(base_url="http://reme.test", client=client)
        result = await writer.write_proposal(
            "t", {"entry": {"title": "x", "content": "y"}}
        )
        assert result.ok is False
        assert result.verified is False
        assert result.error_code == "write_rejected"

    async def test_write_network_raises(self):
        client = httpx.AsyncClient(
            transport=_transport(
                {"/save_to_knowledge": httpx.ConnectError("down")}
            ),
            base_url="http://reme.test",
        )
        writer = HttpReMeWriter(base_url="http://reme.test", client=client)
        with pytest.raises(KbUnreachable):
            await writer.write_proposal(
                "t", {"entry": {"title": "x", "content": "y"}}
            )


class TestLocalVersionHelper:
    def test_pipeline_can_hash(self):
        assert local_entry_version("abc").startswith("h-")
