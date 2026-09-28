"""SdkReMeReader / SdkReMeWriter over mock ReMeMemoryManager."""

from __future__ import annotations

from pathlib import Path

import pytest

from tester_agent.adapters.reme_sdk import SdkReMeReader, SdkReMeWriter
from tester_agent.errors import KbUnreachable
from tester_agent.memory.manager import ReMeMemoryManager


class FakeReMeApp:
    def __init__(self, **_kwargs):
        self.is_started = False
        self.jobs: list[tuple[str, dict]] = []

    async def start(self) -> None:
        self.is_started = True

    async def close(self) -> None:
        self.is_started = False

    async def run_job(self, name: str, **kwargs):
        self.jobs.append((name, kwargs))

        class _Resp:
            success = True
            answer = ""
            metadata = {"results": []}

        if name == "knowledge_search":
            _Resp.metadata = {
                "results": [
                    {
                        "path": "knowledge/business/wiki/a.md",
                        "text": "hello wiki",
                        "id": "c1",
                    }
                ]
            }
        if name == "read":
            _Resp.answer = "---\nname: A\nsignals: [\"chain:链路甲\"]\n---\nbody"
        if name == "save_to_knowledge":
            _Resp.metadata = {"path": "knowledge/business/wiki/a.md"}
        if name == "list":
            _Resp.metadata = {"items": []}
        return _Resp()


@pytest.mark.asyncio
async def test_sdk_reader_search_and_get(tmp_path: Path):
    fake = FakeReMeApp()
    mgr = ReMeMemoryManager(
        "ws1",
        tmp_path / "reme",
        {"kb_id": "kb", "options": {}},
        reme_ctor=lambda **kw: fake,
    )
    await mgr.start()
    reader = SdkReMeReader(mgr, kb_id="kb")
    hits = await reader.search("q", top_k=5)
    assert len(hits) == 1
    assert hits[0].entry_id.endswith("a.md")
    assert "hello" in hits[0].content
    entry = await reader.get_entry("knowledge/business/wiki/a.md")
    assert entry.title == "A"
    assert entry.link_id == "链路甲"


@pytest.mark.asyncio
async def test_sdk_writer_save(tmp_path: Path):
    fake = FakeReMeApp()
    mgr = ReMeMemoryManager(
        "ws1",
        tmp_path / "reme",
        {"kb_id": "kb", "options": {}},
        reme_ctor=lambda **kw: fake,
    )
    await mgr.start()
    writer = SdkReMeWriter(mgr)
    result = await writer.write_proposal(
        "tok", {"title": "T", "content": "C", "bucket": "business/wiki"}
    )
    assert result.ok is True
    assert any(j[0] == "save_to_knowledge" for j in fake.jobs)


@pytest.mark.asyncio
async def test_sdk_reader_not_started(tmp_path: Path):
    mgr = ReMeMemoryManager(
        "ws1",
        tmp_path / "reme",
        {"kb_id": "kb", "options": {}},
        reme_ctor=lambda **kw: FakeReMeApp(),
    )
    # never start
    reader = SdkReMeReader(mgr)
    with pytest.raises(KbUnreachable):
        await reader.search("q", top_k=1)
