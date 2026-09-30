"""WP-32 Task 16 / 场景 13：长任务上下文收敛与可重放（spec §10.2）。

模拟口径（确定性纯 store/assembler/T1，不入真实 LLM）：
- 200 轮 chat（conversation owner）；
- 12-step 控制环（replan×2、repair×3、review×2）+ 100 条用例批次；
- 六项断言 + 编写窗口零 design + 批次 O(1) tokens。
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langchain_core.messages import SystemMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.context.assembler import assemble
from tester_agent.context.budget import PROFILES
from tester_agent.context.journal import JournalAction, JournalRecord
from tester_agent.context.models import (
    ContextEntry,
    ContextPartition,
    EntryKind,
    EntryStatus,
    Phase,
    ProfileName,
    ScopeLevel,
)
from tester_agent.context.rebuild import rebuild_store
from tester_agent.context.scopes import scope
from tester_agent.context.store import ContextStore
from tester_agent.context.tokens import estimate_tokens
from tester_agent.domain import AgentPlan, PlanStep, PlanStepKind
from tester_agent.graph.control.context_t1 import (
    on_await_human_confirmed,
    on_plan_updated,
    on_reflect_decision,
    on_step_completed,
)
from tester_agent.runtime.context import DAOs
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ArtifactDAO,
    ArtifactRow,
    ContextJournalDAO,
    ContextJournalRow,
    MessageDAO,
    TaskDAO,
    TaskRow,
    WorkspaceDAO,
    WorkspaceRow,
)

WS, CONV, TASK = "ws-s13", "conv-s13", "task-s13"
MODEL_WINDOW = 128_000
P0 = [SystemMessage(content="【方法论】场景13固定引导")]
_EPOCH = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[JournalRecord] = []

    async def record(self, records: list[JournalRecord]) -> None:
        self.records.extend(records)


def _ts(i: int) -> str:
    return (_EPOCH + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _msg_hash(messages) -> str:
    blob = "\n".join(f"{type(m).__name__}:{m.content}" for m in messages)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _active_p2(store: ContextStore) -> int:
    return sum(
        1
        for e in store.entries(partition=ContextPartition.P2)
        if e.status is EntryStatus.ACTIVE
    )


def _assembled_tokens(report) -> int:
    return sum(report.per_partition_tokens.values())


def _token_bound_ok(profile: ProfileName, report, store: ContextStore) -> bool:
    budget = PROFILES[profile]
    pinned = sum(
        e.tokens_est
        for e in store.entries()
        if e.pinned and e.status is EntryStatus.ACTIVE
    )
    ceiling = budget.total() + pinned + max(1, budget.total() // 10)
    return _assembled_tokens(report) <= ceiling


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "s13.db"
    run_migrations(path)
    database = Database(path)
    yield database
    database.close()


async def _seed_workspace(db: Database) -> None:
    await WorkspaceDAO(db).create(
        WorkspaceRow.create(
            id=WS,
            name="s13",
            kb_config={
                "kb_id": "kb-s13",
                "knowledge_bases_dir": "",
                "knowledge_dir": "knowledge",
                "create_knowledge_base": False,
                "options": {},
            },
        )
    )
    await db.aexecute(
        "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        (CONV, WS, "s13", _ts(0), _ts(0)),
    )
    await TaskDAO(db).create(
        TaskRow.create(
            id=TASK,
            conversation_id=CONV,
            workspace_id=WS,
            status="running",
            current_stage="coverage_design",
            langgraph_thread_id="th-s13",
            graph_run_id="run-s13",
            snapshot_level="meta",
        )
    )


@pytest.mark.asyncio
async def test_scenario_13_context_convergence_and_replay(db: Database):
    await _seed_workspace(db)
    journal_dao = ContextJournalDAO(db)
    sink = RecordingSink()

    async def _audit_counts() -> dict[str, int]:
        out = {}
        for table in (
            "retrieval_trace",
            "context_snapshot",
            "stage_artifact",
            "message",
            "testcase",
        ):
            row = await db.aquery_one(f"SELECT COUNT(*) AS c FROM {table}")
            out[table] = int(row["c"])
        return out

    before_audit = await _audit_counts()

    # ---------- A. 200 轮 chat ----------
    chat_store = ContextStore(
        owner_type="conversation",
        owner_id=CONV,
        workspace_id=WS,
        journal=sink,
    )
    await chat_store.set_goal("澄清订单需求", reason="scenario")
    p2_active_series: list[int] = []
    for turn in range(1, 201):
        for role, text in (
            ("user", f"用户轮{turn}：" + "问" * 40),
            ("assistant", f"助手轮{turn}：" + "答" * 40),
        ):
            await chat_store.append(
                ContextEntry(
                    entry_id=f"chat:{turn}:{role}",
                    partition=ContextPartition.P2,
                    entry_kind=EntryKind.CHAT_TURN,
                    content=text,
                    digest=text[:40],
                    tokens_est=estimate_tokens(text),
                    role=role,
                    turn_seq=turn,
                    created_at=_ts(turn * 2 + (0 if role == "user" else 1)),
                )
            )
        if turn % 20 == 0 or turn == 200:
            async with scope(phase=Phase.SHARED, turn_seq=turn):
                result = await assemble(
                    chat_store,
                    ProfileName.CHAT,
                    p0_messages=P0,
                    model_window=MODEL_WINDOW,
                    recent_turns=6,
                )
            assert _token_bound_ok(ProfileName.CHAT, result.report, chat_store)
            p2_active_series.append(_active_p2(chat_store))

    # 收敛：CHAT 硬窗口后 P2 活跃数 O(1)（≤ K 轮×2 + goal pinned），不随总轮次增长
    recent_turns = 6
    assert p2_active_series and max(p2_active_series) <= recent_turns * 2 + 2, p2_active_series
    demote_recs = [r for r in sink.records if r.action == JournalAction.DEMOTE]
    assert demote_recs
    assert all(r.reason for r in demote_recs)
    assert any(r.reason == "chat_window" for r in demote_recs)

    # ---------- B. 12-step 任务 + 批次 ----------
    task_sink = RecordingSink()
    task_store = ContextStore(
        owner_type="task",
        owner_id=TASK,
        workspace_id=WS,
        journal=task_sink,
    )
    await task_store.set_goal("生成用例并覆盖", reason="scenario")

    for i in range(6):
        await task_store.append(
            ContextEntry(
                entry_id=f"kb:s{i}",
                partition=ContextPartition.P1,
                entry_kind=EntryKind.KB_BLOCK,
                content="知识块" + "K" * 200,
                digest=f"kb{i}",
                tokens_est=estimate_tokens("知识块" + "K" * 200),
                step_id=f"s{i}",
                step_seq=i,
                phase=Phase.DESIGN if i < 4 else Phase.WRITE,
                created_at=_ts(1000 + i),
            )
        )

    # 12 steps：含 replan×2 / repair×3 / review×2
    step_specs: list[tuple[str, PlanStepKind]] = [
        ("s0", PlanStepKind.INTAKE_PARSE),
        ("s1", PlanStepKind.COVERAGE_DESIGN),
        ("s2", PlanStepKind.POINT_DESIGN),
        ("s3", PlanStepKind.CASE_GENERATE),
        ("s4", PlanStepKind.REPAIR),
        ("s5", PlanStepKind.REVIEW_COVERAGE),
        ("s6", PlanStepKind.CASE_GENERATE),
        ("s7", PlanStepKind.REPAIR),
        ("s8", PlanStepKind.REVIEW_QUALITY),
        ("s9", PlanStepKind.COVERAGE_DESIGN),
        ("s10", PlanStepKind.REPAIR),
        ("s11", PlanStepKind.CASE_GENERATE),
    ]
    stage_ver: dict[str, int] = {}
    plan = AgentPlan(plan_id="p-s13", version=1, goal="生成用例并覆盖", steps=[])
    await on_plan_updated(task_store, plan=plan, plan_artifact_id="plan-v1")
    repair_count = 0
    replan_count = 0
    review_count = 0
    p1_active_series: list[int] = []

    def _active_p1() -> int:
        return sum(
            1
            for e in task_store.entries(partition=ContextPartition.P1)
            if e.status is EntryStatus.ACTIVE
        )

    for seq, (sid, kind) in enumerate(step_specs):
        step = PlanStep(
            step_id=sid, kind=kind, goal=f"g-{sid}", status="done", output_ref=f"art-{sid}"
        )
        plan.steps.append(step)
        akind = kind.value
        stage_ver[akind] = stage_ver.get(akind, 0) + 1
        payload = {"title": f"产物{sid}", "goal": plan.goal, "items": [{"id": sid}]}
        await ArtifactDAO(db).put(
            ArtifactRow.create(
                id=f"art-{sid}",
                task_id=TASK,
                stage=akind,
                graph_run_id="run-s13",
                stage_version=stage_ver[akind],
                payload=payload,
                origin="system",
                status="active",
                confirmed_by=None,
                kind=akind,
            )
        )
        await on_step_completed(
            task_store,
            step=step,
            step_seq=seq,
            artifact_id=f"art-{sid}",
            artifact_kind=akind,
            payload=payload,
            step_window=1,
        )
        p1_active_series.append(_active_p1())
        if kind is PlanStepKind.COVERAGE_DESIGN:
            await on_await_human_confirmed(
                task_store,
                artifact_id=f"art-{sid}",
                artifact_kind=akind,
                payload=payload,
            )
            await db.aexecute(
                "UPDATE stage_artifact SET confirmed_by = ? WHERE id = ?",
                ("user", f"art-{sid}"),
            )
        if kind is PlanStepKind.REPAIR:
            repair_count += 1
            await on_reflect_decision(
                task_store, step=step, decision="repair", reflection_count=repair_count
            )
        if kind in (PlanStepKind.REVIEW_COVERAGE, PlanStepKind.REVIEW_QUALITY):
            review_count += 1
            await on_reflect_decision(
                task_store,
                step=step,
                decision="continue",
                reflection_count=100 + review_count,
            )
        # replan ×2：在第 2、8 步后换 plan
        if seq in (2, 8):
            replan_count += 1
            plan = AgentPlan(
                plan_id="p-s13",
                version=plan.version + 1,
                goal=f"生成用例并覆盖-v{plan.version + 1}",
                steps=list(plan.steps),
            )
            await on_plan_updated(
                task_store, plan=plan, plan_artifact_id=f"plan-v{plan.version}"
            )

    assert replan_count == 2
    assert repair_count == 3
    assert review_count == 2
    # 收敛（spec §10.2 #2）：step_window 淘汰使 P1 活跃数下降且 journal 可归因
    assert any(
        p1_active_series[i] < p1_active_series[i - 1]
        for i in range(1, len(p1_active_series))
    ), p1_active_series
    step_demotes = [
        r
        for r in task_sink.records
        if r.action == JournalAction.DEMOTE and r.reason == "step_window"
    ]
    assert step_demotes

    batch_id = "batch-s13"
    for i in range(1, 101):
        body = f"case{i}-marker " + ("步" * 120)
        await task_store.append(
            ContextEntry(
                entry_id=f"item:{batch_id}:p{i}",
                partition=ContextPartition.P2,
                entry_kind=EntryKind.ARTIFACT_DIGEST,
                content=body,
                digest=body[:40],
                tokens_est=estimate_tokens(body),
                scope_level=ScopeLevel.ITEM,
                phase=Phase.WRITE,
                batch_id=batch_id,
                item_key=f"{batch_id}:p{i}",
                step_seq=11,
                created_at=_ts(2000 + i),
            )
        )
    await task_store.append(
        ContextEntry(
            entry_id="design:draft-secret",
            partition=ContextPartition.P2,
            entry_kind=EntryKind.REFLECTION,
            content="设计期机密草稿不应出现在编写窗口",
            digest="design-secret",
            tokens_est=20,
            phase=Phase.DESIGN,
            step_seq=2,
            created_at=_ts(1999),
        )
    )

    async def _assemble_item(i: int):
        async with scope(
            phase=Phase.WRITE,
            batch_id=batch_id,
            item_key=f"{batch_id}:p{i}",
            step_seq=11,
        ):
            # O(1) 对照禁止 persist：否则第 1 条裁剪 demote 会污染第 100 条窗口（墓碑膨胀）
            return await assemble(
                task_store,
                ProfileName.CASE_ITEM,
                p0_messages=P0,
                model_window=MODEL_WINDOW,
                persist_evictions=False,
            )

    first = await _assemble_item(1)
    last = await _assemble_item(100)
    assert _token_bound_ok(ProfileName.CASE_ITEM, first.report, task_store)
    assert _token_bound_ok(ProfileName.CASE_ITEM, last.report, task_store)
    t1 = _assembled_tokens(first.report)
    t100 = _assembled_tokens(last.report)
    assert abs(t100 - t1) / max(t1, 1) < 0.1
    last_text = "\n".join(str(m.content) for m in last.messages)
    assert "case100-marker" in last_text
    assert "case99-marker" not in last_text
    assert "设计期机密草稿" not in last_text
    assert "design:draft-secret" not in last.report.included

    async with scope(phase=Phase.WRITE, step_seq=11):
        exec_r = await assemble(
            task_store,
            ProfileName.EXECUTE,
            p0_messages=P0,
            model_window=MODEL_WINDOW,
            goal=task_store.goal_text(),
        )
    assert _token_bound_ok(ProfileName.EXECUTE, exec_r.report, task_store)
    pinned_ids = {
        e.entry_id
        for e in task_store.entries()
        if e.pinned and e.status is EntryStatus.ACTIVE
    }
    assert any(eid.startswith("goal:") or eid.startswith("artifact:plan") for eid in pinned_ids)
    # 确认过的 coverage_design digest 仍 pinned
    assert any(
        e.pinned and e.entry_id.startswith("artifact:art-s")
        for e in task_store.entries()
        if e.status is EntryStatus.ACTIVE
    )

    await journal_dao.put_batch(
        [ContextJournalRow.from_record(r) for r in task_sink.records]
    )

    # ---------- C. 重放等价（同 rebuild 两次组装字节一致） ----------
    daos = DAOs(
        task=TaskDAO(db),
        message=MessageDAO(db),
        artifact=ArtifactDAO(db),
        journal=journal_dao,
    )
    rebuilt = await rebuild_store(
        owner_type="task",
        owner_id=TASK,
        workspace_id=WS,
        daos=daos,
        journal_sink=None,
    )
    async with scope(phase=Phase.WRITE, step_seq=11):
        replay_r = await assemble(
            rebuilt,
            ProfileName.EXECUTE,
            p0_messages=P0,
            model_window=MODEL_WINDOW,
            goal=rebuilt.goal_text() or task_store.goal_text(),
            persist_evictions=False,
        )
        replay_r2 = await assemble(
            rebuilt,
            ProfileName.EXECUTE,
            p0_messages=P0,
            model_window=MODEL_WINDOW,
            goal=rebuilt.goal_text() or task_store.goal_text(),
            persist_evictions=False,
        )
    assert _msg_hash(replay_r.messages) == _msg_hash(replay_r2.messages)
    assert replay_r.report.included == replay_r2.report.included

    # ---------- D. 审计完整（检索/用例/消息不被淘汰改写） ----------
    after_audit = await _audit_counts()
    for k in ("retrieval_trace", "context_snapshot", "testcase", "message"):
        assert after_audit[k] == before_audit[k]
    assert after_audit["stage_artifact"] >= before_audit["stage_artifact"]

    # ---------- E. 淘汰归因可查 ----------
    page = await journal_dao.list_by_owner(
        workspace_id=WS,
        owner_type="task",
        owner_id=TASK,
        cursor=None,
        limit=200,
        actions=[
            JournalAction.DEMOTE,
            JournalAction.EVICT,
            JournalAction.GOAL,
            JournalAction.BATCH_CLOSE,
            JournalAction.PIN,
        ],
    )
    assert page.items
    assert all(it.reason for it in page.items)

    await task_store.close_batch(batch_id)
    assert task_store.get(f"item:{batch_id}:p1").status is EntryStatus.EVICTED
