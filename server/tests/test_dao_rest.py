"""WP-04 验收：其余十类 DAO + Page 游标分页。

覆盖（验收口径：分页无重复无遗漏；workspace 强制过滤）：
- WorkspaceDAO：CRUD、软删（软删视同不存在、include_deleted 逃生口）、列表过滤软删、分页；
- AgentDAO：CRUD、bind/unbind 幂等、list_for_workspace JOIN 隔离、硬删级联绑定、JOIN 分页；
- ConversationDAO/MessageDAO：会话隔离、消息按会话过滤、touch、分页/tiebreak、JSON 默认值；
- TraceDAO：append/update_referenced 回填闭环、stage/version 过滤、任务隔离、Row↔Pydantic；
- SnapshotDAO：put/get/list、items/usage 等 JSON 往返、任务隔离；
- EventDAO：自增 id、list_after 边界/限量/任务隔离、purge_before 只清更旧；
- ReviewDAO：append/get/list 任务隔离与分页；
- ProposalDAO：幂等键冲突、fail_count 递增且行保持 pending、终态、状态过滤、ws 隔离；
- ConfigDAO：引导行读取、model/runtime 互不覆盖、缺行 NotFound。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.domain import (
    Candidate,
    CaseRecord,
    CaseStatus,
    DegradedStep,
    EntryType,
    InjectedItem,
    Lineage,
    MessageKind,
    MessageRole,
    ProposalStatus,
    QueryVariant,
    ReviewStatus,
    TraceRefs,
)
from tester_agent.errors import NotFoundError, VersionConflict
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    AgentDAO,
    AgentRow,
    CaseRow,
    ConfigDAO,
    ConversationDAO,
    ConversationRow,
    EventDAO,
    MessageDAO,
    MessageRow,
    Page,
    ProposalDAO,
    ProposalRow,
    ReviewDAO,
    ReviewRow,
    SnapshotDAO,
    SnapshotRow,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
    TraceDAO,
    TraceRow,
    WorkspaceDAO,
    WorkspaceRow,
)

TS0 = "2026-09-20T00:00:00.000Z"
STAGE_LINK = "link_identify"
STAGE_POINT = "point_write"


# ---------- 夹具与种子助手 ----------


@pytest.fixture()
def db(tmp_path):
    db_path = tmp_path / "app.db"
    run_migrations(db_path)
    handle = Database(db_path)
    yield handle
    handle.close()


async def _mk_ws(db: Database, ws: str = "ws1", *, now: str = TS0, **kw) -> WorkspaceRow:
    row = WorkspaceRow.create(id=ws, name=kw.pop("name", f"name-{ws}"), now=now, **kw)
    await WorkspaceDAO(db).create(row)
    return row


async def _mk_conv(
    db: Database, conv: str = "conv1", ws: str = "ws1", *, now: str = TS0
) -> ConversationRow:
    row = ConversationRow.create(id=conv, workspace_id=ws, now=now)
    await ConversationDAO(db).create(row)
    return row


async def _mk_task(
    db: Database,
    task_id: str = "t1",
    *,
    ws: str = "ws1",
    conv: str = "conv1",
    run: str = "run-1",
    now: str = TS0,
) -> TaskRow:
    row = TaskRow.create(
        id=task_id,
        conversation_id=conv,
        workspace_id=ws,
        status="running",
        current_stage=STAGE_LINK,
        langgraph_thread_id=f"thread-{task_id}",
        graph_run_id=run,
        now=now,
    )
    await TaskDAO(db).create(row)
    return row


async def _seed_chain(
    db: Database, *, ws: str = "ws1", conv: str = "conv1", task: str = "t1"
) -> None:
    """工作区→会话→任务 外键链（trace/snapshot/event/review 的前置）。"""
    await _mk_ws(db, ws)
    await _mk_conv(db, conv, ws)
    await _mk_task(db, task, ws=ws, conv=conv)


async def _mk_case(
    db: Database,
    case_id: str = "c1",
    *,
    task: str = "t1",
    now: str = TS0,
    review: ReviewStatus = ReviewStatus.PENDING,
) -> str:
    record = CaseRecord(
        case_id=case_id,
        point_id=f"pt-{case_id}",
        stage_version=1,
        lineage=Lineage(root_case_id=case_id),
        status=CaseStatus.ACTIVE,
        review_status=review,
        file_path=f"workspaces/{task}/v1/{case_id}.md",
        content_hash=f"hash-{case_id}",
        title=f"用例-{case_id}",
        trace_refs=TraceRefs(),
    )
    await TestcaseDAO(db).put_batch([CaseRow.from_record(task, record, now=now)])
    return case_id


# ---------- WorkspaceDAO ----------


class TestWorkspaceDAO:
    async def test_create_get_roundtrip_and_json_defaults(self, db):
        await _mk_ws(db, "ws1", kb_config={"mode": "sdk", "kb_id": "kb-9"})
        got = await WorkspaceDAO(db).get("ws1")
        assert got.id == "ws1"
        assert got.name == "name-ws1"
        assert got.description == ""
        assert got.kb_config_obj() == {"mode": "sdk", "kb_id": "kb-9"}
        assert got.deleted_at is None
        assert got.created_at == TS0

    async def test_get_missing_raises(self, db):
        with pytest.raises(NotFoundError):
            await WorkspaceDAO(db).get("ghost")

    async def test_soft_delete_hides_get_and_list(self, db):
        await _mk_ws(db, "ws1", now="2026-09-20T00:00:00.000Z")
        await _mk_ws(db, "ws2", now="2026-09-20T00:00:01.000Z")
        dao = WorkspaceDAO(db)
        await dao.soft_delete("ws1")

        # 软删与不存在同等待遇（不暴露存在性）
        with pytest.raises(NotFoundError):
            await dao.get("ws1")
        with pytest.raises(NotFoundError):
            await dao.update("ws1", name="x")
        with pytest.raises(NotFoundError):
            await dao.soft_delete("ws1")
        # 列表不含软删行
        page = await dao.list(cursor=None, limit=50)
        assert [w.id for w in page.items] == ["ws2"]
        # 逃生口：include_deleted 可取回（对账/审计路径）
        deleted = await dao.get("ws1", include_deleted=True)
        assert deleted.deleted_at is not None

    async def test_soft_delete_missing_raises(self, db):
        with pytest.raises(NotFoundError):
            await WorkspaceDAO(db).soft_delete("ghost")

    async def test_update_sentinel_does_not_clobber(self, db):
        await _mk_ws(db, "ws1", description="原始描述", kb_config={"mode": "service"})
        dao = WorkspaceDAO(db)
        await dao.update("ws1", name="新名字")
        got = await dao.get("ws1")
        assert got.name == "新名字"
        assert got.description == "原始描述"  # 未传列保持
        assert got.kb_config_obj() == {"mode": "service"}

        await dao.update("ws1", kb_config={"mode": "sdk"})
        got = await dao.get("ws1")
        assert got.name == "新名字"
        assert got.description == "原始描述"
        assert got.kb_config_obj() == {"mode": "sdk"}
        # 无字段可改时短路，不报错
        await dao.update("ws1")

    async def test_update_missing_raises(self, db):
        with pytest.raises(NotFoundError):
            await WorkspaceDAO(db).update("ghost", name="x")

    async def test_list_pagination_no_dup_no_gap_and_tiebreak(self, db):
        for i in range(5):
            await _mk_ws(db, f"ws{i}", now=f"2026-09-20T00:00:0{i}.000Z")
        dao = WorkspaceDAO(db)

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page: Page[WorkspaceRow] = await dao.list(cursor=cursor, limit=2)
            seen.extend(w.id for w in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert sorted(seen) == [f"ws{i}" for i in range(5)]
        assert len(seen) == len(set(seen))
        assert seen == [f"ws{i}" for i in range(4, -1, -1)]  # created_at 倒序

        # 同时间戳 id tiebreak（两条均新于分页 5 行，位于首页）
        await _mk_ws(db, "aaa", now="2026-09-20T00:00:05.000Z")
        await _mk_ws(db, "bbb", now="2026-09-20T00:00:05.000Z")
        page1 = await dao.list(cursor=None, limit=1)
        assert page1.items[0].id == "bbb"
        page2 = await dao.list(cursor=page1.next_cursor, limit=1)
        assert page2.items[0].id == "aaa"
        page3 = await dao.list(cursor=page2.next_cursor, limit=1)
        assert page3.items[0].id == "ws4"


# ---------- AgentDAO ----------


class TestAgentDAO:
    async def _mk_agent(
        self,
        db: Database,
        agent_id: str,
        *,
        now: str = TS0,
        config: dict | None = None,
        builtin: bool = False,
    ) -> AgentRow:
        row = AgentRow.create(
            id=agent_id, name=f"agent-{agent_id}", config=config, builtin=builtin,
            now=now,
        )
        await AgentDAO(db).create(row)
        return row

    async def test_create_get_roundtrip(self, db):
        await self._mk_agent(db, "a1", config={"temperature": 0.2}, builtin=True)
        got = await AgentDAO(db).get("a1")
        assert got.builtin == 1
        assert got.agent_type == "case_designer"
        assert got.config_obj() == {"temperature": 0.2}

    async def test_get_update_delete_missing_raise(self, db):
        dao = AgentDAO(db)
        with pytest.raises(NotFoundError):
            await dao.get("ghost")
        with pytest.raises(NotFoundError):
            await dao.update("ghost", name="x")
        with pytest.raises(NotFoundError):
            await dao.delete("ghost")

    async def test_update_sentinel_and_delete(self, db):
        await self._mk_agent(db, "a1", config={"k": 1})
        dao = AgentDAO(db)
        await dao.update("a1", name="改名")
        got = await dao.get("a1")
        assert got.name == "改名"
        assert got.config_obj() == {"k": 1}  # config 未被覆盖
        await dao.delete("a1")
        with pytest.raises(NotFoundError):
            await dao.get("a1")

    async def test_bind_idempotent_and_list_isolation(self, db):
        await _mk_ws(db, "ws1")
        await _mk_ws(db, "ws2")
        await self._mk_agent(db, "a1")
        await self._mk_agent(db, "a2")
        await self._mk_agent(db, "a3")
        dao = AgentDAO(db)
        # a1→ws1；a2→ws1+ws2；a3→ws2；重复绑定静默成功且不产生重复行
        await dao.bind("a1", "ws1")
        await dao.bind("a2", "ws1")
        await dao.bind("a2", "ws1")
        await dao.bind("a2", "ws2")
        await dao.bind("a3", "ws2")

        count = await db.aquery_one("SELECT COUNT(*) AS c FROM agent_workspace")
        assert count["c"] == 4

        ws1 = await dao.list_for_workspace("ws1", cursor=None, limit=50)
        assert {a.id for a in ws1.items} == {"a1", "a2"}
        ws2 = await dao.list_for_workspace("ws2", cursor=None, limit=50)
        assert {a.id for a in ws2.items} == {"a2", "a3"}

        # unbind 后该工作区不可见，另一工作区绑定保留
        await dao.unbind("a2", "ws1")
        ws1 = await dao.list_for_workspace("ws1", cursor=None, limit=50)
        assert {a.id for a in ws1.items} == {"a1"}
        ws2 = await dao.list_for_workspace("ws2", cursor=None, limit=50)
        assert {a.id for a in ws2.items} == {"a2", "a3"}
        # 解绑后重新绑定可用
        await dao.bind("a2", "ws1")
        ws1 = await dao.list_for_workspace("ws1", cursor=None, limit=50)
        assert {a.id for a in ws1.items} == {"a1", "a2"}

    async def test_delete_cascades_binding(self, db):
        await _mk_ws(db, "ws1")
        await self._mk_agent(db, "a1")
        dao = AgentDAO(db)
        await dao.bind("a1", "ws1")
        await dao.delete("a1")  # agent_workspace ON DELETE CASCADE
        rows = await db.aquery(
            "SELECT agent_id FROM agent_workspace WHERE agent_id = ?", ("a1",)
        )
        assert rows == []
        page = await dao.list_for_workspace("ws1", cursor=None, limit=50)
        assert page.items == []

    async def test_list_for_workspace_pagination_and_tiebreak(self, db):
        await _mk_ws(db, "ws1")
        dao = AgentDAO(db)
        for i in range(5):
            await self._mk_agent(db, f"a{i}", now=f"2026-09-20T00:00:0{i}.000Z")
            await dao.bind(f"a{i}", "ws1")

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page: Page[AgentRow] = await dao.list_for_workspace(
                "ws1", cursor=cursor, limit=2
            )
            seen.extend(a.id for a in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert sorted(seen) == [f"a{i}" for i in range(5)]
        assert len(seen) == len(set(seen))
        assert seen == [f"a{i}" for i in range(4, -1, -1)]

        # JOIN 场景同时间戳仍按 a.id 倒序稳定翻页（qualify="a."）
        await self._mk_agent(db, "xaaa", now="2026-09-20T00:00:05.000Z")
        await self._mk_agent(db, "xbbb", now="2026-09-20T00:00:05.000Z")
        await dao.bind("xaaa", "ws1")
        await dao.bind("xbbb", "ws1")
        page1 = await dao.list_for_workspace("ws1", cursor=None, limit=1)
        assert page1.items[0].id == "xbbb"
        page2 = await dao.list_for_workspace(
            "ws1", cursor=page1.next_cursor, limit=1
        )
        assert page2.items[0].id == "xaaa"


# ---------- ConversationDAO / MessageDAO ----------


class TestConversationDAO:
    async def test_create_get_and_isolation(self, db):
        await _mk_ws(db, "ws1")
        await _mk_ws(db, "ws2")
        await _mk_conv(db, "conv1", "ws1")
        await _mk_conv(db, "conv2", "ws2")
        dao = ConversationDAO(db)
        got = await dao.get("conv1")
        assert got.workspace_id == "ws1"
        assert got.title == ""
        assert got.updated_at == got.created_at == TS0

        page = await dao.list_by_workspace("ws1", cursor=None, limit=50)
        assert [c.id for c in page.items] == ["conv1"]
        page2 = await dao.list_by_workspace("ws2", cursor=None, limit=50)
        assert [c.id for c in page2.items] == ["conv2"]

    async def test_get_and_touch_missing_raise(self, db):
        with pytest.raises(NotFoundError):
            await ConversationDAO(db).get("ghost")
        with pytest.raises(NotFoundError):
            await ConversationDAO(db).touch("ghost")

    async def test_touch_refreshes_updated_at(self, db):
        await _mk_ws(db)
        await _mk_conv(db)
        await ConversationDAO(db).touch("conv1")
        got = await ConversationDAO(db).get("conv1")
        assert got.updated_at > TS0  # 刷为真实当前时间
        assert got.created_at == TS0  # created_at 不动

    async def test_list_pagination_no_dup_no_gap(self, db):
        await _mk_ws(db)
        for i in range(5):
            await _mk_conv(db, f"conv{i}", now=f"2026-09-20T00:00:0{i}.000Z")
        dao = ConversationDAO(db)
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page: Page[ConversationRow] = await dao.list_by_workspace(
                "ws1", cursor=cursor, limit=2
            )
            seen.extend(c.id for c in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert sorted(seen) == [f"conv{i}" for i in range(5)]
        assert len(seen) == len(set(seen))
        assert seen == [f"conv{i}" for i in range(4, -1, -1)]


class TestMessageDAO:
    async def _setup_two_convs(self, db: Database) -> None:
        await _mk_ws(db, "ws1")
        await _mk_ws(db, "ws2")
        await _mk_conv(db, "conv1", "ws1")
        await _mk_conv(db, "conv2", "ws2")

    async def test_put_get_roundtrip_and_json_defaults(self, db):
        await self._setup_two_convs(db)
        row = MessageRow.create(
            id="m1",
            conversation_id="conv1",
            role=MessageRole.USER,
            kind=MessageKind.CHAT,
            content="你好",
        )
        await MessageDAO(db).put(row)
        got = await MessageDAO(db).get("m1")
        assert got.role == "user"
        assert got.kind == "chat"
        assert got.content == "你好"
        assert got.task_id is None
        assert got.ref_artifact_id is None
        assert got.payload_dict() == {}  # JSON 默认值闭环
        assert got.created_at  # 缺省由 DAO 补当前时间

    async def test_get_missing_raises(self, db):
        with pytest.raises(NotFoundError):
            await MessageDAO(db).get("ghost")

    async def test_list_conversation_isolation(self, db):
        await self._setup_two_convs(db)
        dao = MessageDAO(db)
        await dao.put(
            MessageRow.create(id="m1", conversation_id="conv1", role="user",
                              kind="chat", content="一号")
        )
        await dao.put(
            MessageRow.create(id="m2", conversation_id="conv2", role="user",
                              kind="chat", content="二号")
        )
        p1 = await dao.list_by_conversation("conv1", cursor=None, limit=50)
        assert [m.id for m in p1.items] == ["m1"]
        p2 = await dao.list_by_conversation("conv2", cursor=None, limit=50)
        assert [m.id for m in p2.items] == ["m2"]

    async def test_list_pagination_no_dup_no_gap_and_tiebreak(self, db):
        await _mk_ws(db)
        await _mk_conv(db)
        dao = MessageDAO(db)
        for i in range(5):
            await dao.put(
                MessageRow.create(
                    id=f"m{i}", conversation_id="conv1", role="user", kind="chat",
                    content=f"msg-{i}", now=f"2026-09-20T00:00:0{i}.000Z",
                )
            )
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page: Page[MessageRow] = await dao.list_by_conversation(
                "conv1", cursor=cursor, limit=2
            )
            seen.extend(m.id for m in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == [f"m{i}" for i in range(4, -1, -1)]
        assert len(seen) == len(set(seen))

        # 同时间戳 tiebreak（两条均新于分页 5 行，位于首页）
        await dao.put(
            MessageRow.create(id="maaa", conversation_id="conv1", role="user",
                              kind="chat", content="a",
                              now="2026-09-20T00:00:05.000Z")
        )
        await dao.put(
            MessageRow.create(id="mbbb", conversation_id="conv1", role="user",
                              kind="chat", content="b",
                              now="2026-09-20T00:00:05.000Z")
        )
        page1 = await dao.list_by_conversation("conv1", cursor=None, limit=1)
        assert page1.items[0].id == "mbbb"
        page2 = await dao.list_by_conversation(
            "conv1", cursor=page1.next_cursor, limit=1
        )
        assert page2.items[0].id == "maaa"


# ---------- TraceDAO ----------


def _trace_row(
    trace_id: str,
    *,
    task: str = "t1",
    stage: str = STAGE_LINK,
    version: int = 1,
    node: str | None = None,
    now: str = TS0,
) -> TraceRow:
    return TraceRow.create(
        id=trace_id,
        task_id=task,
        graph_run_id="run-1",
        stage=stage,
        stage_version=version,
        node=node or stage,
        query_variant=QueryVariant(channel="keyword", text="退款 超时"),
        candidates=[
            Candidate(
                entry_id="e1",
                entry_version="v1",
                title="退款链路",
                score=0.91,
                source_channel="keyword:1",
                entry_type=EntryType.BUSINESS,
                kept=True,
            ),
            Candidate(
                entry_id="e2",
                entry_version="v1",
                title="缺陷-重复退款",
                score=0.32,
                source_channel="raw:0",
                entry_type=EntryType.DEFECT,
                kept=False,
                drop_reason="rerank_cutoff",
            ),
        ],
        injected_ids=["e1"],
        degraded=[
            DegradedStep(step="rerank", reason="timeout", fallback="score_passthrough")
        ],
        now=now,
    )


class TestTraceDAO:
    async def test_append_get_and_pydantic_roundtrip(self, db):
        await _seed_chain(db)
        dao = TraceDAO(db)
        tid = await dao.append(_trace_row("tr1"))
        assert tid == "tr1"
        got = await dao.get("tr1")
        assert got.query_obj() == QueryVariant(channel="keyword", text="退款 超时")
        candidates = got.candidates_obj()
        assert [c.entry_id for c in candidates] == ["e1", "e2"]
        assert candidates[1].drop_reason == "rerank_cutoff"
        assert got.injected_ids_obj() == ["e1"]
        # 生成前引用列默认空
        assert got.referenced_ids_obj() == []
        assert got.hallucinated_ids_obj() == []
        assert got.weak_ref_ids_obj() == []
        degraded = got.degraded_obj()
        assert degraded[0].fallback == "score_passthrough"

    async def test_append_minimal_json_defaults(self, db):
        await _seed_chain(db)
        row = TraceRow.create(
            id="tr0", task_id="t1", graph_run_id="run-1", stage=STAGE_LINK,
            stage_version=1, node=STAGE_LINK,
        )
        await TraceDAO(db).append(row)
        got = await TraceDAO(db).get("tr0")
        assert got.query_obj() is None
        assert got.candidates_obj() == []
        assert got.injected_ids_obj() == []
        assert got.degraded_obj() == []

    async def test_update_referenced_backfill(self, db):
        await _seed_chain(db)
        dao = TraceDAO(db)
        await dao.append(_trace_row("tr1"))
        await dao.update_referenced(
            "tr1", referenced=["e1"], weak=["e3"], hallucinated=["e9"]
        )
        got = await dao.get("tr1")
        assert got.referenced_ids_obj() == ["e1"]
        assert got.weak_ref_ids_obj() == ["e3"]
        assert got.hallucinated_ids_obj() == ["e9"]
        # 回填不影响注入白名单/候选
        assert got.injected_ids_obj() == ["e1"]
        assert len(got.candidates_obj()) == 2

    async def test_update_referenced_and_get_missing_raise(self, db):
        dao = TraceDAO(db)
        with pytest.raises(NotFoundError):
            await dao.get("ghost")
        with pytest.raises(NotFoundError):
            await dao.update_referenced("ghost", [], [], [])

    async def test_list_filters_isolation_and_pagination(self, db):
        await _seed_chain(db, ws="ws1", conv="conv1", task="t1")
        await _seed_chain(db, ws="ws2", conv="conv2", task="t2")
        dao = TraceDAO(db)
        await dao.append(_trace_row("tr-link-1", now="2026-09-20T00:00:00.000Z"))
        await dao.append(_trace_row("tr-link-2", now="2026-09-20T00:00:01.000Z"))
        await dao.append(
            _trace_row("tr-point-1", stage=STAGE_POINT, node=STAGE_POINT,
                       now="2026-09-20T00:00:02.000Z")
        )
        await dao.append(
            _trace_row("tr-link-v2", version=2, now="2026-09-20T00:00:03.000Z")
        )
        await dao.append(_trace_row("tr-other-task", task="t2"))

        # 任务隔离
        page = await dao.list_by_task(
            "t1", stage=None, version=None, cursor=None, limit=50
        )
        assert {t.id for t in page.items} == {
            "tr-link-1", "tr-link-2", "tr-point-1", "tr-link-v2"
        }
        # stage 过滤
        page = await dao.list_by_task(
            "t1", stage=STAGE_LINK, version=None, cursor=None, limit=50
        )
        assert {t.id for t in page.items} == {
            "tr-link-1", "tr-link-2", "tr-link-v2"
        }
        # version 过滤
        page = await dao.list_by_task(
            "t1", stage=None, version=2, cursor=None, limit=50
        )
        assert [t.id for t in page.items] == ["tr-link-v2"]

        # 分页无重无漏，created_at 倒序
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            p: Page[TraceRow] = await dao.list_by_task(
                "t1", stage=None, version=None, cursor=cursor, limit=2
            )
            seen.extend(t.id for t in p.items)
            cursor = p.next_cursor
            if cursor is None:
                break
        assert seen == [
            "tr-link-v2", "tr-point-1", "tr-link-2", "tr-link-1"
        ]
        assert len(seen) == len(set(seen))


# ---------- SnapshotDAO ----------


class TestSnapshotDAO:
    async def test_put_get_roundtrip(self, db):
        await _seed_chain(db)
        row = SnapshotRow.create(
            id="s1",
            task_id="t1",
            graph_run_id="run-1",
            stage=STAGE_POINT,
            stage_version=1,
            node=STAGE_POINT,
            prompt_template_ver="point_write@2026-09-01",
            items=[
                InjectedItem(
                    entry_id="e1", entry_version="v1", title="退款链路",
                    tokens_est=820, position=0, char_offset=12, byte_length=300,
                )
            ],
            snapshot_path="workspace/t1/snapshots/point_write/v1/point_write-x.jsonl",
            total_tokens_est=820,
            budget=16000,
            truncated=True,
            model_ref={"model": "deepseek-chat"},
            usage={"prompt_tokens": 1000, "completion_tokens": 200},
            latencies={"retrieve_ms": 35},
            batch_id="b1",
        )
        sid = await SnapshotDAO(db).put(row)
        assert sid == "s1"
        got = await SnapshotDAO(db).get("s1")
        assert got.truncated == 1
        assert got.budget == 16000
        assert got.batch_id == "b1"
        items = got.items_obj()
        assert items[0].entry_id == "e1"
        assert items[0].position == 0
        assert got.model_ref_obj() == {"model": "deepseek-chat"}
        assert got.usage_obj() == {"prompt_tokens": 1000, "completion_tokens": 200}
        assert got.latencies_obj() == {"retrieve_ms": 35}

    async def test_put_minimal_json_defaults(self, db):
        await _seed_chain(db)
        row = SnapshotRow.create(
            id="s0", task_id="t1", graph_run_id="run-1", stage=STAGE_LINK,
            stage_version=1, node=STAGE_LINK, prompt_template_ver="link@1",
        )
        await SnapshotDAO(db).put(row)
        got = await SnapshotDAO(db).get("s0")
        assert got.items_obj() == []
        assert got.model_ref_obj() == {}
        assert got.usage_obj() == {}
        assert got.latencies_obj() == {}
        assert got.truncated == 0
        assert got.snapshot_path is None

    async def test_get_missing_raises(self, db):
        with pytest.raises(NotFoundError):
            await SnapshotDAO(db).get("ghost")

    async def test_list_isolation_and_pagination(self, db):
        await _seed_chain(db, ws="ws1", conv="conv1", task="t1")
        await _seed_chain(db, ws="ws2", conv="conv2", task="t2")
        dao = SnapshotDAO(db)

        def _row(sid: str, task: str, now: str) -> SnapshotRow:
            return SnapshotRow.create(
                id=sid, task_id=task, graph_run_id="run-1", stage=STAGE_LINK,
                stage_version=1, node=STAGE_LINK, prompt_template_ver="link@1",
                now=now,
            )

        for i in range(5):
            await dao.put(_row(f"s{i}", "t1", f"2026-09-20T00:00:0{i}.000Z"))
        await dao.put(_row("s-other", "t2", TS0))

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page: Page[SnapshotRow] = await dao.list_by_task(
                "t1", cursor=cursor, limit=2
            )
            seen.extend(s.id for s in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == [f"s{i}" for i in range(4, -1, -1)]
        assert len(seen) == len(set(seen))
        other = await dao.list_by_task("t2", cursor=None, limit=50)
        assert [s.id for s in other.items] == ["s-other"]


# ---------- EventDAO ----------


class TestEventDAO:
    async def test_append_returns_increasing_ids(self, db):
        await _seed_chain(db)
        dao = EventDAO(db)
        id1 = await dao.append("t1", "node_started", {"node": STAGE_LINK})
        id2 = await dao.append("t1", "node_progress", {"pct": 50})
        id3 = await dao.append("t1", "node_finished", {})
        assert isinstance(id1, int)
        assert id1 < id2 < id3
        # payload 默认 {} 闭环
        rows = await dao.list_after("t1", 0)
        assert rows[2].payload_dict() == {}
        assert rows[0].type == "node_started"
        assert rows[0].payload_dict() == {"node": STAGE_LINK}

    async def test_list_after_boundary_limit_and_task_isolation(self, db):
        await _seed_chain(db, ws="ws1", conv="conv1", task="t1")
        await _seed_chain(db, ws="ws2", conv="conv2", task="t2")
        dao = EventDAO(db)
        ids = [await dao.append("t1", f"e{i}", {"i": i}) for i in range(4)]
        await dao.append("t2", "other", {})

        # after_id 为开区间边界（不含本身），按 id 升序
        after_first = await dao.list_after("t1", ids[0])
        assert [e.id for e in after_first] == ids[1:]
        # limit 截断仍保序
        limited = await dao.list_after("t1", 0, limit=2)
        assert [e.id for e in limited] == ids[:2]
        # 追平后为空
        assert await dao.list_after("t1", ids[-1]) == []
        # 任务隔离：t2 事件不串入 t1
        t1_all = await dao.list_after("t1", 0)
        assert len(t1_all) == 4
        assert all(e.task_id == "t1" for e in t1_all)

    async def test_purge_before_deletes_only_older(self, db):
        await _seed_chain(db)
        dao = EventDAO(db)
        new_id = await dao.append("t1", "new", {})
        # 手工落一条更早的事件（保留期清理目标）
        await db.aexecute(
            "INSERT INTO task_event (task_id, type, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t1", "old", "{}", "2026-09-01T00:00:00.000Z"),
        )
        cutoff = "2026-09-15T00:00:00.000Z"
        deleted = await dao.purge_before(cutoff)
        assert deleted == 1
        remaining = await dao.list_after("t1", 0)
        assert [e.id for e in remaining] == [new_id]
        # 再跑幂等：没有更旧行
        assert await dao.purge_before(cutoff) == 0


# ---------- ReviewDAO ----------


class TestReviewDAO:
    async def test_append_get_detail_roundtrip(self, db):
        await _seed_chain(db)
        await _mk_case(db, "c1")
        row = ReviewRow.create(
            id="r1", task_id="t1", testcase_id="c1", action="adopted",
            detail={"reason": "覆盖到位", "reviewer": "tester"},
        )
        rid = await ReviewDAO(db).append(row)
        assert rid == "r1"
        got = await ReviewDAO(db).get("r1")
        assert got.action == "adopted"
        assert got.testcase_id == "c1"
        assert got.detail_dict() == {"reason": "覆盖到位", "reviewer": "tester"}

    async def test_append_detail_defaults_and_get_missing(self, db):
        await _seed_chain(db)
        await _mk_case(db, "c1")
        await ReviewDAO(db).append(
            ReviewRow.create(id="r0", task_id="t1", testcase_id="c1", action="open")
        )
        assert (await ReviewDAO(db).get("r0")).detail_dict() == {}
        with pytest.raises(NotFoundError):
            await ReviewDAO(db).get("ghost")

    async def test_list_task_isolation_and_pagination(self, db):
        await _seed_chain(db, ws="ws1", conv="conv1", task="t1")
        await _seed_chain(db, ws="ws2", conv="conv2", task="t2")
        await _mk_case(db, "c1", task="t1")
        await _mk_case(db, "c2", task="t2")
        dao = ReviewDAO(db)
        for i in range(5):
            await dao.append(
                ReviewRow.create(
                    id=f"r{i}", task_id="t1", testcase_id="c1", action="adopted",
                    now=f"2026-09-20T00:00:0{i}.000Z",
                )
            )
        await dao.append(
            ReviewRow.create(id="r-other", task_id="t2", testcase_id="c2",
                             action="rejected")
        )

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page: Page[ReviewRow] = await dao.list_by_task(
                "t1", cursor=cursor, limit=2
            )
            seen.extend(r.id for r in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == [f"r{i}" for i in range(4, -1, -1)]
        assert len(seen) == len(set(seen))
        other = await dao.list_by_task("t2", cursor=None, limit=50)
        assert [r.id for r in other.items] == ["r-other"]


# ---------- ProposalDAO ----------


def _proposal_row(
    proposal_id: str,
    *,
    ws: str = "ws1",
    task: str | None = None,
    key: str | None = None,
    now: str = TS0,
    status: str = "pending",
) -> ProposalRow:
    return ProposalRow.create(
        id=proposal_id,
        workspace_id=ws,
        task_id=task,
        payload={"entries": [{"id": "e1"}]},
        status=status,
        idempotency_key=key,
        expires_at="2026-12-31T00:00:00.000Z",
        now=now,
    )


class TestProposalDAO:
    async def test_create_get_defaults(self, db):
        await _mk_ws(db)
        await ProposalDAO(db).create(_proposal_row("p1", key="k1"))
        got = await ProposalDAO(db).get("p1")
        assert got.status == ProposalStatus.PENDING.value
        assert got.fail_count == 0
        assert got.confirmed_at is None
        assert got.write_result is None
        assert got.payload_dict() == {"entries": [{"id": "e1"}]}

    async def test_duplicate_idempotency_key_raises_conflict(self, db):
        await _mk_ws(db)
        dao = ProposalDAO(db)
        await dao.create(_proposal_row("p1", key="dup-key"))
        with pytest.raises(VersionConflict):
            await dao.create(_proposal_row("p2", key="dup-key"))
        # 不同键可正常写入；NULL 键互不冲突（可空非唯一值语义）
        await dao.create(_proposal_row("p3", key="other"))
        await dao.create(_proposal_row("p4", key=None))

    async def test_increment_fail_count_keeps_pending(self, db):
        await _mk_ws(db)
        dao = ProposalDAO(db)
        await dao.create(_proposal_row("p1", key="k1"))
        assert await dao.increment_fail_count("p1") == 1
        assert await dao.increment_fail_count("p1") == 2
        got = await dao.get("p1")
        assert got.fail_count == 2
        assert got.status == "pending"  # 失败不终态，保留可重试（dd §11.4）

    async def test_mark_confirmed_terminal_and_write_result(self, db):
        await _mk_ws(db)
        dao = ProposalDAO(db)
        await dao.create(_proposal_row("p1", key="k1"))
        result = {"written": 2, "verified": True, "needs_manual_check": False}
        await dao.mark_confirmed("p1", result)
        got = await dao.get("p1")
        assert got.status == "confirmed"
        assert got.confirmed_at is not None
        assert got.write_result_dict() == result

    async def test_set_status(self, db):
        await _mk_ws(db)
        dao = ProposalDAO(db)
        await dao.create(_proposal_row("p1", key="k1"))
        await dao.set_status("p1", ProposalStatus.REJECTED)
        assert (await dao.get("p1")).status == "rejected"

    async def test_missing_raise(self, db):
        dao = ProposalDAO(db)
        with pytest.raises(NotFoundError):
            await dao.get("ghost")
        with pytest.raises(NotFoundError):
            await dao.increment_fail_count("ghost")
        with pytest.raises(NotFoundError):
            await dao.mark_confirmed("ghost", {})
        with pytest.raises(NotFoundError):
            await dao.set_status("ghost", "rejected")

    async def test_list_status_filter_isolation_pagination(self, db):
        await _mk_ws(db, "ws1")
        await _mk_ws(db, "ws2")
        dao = ProposalDAO(db)
        for i in range(3):
            await dao.create(
                _proposal_row(f"pend-{i}", key=f"k-pend-{i}",
                              now=f"2026-09-20T00:00:0{i}.000Z")
            )
        for i in range(2):
            await dao.create(
                _proposal_row(f"conf-{i}", key=f"k-conf-{i}",
                              status="confirmed",
                              now=f"2026-09-20T00:00:1{i}.000Z")
            )
        await dao.create(_proposal_row("p-ws2", ws="ws2", key="k-ws2"))

        # workspace 隔离
        page = await dao.list_by_workspace(
            "ws1", status=None, cursor=None, limit=50
        )
        assert {p.id for p in page.items} == {
            "pend-0", "pend-1", "pend-2", "conf-0", "conf-1"
        }
        # status 过滤
        page = await dao.list_by_workspace(
            "ws1", status="pending", cursor=None, limit=50
        )
        assert {p.id for p in page.items} == {"pend-0", "pend-1", "pend-2"}
        # ws2 只见自己
        page = await dao.list_by_workspace(
            "ws2", status=None, cursor=None, limit=50
        )
        assert [p.id for p in page.items] == ["p-ws2"]

        # 分页无重无漏（pending 3 条，limit=2）
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            p: Page[ProposalRow] = await dao.list_by_workspace(
                "ws1", status="pending", cursor=cursor, limit=2
            )
            seen.extend(x.id for x in p.items)
            cursor = p.next_cursor
            if cursor is None:
                break
        assert sorted(seen) == ["pend-0", "pend-1", "pend-2"]
        assert len(seen) == len(set(seen))


# ---------- ConfigDAO ----------


class TestConfigDAO:
    async def test_get_bootstrap_row(self, db):
        # 001 DDL 内置引导行：两列均为 '{}'（未初始化形态）
        got = await ConfigDAO(db).get()
        assert got.id == 1
        assert got.model_dict() == {}
        assert got.runtime_dict() == {}

    async def test_updates_do_not_overwrite_each_other(self, db):
        dao = ConfigDAO(db)
        model_cfg = {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"}
        runtime_cfg = {"llm_concurrency": 8, "batch_size": 3}
        await dao.update_model(model_cfg)
        await dao.update_runtime(runtime_cfg)

        got = await dao.get()
        assert got.model_dict() == model_cfg
        assert got.runtime_dict() == runtime_cfg

        # 二次更新 model 不影响 runtime
        await dao.update_model({"model": "deepseek-reasoner"})
        got = await dao.get()
        assert got.model_dict() == {"model": "deepseek-reasoner"}
        assert got.runtime_dict() == runtime_cfg

    async def test_missing_row_raises(self, db):
        await db.aexecute("DELETE FROM config WHERE id = 1")
        dao = ConfigDAO(db)
        with pytest.raises(NotFoundError):
            await dao.get()
        with pytest.raises(NotFoundError):
            await dao.update_model({"a": 1})
        with pytest.raises(NotFoundError):
            await dao.update_runtime({"a": 1})
