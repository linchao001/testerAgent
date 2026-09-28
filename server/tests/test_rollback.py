"""WP-24 回退协议（dd §11.2 §18.3 §2.9）。

验收口径（WBS #24）：
- 场景 3a：改 story 摘要 → 下游全部 unaffected → 继承零 LLM；
- 场景 3b：删 story → 其下 point/case 精确 obsolete，其余不动；
- 状态守卫（仅 waiting_confirm/waiting_input/completed 可回退）；
- 版本冲突（expected_version 不匹配 → VERSION_CONFLICT）；
- BEGIN IMMEDIATE 事务原子性（异常回滚）。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.domain import (
    LinkPlan,
    LinkRef,
    PointPlan,
    StoryRef,
    TestPoint as PointModel,
)
from tester_agent.errors import TaskStateConflict, VersionConflict
from tester_agent.graph.constants import (
    STAGE_CASE_GENERATE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from tester_agent.graph.registry import CASE_DESIGNER, GraphRegistry
from tester_agent.graph.rollback import RollbackIn, analyze_impact, rollback
from tester_agent.runtime.context import AppContext, DAOs, TaskContext
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ArtifactDAO,
    ArtifactRow,
    CaseRow,
    ConfigDAO,
    MessageDAO,
    TaskDAO,
    TaskRow,
    TestcaseDAO,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

from fakes import FakeLLM, FakeReMeReader

WS, CONV, TASK, RUN = "ws1", "conv1", "task1", "run-1"


def _link_plan_v1() -> LinkPlan:
    return LinkPlan(
        links=[
            LinkRef(link_id="L1", title="订单链路", summary="下单到履约",
                    hit=True, entry_id="l1", confidence=0.9, story_ids=["S1", "S2"]),
        ],
        stories=[
            StoryRef(story_id="S1", link_id="L1", title="下单故事",
                     summary="购物车结算下单", hit=True, entry_id="s1",
                     confidence=0.8, rationale="r", related_clause_ids=["h2-1"]),
            StoryRef(story_id="S2", link_id="L1", title="支付故事",
                     summary="收银台与回调", hit=True, entry_id="s2",
                     confidence=0.88, rationale="r", related_clause_ids=["h2-2"]),
        ],
        new_suggestions=[],
    )


def _point_plan_v1() -> PointPlan:
    return PointPlan(points=[
        PointModel(point_id="pt-1-1", story_id="S1", title="正常下单",
                  angle="正常", method="场景法", clause_ids=["h2-1"],
                  source_entry_ids=["b1"], priority="P0"),
        PointModel(point_id="pt-2-1", story_id="S2", title="支付回调",
                  angle="正常", method="状态迁移", clause_ids=["h2-2"],
                  source_entry_ids=["b2"], priority="P1"),
    ])


# ---------- 夹具 ----------


@pytest.fixture()
async def rollback_ctx(tmp_path):
    """构造回退测试上下文：db + workspace/conversation/task + 全链路 artifacts。"""
    db_path = tmp_path / "app.db"
    run_migrations(db_path)
    db = Database(db_path)
    await WorkspaceDAO(db).create(
        WorkspaceRow.create(id=WS, name="w", kb_config={"kb_id": "KB", "options": {}})
    )
    await db.aexecute(
        "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (CONV, WS, "t", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
    )
    await TaskDAO(db).create(
        TaskRow.create(id=TASK, conversation_id=CONV, workspace_id=WS,
                       status="waiting_confirm", current_stage=STAGE_LINK_IDENTIFY,
                       langgraph_thread_id="th-1", graph_run_id=RUN)
    )
    store = FileStore(tmp_path / "data")
    app = AppContext(db=db, file_store=store, llm=FakeLLM([]),
                     reme_factory=None, config=ConfigDAO(db))
    task = await TaskDAO(db).get(TASK)
    daos = DAOs(
        task=TaskDAO(db), message=MessageDAO(db),
        artifact=ArtifactDAO(db), testcase=TestcaseDAO(db),
    )
    ctx = TaskContext(
        app=app, task=task, run_id=RUN, files=store,
        reader=FakeReMeReader([]), snapshot_level="off",
        mirror=None, daos=daos,
    )
    yield ctx, db
    db.close()


async def _seed_chain(db, *, link_payload=None, point_payload=None,
                      case_payload=None, cases=None):
    """播种 link_identify/point_write/case_generate active artifacts + 可选用例。"""
    if link_payload is not None:
        await ArtifactDAO(db).put(ArtifactRow.create(
            id="art-link", task_id=TASK, stage=STAGE_LINK_IDENTIFY,
            graph_run_id=RUN, stage_version=1, payload=link_payload,
            origin="system", status="active", confirmed_by=None,
        ))
    if point_payload is not None:
        await ArtifactDAO(db).put(ArtifactRow.create(
            id="art-point", task_id=TASK, stage=STAGE_POINT_WRITE,
            graph_run_id=RUN, stage_version=1, payload=point_payload,
            origin="system", status="active", confirmed_by=None,
        ))
    if case_payload is not None:
        await ArtifactDAO(db).put(ArtifactRow.create(
            id="art-case", task_id=TASK, stage=STAGE_CASE_GENERATE,
            graph_run_id=RUN, stage_version=1, payload=case_payload,
            origin="system", status="active", confirmed_by=None,
            progress=[{"batch_id": "b0", "node": STAGE_CASE_GENERATE,
                       "unit_ids": [], "status": "done", "idem": "",
                       "result_ids": [c["id"] for c in (cases or [])]}],
        ))
    if cases:
        rows = [CaseRow(
            id=c["id"], task_id=TASK, point_id=c["point_id"],
            stage_version=1, batch_id="b0",
            file_path=f"cases/{c['id']}.md", content_hash="h",
            title=c.get("title", "t"), status="active", review_status="pending",
            created_at="2026-09-26T00:00:00.000Z",
            updated_at="2026-09-26T00:00:00.000Z",
        ) for c in cases]
        await TestcaseDAO(db).put_batch(rows)


# ---------- ① analyze_impact 结构化 diff ----------


def test_analyze_impact_summary_only_unaffected():
    """story 仅 summary 改动 → 无 affected，下游全部继承。"""
    old = _link_plan_v1()
    new = old.model_copy(deep=True)
    new.stories[1].summary = "新摘要文案"
    impact = analyze_impact(STAGE_LINK_IDENTIFY, old.model_dump(), new.model_dump())
    assert impact.target_stage == STAGE_LINK_IDENTIFY
    assert len(impact.story_changes) == 1
    assert impact.story_changes[0].id == "S2"
    assert "summary" in impact.story_changes[0].fields_changed
    # 仅 summary → unaffected
    assert not impact.downstream[0].affected_ids  # point_write
    assert not impact.downstream[1].affected_ids  # case_generate
    assert "无结构变更" in impact.summary


def test_analyze_impact_story_removed_affected():
    """删除 story → 该 story affected，下游需重算。"""
    old = _link_plan_v1()
    new = old.model_copy(deep=True)
    new.stories = [s for s in new.stories if s.story_id != "S2"]
    new.links[0].story_ids = ["S1"]
    impact = analyze_impact(STAGE_LINK_IDENTIFY, old.model_dump(), new.model_dump())
    assert "S2" in impact.downstream[0].affected_ids
    assert "S2" in impact.downstream[1].affected_ids
    assert "1 条 story" in impact.summary


def test_analyze_impact_link_changed_affects_stories():
    """link 结构变化 → 其下全部 story affected。"""
    old = _link_plan_v1()
    new = old.model_copy(deep=True)
    new.links[0].title = "改了链路标题"
    impact = analyze_impact(STAGE_LINK_IDENTIFY, old.model_dump(), new.model_dump())
    # L1 下的 S1、S2 都受影响
    assert set(impact.downstream[0].affected_ids) == {"S1", "S2"}


def test_analyze_impact_point_clause_change_affected():
    """point_write 回退：point 的 clause_ids 变化 → affected。"""
    old = _point_plan_v1()
    new = old.model_copy(deep=True)
    new.points[0].clause_ids = ["h2-1", "h2-3"]
    impact = analyze_impact(STAGE_POINT_WRITE, old.model_dump(), new.model_dump())
    assert "pt-1-1" in impact.affected_point_ids
    assert "pt-2-1" not in impact.affected_point_ids


def test_analyze_impact_point_priority_only_unaffected():
    """point_write 回退：仅 priority 变化 → unaffected。"""
    old = _point_plan_v1()
    new = old.model_copy(deep=True)
    new.points[0].priority = "P2"
    impact = analyze_impact(STAGE_POINT_WRITE, old.model_dump(), new.model_dump())
    assert not impact.affected_point_ids
    assert "无结构变更" in impact.summary


# ---------- ② TaskDAO.run_seq ----------


def test_run_seq_first_run():
    assert TaskDAO.run_seq("th-1") == 1


def test_run_seq_incremental():
    assert TaskDAO.run_seq("th-1::run1") == 2
    assert TaskDAO.run_seq("th-1::run1::run2") == 3


# ---------- ③ ArtifactDAO 继承 / 作废 ----------


@pytest.mark.asyncio()
async def test_inherit_to_run_reassigns_and_marks(rollback_ctx):
    ctx, db = rollback_ctx
    await _seed_chain(db, link_payload=_link_plan_v1().model_dump())
    art = await ArtifactDAO(db).get_active(TASK, STAGE_LINK_IDENTIFY)
    old_run = art.graph_run_id
    await ArtifactDAO(db).inherit_to_run(art.id, "run-new", old_run)
    updated = await ArtifactDAO(db).get(art.id)
    assert updated.graph_run_id == "run-new"
    assert updated.status == "active"
    assert updated.payload_dict()["inherited_from_run"] == old_run


@pytest.mark.asyncio()
async def test_superseded_and_obsolete_inherits_unaffected(rollback_ctx):
    from tester_agent.domain import StageImpact
    ctx, db = rollback_ctx
    await _seed_chain(db, point_payload=_point_plan_v1().model_dump(),
                      case_payload={"case_count": 2})
    # point_write: 无 affected → inherit；case_generate: 无 affected → inherit
    downstream = [
        StageImpact(stage=STAGE_POINT_WRITE, affected_ids=[]),
        StageImpact(stage=STAGE_CASE_GENERATE, affected_ids=[]),
    ]
    actions = await ArtifactDAO(db).superseded_and_obsolete(
        TASK, from_stage=STAGE_LINK_IDENTIFY, downstream=downstream, new_run="run-2"
    )
    assert actions[STAGE_POINT_WRITE] == "inherited"
    assert actions[STAGE_CASE_GENERATE] == "inherited"
    pw = await ArtifactDAO(db).get_active(TASK, STAGE_POINT_WRITE)
    assert pw.graph_run_id == "run-2"
    assert pw.status == "active"


@pytest.mark.asyncio()
async def test_superseded_and_obsolete_supersedes_affected(rollback_ctx):
    from tester_agent.domain import StageImpact
    ctx, db = rollback_ctx
    await _seed_chain(db, point_payload=_point_plan_v1().model_dump())
    downstream = [
        StageImpact(stage=STAGE_POINT_WRITE, affected_ids=["S2"]),
    ]
    actions = await ArtifactDAO(db).superseded_and_obsolete(
        TASK, from_stage=STAGE_LINK_IDENTIFY, downstream=downstream, new_run="run-2"
    )
    assert actions[STAGE_POINT_WRITE] == "superseded"
    # active 产物应不存在（已 superseded）
    assert await ArtifactDAO(db).get_active(TASK, STAGE_POINT_WRITE) is None


# ---------- ④ TestcaseDAO.mark_obsolete_by_points ----------


@pytest.mark.asyncio()
async def test_mark_obsolete_by_points_precise(rollback_ctx):
    ctx, db = rollback_ctx
    await _seed_chain(db, cases=[
        {"id": "c1", "point_id": "pt-1-1"},
        {"id": "c2", "point_id": "pt-2-1"},
    ])
    n = await TestcaseDAO(db).mark_obsolete_by_points(TASK, ["pt-2-1"])
    assert n == 1
    c1 = await TestcaseDAO(db).get("c1")
    c2 = await TestcaseDAO(db).get("c2")
    assert c1.status == "active"
    assert c2.status == "obsolete"


# ---------- ⑤ rollback 主协议 ----------


@pytest.mark.asyncio()
async def test_rollback_summary_change_full_inheritance(rollback_ctx):
    """场景 3a：改 story 摘要 → 下游全部继承，零 LLM（无图执行也不调 LLM）。"""
    ctx, db = rollback_ctx
    link_v1 = _link_plan_v1()
    point_v1 = _point_plan_v1()
    await _seed_chain(
        db,
        link_payload=link_v1.model_dump(),
        point_payload=point_v1.model_dump(),
        case_payload={"case_count": 2},
        cases=[{"id": "c1", "point_id": "pt-1-1"},
               {"id": "c2", "point_id": "pt-2-1"}],
    )
    # 修订：仅改 S2 summary
    revised = link_v1.model_copy(deep=True)
    revised.stories[1].summary = "更新后的摘要"

    impact = await rollback(
        ctx, TASK,
        RollbackIn(artifact_id="art-link", expected_version=1,
                   revised_artifact=revised.model_dump()),
    )
    # 影响面：无结构变更
    assert not impact.downstream[0].affected_ids
    assert not impact.affected_points()

    # link_identify：v1 superseded，v2(user_revised) active 且挂新 run
    link_active = await ArtifactDAO(db).get_active(TASK, STAGE_LINK_IDENTIFY)
    assert link_active.stage_version == 2
    assert link_active.origin == "user_revised"
    assert link_active.confirmed_by == "user"
    assert link_active.payload_dict()["stories"][1]["summary"] == "更新后的摘要"

    # point_write / case_generate：继承（graph_run_id 改挂新 run，status 仍 active）
    pw = await ArtifactDAO(db).get_active(TASK, STAGE_POINT_WRITE)
    assert pw.status == "active"
    assert pw.graph_run_id != RUN
    assert pw.payload_dict()["inherited_from_run"] == RUN

    cg = await ArtifactDAO(db).get_active(TASK, STAGE_CASE_GENERATE)
    assert cg.status == "active"
    assert cg.graph_run_id == pw.graph_run_id

    # 用例：全部保持 active（无 affected point）
    c1 = await TestcaseDAO(db).get("c1")
    c2 = await TestcaseDAO(db).get("c2")
    assert c1.status == "active" and c2.status == "active"

    # task：新 run + 派生 thread + running
    task = await TaskDAO(db).get(TASK)
    assert task.status == "running"
    assert task.graph_run_id != RUN
    assert task.langgraph_thread_id == "th-1::run1"
    assert task.current_stage == STAGE_LINK_IDENTIFY


@pytest.mark.asyncio()
async def test_rollback_story_deletion_precise_obsolete(rollback_ctx):
    """场景 3b：删 story → 其下 point/case 精确 obsolete，其余继承。"""
    ctx, db = rollback_ctx
    link_v1 = _link_plan_v1()
    point_v1 = _point_plan_v1()
    await _seed_chain(
        db,
        link_payload=link_v1.model_dump(),
        point_payload=point_v1.model_dump(),
        case_payload={"case_count": 2},
        cases=[{"id": "c1", "point_id": "pt-1-1"},
               {"id": "c2", "point_id": "pt-2-1"}],
    )
    # 修订：删除 S2
    revised = link_v1.model_copy(deep=True)
    revised.stories = [s for s in revised.stories if s.story_id != "S2"]
    revised.links[0].story_ids = ["S1"]

    impact = await rollback(
        ctx, TASK,
        RollbackIn(artifact_id="art-link", expected_version=1,
                   revised_artifact=revised.model_dump()),
    )
    # S2 affected
    assert "S2" in impact.downstream[0].affected_ids
    assert "pt-2-1" in impact.affected_points()

    # point_write：superseded（存在受影响 story）
    assert await ArtifactDAO(db).get_active(TASK, STAGE_POINT_WRITE) is None

    # case_generate：同样 superseded（affected 非空）
    assert await ArtifactDAO(db).get_active(TASK, STAGE_CASE_GENERATE) is None

    # 用例：pt-2-1（S2 的点）→ obsolete；pt-1-1 → active
    c1 = await TestcaseDAO(db).get("c1")
    c2 = await TestcaseDAO(db).get("c2")
    assert c1.status == "active"
    assert c2.status == "obsolete"

    # link_identify v2 active
    link_active = await ArtifactDAO(db).get_active(TASK, STAGE_LINK_IDENTIFY)
    assert link_active.stage_version == 2
    assert len(link_active.payload_dict()["stories"]) == 1


@pytest.mark.asyncio()
async def test_rollback_state_guard_rejects_running(rollback_ctx):
    """running 状态不允许回退 → TASK_STATE_CONFLICT。"""
    ctx, db = rollback_ctx
    await _seed_chain(db, link_payload=_link_plan_v1().model_dump())
    await TaskDAO(db).update_status(TASK, status="running")
    with pytest.raises(TaskStateConflict):
        await rollback(
            ctx, TASK,
            RollbackIn(artifact_id="art-link", expected_version=1,
                       revised_artifact=_link_plan_v1().model_dump()),
        )


@pytest.mark.asyncio()
async def test_rollback_version_conflict(rollback_ctx):
    """expected_version 不匹配 → VERSION_CONFLICT，不落账。"""
    ctx, db = rollback_ctx
    await _seed_chain(db, link_payload=_link_plan_v1().model_dump())
    with pytest.raises(VersionConflict):
        await rollback(
            ctx, TASK,
            RollbackIn(artifact_id="art-link", expected_version=99,
                       revised_artifact=_link_plan_v1().model_dump()),
        )
    # 不落账：link v1 仍 active
    link = await ArtifactDAO(db).get_active(TASK, STAGE_LINK_IDENTIFY)
    assert link.stage_version == 1


@pytest.mark.asyncio()
async def test_rollback_no_revision_reruns_target(rollback_ctx):
    """无 revised_artifact：不写新版本，目标阶段重跑（下游 unaffected 继承）。"""
    ctx, db = rollback_ctx
    link_v1 = _link_plan_v1()
    await _seed_chain(
        db,
        link_payload=link_v1.model_dump(),
        point_payload=_point_plan_v1().model_dump(),
    )
    impact = await rollback(
        ctx, TASK,
        RollbackIn(artifact_id="art-link", expected_version=1),
    )
    # old==new → 无 affected
    assert not impact.downstream[0].affected_ids
    # link v1 仍 active（无新版本）
    link = await ArtifactDAO(db).get_active(TASK, STAGE_LINK_IDENTIFY)
    assert link.stage_version == 1
    # point_write 继承
    pw = await ArtifactDAO(db).get_active(TASK, STAGE_POINT_WRITE)
    assert pw.graph_run_id != RUN


@pytest.mark.asyncio()
async def test_rollback_atomicity_on_dao_error(rollback_ctx):
    """BEGIN IMMEDIATE：落账中途异常 → 全部回滚（无新 run、无 v2）。"""
    ctx, db = rollback_ctx
    link_v1 = _link_plan_v1()
    await _seed_chain(db, link_payload=link_v1.model_dump())

    # 篡改 artifact_id 指向不存在的产物 → get 抛 NotFound → 事务回滚
    with pytest.raises(Exception):
        await rollback(
            ctx, TASK,
            RollbackIn(artifact_id="nonexistent", expected_version=1,
                       revised_artifact=link_v1.model_dump()),
        )
    task = await TaskDAO(db).get(TASK)
    assert task.graph_run_id == RUN  # 未切新 run
    link = await ArtifactDAO(db).get_active(TASK, STAGE_LINK_IDENTIFY)
    assert link.stage_version == 1  # 无 v2


# ---------- ⑥ start_run_from_plan 派生 thread ----------


@pytest.mark.asyncio()
async def test_start_run_from_plan_forks_to_dispatch(tmp_path):
    """回退派生 thread：as_node=plan → next=dispatch，入口 state 已注入。"""
    from tester_agent.domain import AgentPlan, PlanStep, PlanStepKind
    from tester_agent.graph.control.graph import build_control_graph

    conn = await aiosqlite.connect(
        str(tmp_path / "ckpt.db"), check_same_thread=False
    )
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    graph = build_control_graph(saver)
    registry = GraphRegistry.from_graph(CASE_DESIGNER, graph)

    plan = AgentPlan(
        plan_id="p-rb",
        version=1,
        goal="rollback",
        steps=[
            PlanStep(
                step_id="s1",
                kind=PlanStepKind.INTAKE_PARSE,
                goal="done",
                status="done",
            ),
            PlanStep(
                step_id="s2",
                kind=PlanStepKind.POINT_DESIGN,
                goal="rerun",
                status="pending",
                requires_confirm=True,
            ),
        ],
        status="active",
    )
    await registry.start_run_from_plan(
        "th-1::run1",
        entry_state={
            "task_id": "t1",
            "graph_run_id": "r2",
            "workspace_id": "w1",
            "agent_plan": plan.model_dump(),
            "link_plan": {"x": 1},
            "human_gates": {"link": True, "point": True, "review": False},
        },
    )
    cfg = {"configurable": {"thread_id": "th-1::run1"}}
    state = await graph.aget_state(cfg)
    assert state.next == ("dispatch",)
    assert state.values["link_plan"] == {"x": 1}
    assert state.values["agent_plan"]["steps"][1]["status"] == "pending"

    await conn.close()
