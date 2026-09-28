"""回退协议（dd §11.2 §18.3 §2.9）。

核心流程：

1. ``analyze_impact(stage, old_plan, new_plan)``：结构化 diff（不靠 LLM），
   判定每个 link/story/point 是 added/removed/affected/unaffected；
2. ``rollback(ctx, task_id, body)``：``BEGIN IMMEDIATE`` 单事务内完成
   落账——目标阶段旧版本 superseded + 用户修订版 v+1（user_revised）落库、
   下游产物按影响面 supersede 或 inherit（graph_run_id 改挂新 run）、
   受影响 point 的用例置 obsolete、task 切新 run/派生 thread；
3. 事务提交后 ``GraphRegistry.start_run_from_plan`` 启动新 thread
   （入口 = 修订后 AgentPlan + 继承 artifacts）。

影响面判定规则（dd §11.2）：
- link_identify 回退：story 删除/link 变更 → 其下 points 全部 affected；
  story 仅 summary 文案改动 → points unaffected（用例依据条款与知识而非摘要）；
  新增 story → 仅新增不影响存量。
- point_write 回退：point 删除/clause_ids 集合变化 → 关联 case affected；
  仅 priority/title 微调 → unaffected。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from ..domain import (
    AgentPlan,
    IdChange,
    ImpactAnalysis,
    LinkPlan,
    PlanStep,
    PlanStepKind,
    PointPlan,
    StageImpact,
)
from ..errors import AppError, TaskStateConflict, VersionConflict
from ..graph.constants import (
    STAGE_CASE_GENERATE,
    STAGE_LINK_IDENTIFY,
    STAGE_POINT_WRITE,
)
from ..store.models import ArtifactRow, MessageRow, TaskDAO
from ..tools.capabilities import STAGE_TO_KIND

if TYPE_CHECKING:
    from ..runtime.context import TaskContext

#: link_identify 回退时，story 仅这些字段变化视为 unaffected（用例依据条款与知识）
_LINK_STORY_UNAFFECTED_FIELDS = {"summary"}
#: point_write 回退时，point 仅这些字段变化视为 unaffected
_POINT_UNAFFECTED_FIELDS = {"priority", "title"}

# 回退目标阶段 → 控制环对应步 kind（其后步骤一律 pending 重跑）
_STAGE_KIND_ORDER: list[tuple[str, PlanStepKind]] = [
    ("intake", PlanStepKind.INTAKE_PARSE),
    (STAGE_LINK_IDENTIFY, PlanStepKind.COVERAGE_DESIGN),
    (STAGE_POINT_WRITE, PlanStepKind.POINT_DESIGN),
    (STAGE_CASE_GENERATE, PlanStepKind.CASE_GENERATE),
]

# ---------- 结构化 diff（dd §11.2 影响面判定规则） ----------


def _field_diff(old: dict, new: dict) -> list[str]:
    """返回值不同的字段名列表（忽略 None vs 缺省的等价）。"""
    diff: list[str] = []
    keys = set(old) | set(new)
    for k in sorted(keys):
        ov = old.get(k)
        nv = new.get(k)
        if ov != nv:
            diff.append(k)
    return diff


def _diff_stories(old: LinkPlan, new: LinkPlan) -> tuple[list[IdChange], set[str]]:
    """对比两个 LinkPlan 的 stories，返回 (变更列表, affected_story_ids)。"""
    old_by_id = {s.story_id: s for s in old.stories}
    new_by_id = {s.story_id: s for s in new.stories}
    changes: list[IdChange] = []
    affected: set[str] = set()

    for sid, ns in new_by_id.items():
        if sid not in old_by_id:
            changes.append(IdChange(id=sid, kind="story", fields_changed=["__added__"]))
            continue
        os_ = old_by_id[sid]
        diff = _field_diff(os_.model_dump(), ns.model_dump())
        if diff:
            changes.append(IdChange(id=sid, kind="story", fields_changed=diff))
            if not set(diff).issubset(_LINK_STORY_UNAFFECTED_FIELDS):
                affected.add(sid)

    for sid in old_by_id:
        if sid not in new_by_id:
            changes.append(IdChange(id=sid, kind="story", fields_changed=["__removed__"]))
            affected.add(sid)

    return changes, affected


def _diff_links(old: LinkPlan, new: LinkPlan) -> tuple[list[IdChange], set[str]]:
    """对比两个 LinkPlan 的 links；link 结构变化会波及其下全部 story。

    ``story_ids`` 是从 stories 列表派生的引用字段，仅因 story 增删而变化，
    不计入 link 结构变更（避免删一个 story 波及同 link 下其余 story）。
    """
    # story_ids 不计入结构比对
    STRUCTURAL_FIELDS = {"link_id", "title", "summary", "hit", "entry_id",
                         "entry_version", "confidence"}
    old_by_id = {l.link_id: l for l in old.links}
    new_by_id = {l.link_id: l for l in new.links}
    changes: list[IdChange] = []
    affected_links: set[str] = set()

    for lid, nl in new_by_id.items():
        if lid not in old_by_id:
            changes.append(IdChange(id=lid, kind="link", fields_changed=["__added__"]))
            continue
        ol = old_by_id[lid]
        diff = [f for f in _field_diff(ol.model_dump(), nl.model_dump())
                if f in STRUCTURAL_FIELDS]
        if diff:
            changes.append(IdChange(id=lid, kind="link", fields_changed=diff))
            affected_links.add(lid)

    for lid in old_by_id:
        if lid not in new_by_id:
            changes.append(IdChange(id=lid, kind="link", fields_changed=["__removed__"]))
            affected_links.add(lid)

    return changes, affected_links


def _diff_points(old: PointPlan, new: PointPlan) -> tuple[list[IdChange], set[str]]:
    """对比两个 PointPlan 的 points，返回 (变更列表, affected_point_ids)。"""
    old_by_id = {p.point_id: p for p in old.points}
    new_by_id = {p.point_id: p for p in new.points}
    changes: list[IdChange] = []
    affected: set[str] = set()

    for pid, np_ in new_by_id.items():
        if pid not in old_by_id:
            changes.append(IdChange(id=pid, kind="point", fields_changed=["__added__"]))
            continue
        op = old_by_id[pid]
        diff = _field_diff(op.model_dump(), np_.model_dump())
        if diff:
            changes.append(IdChange(id=pid, kind="point", fields_changed=diff))
            if not set(diff).issubset(_POINT_UNAFFECTED_FIELDS):
                affected.add(pid)

    for pid in old_by_id:
        if pid not in new_by_id:
            changes.append(IdChange(id=pid, kind="point", fields_changed=["__removed__"]))
            affected.add(pid)

    return changes, affected


def analyze_impact(stage: str, old_plan: dict, new_plan: dict) -> ImpactAnalysis:
    """结构化 diff 判定影响面（dd §11.2）。

    仅做目标阶段产物的字段级对比；下游 point/case 的精确归属由 rollback
    落账时读下游 artifact 解析（本函数只输出 story/point 粒度的 affected
    集合占位）。
    """
    if stage == STAGE_LINK_IDENTIFY:
        old = LinkPlan.model_validate(old_plan)
        new = LinkPlan.model_validate(new_plan)
        story_changes, affected_stories = _diff_stories(old, new)
        link_changes, affected_links = _diff_links(old, new)

        # link 结构变化波及其下全部 story
        link_to_stories: dict[str, list[str]] = {}
        for s in old.stories:
            link_to_stories.setdefault(s.link_id, []).append(s.story_id)
        for lid in affected_links:
            affected_stories.update(link_to_stories.get(lid, []))

        downstream = [
            StageImpact(
                stage=STAGE_POINT_WRITE,
                affected_ids=sorted(affected_stories),
            ),
            StageImpact(
                stage=STAGE_CASE_GENERATE,
                affected_ids=sorted(affected_stories),
            ),
        ]
        n_affected = len(affected_stories)
        return ImpactAnalysis(
            target_stage=stage,
            link_changes=link_changes,
            story_changes=story_changes,
            downstream=downstream,
            affected_point_ids=[],
            summary=(
                f"影响 {n_affected} 条 story（其下测试点与用例需重算）"
                if n_affected
                else "无结构变更，下游全部继承"
            ),
        )

    if stage == STAGE_POINT_WRITE:
        old = PointPlan.model_validate(old_plan)
        new = PointPlan.model_validate(new_plan)
        point_changes, affected_points = _diff_points(old, new)
        downstream = [
            StageImpact(
                stage=STAGE_CASE_GENERATE,
                affected_ids=sorted(affected_points),
            ),
        ]
        return ImpactAnalysis(
            target_stage=stage,
            point_changes=point_changes,
            downstream=downstream,
            affected_point_ids=sorted(affected_points),
            summary=(
                f"影响 {len(affected_points)} 条测试点（关联用例需重算）"
                if affected_points
                else "无结构变更，下游全部继承"
            ),
        )

    return ImpactAnalysis(target_stage=stage, summary="无下游影响")


# ---------- RollbackIn 入参 ----------


class RollbackIn:
    """回退入参（dd §11.2）。

    :param artifact_id: 目标阶段当前 active 产物 id（回退锚点）
    :param expected_version: 期望的 stage_version（乐观锁）
    :param revised_artifact: 用户修订后的产物 payload（None = 仅回退不修订）
    """

    def __init__(
        self,
        *,
        artifact_id: str,
        expected_version: int,
        revised_artifact: dict | None = None,
    ) -> None:
        self.artifact_id = artifact_id
        self.expected_version = int(expected_version)
        self.revised_artifact = revised_artifact


# ---------- 下游 affected point 解析 ----------


async def _resolve_affected_points(
    artifact_dao, task_id: str, impact: ImpactAnalysis
) -> list[str]:
    """link_identify 回退时，把 affected story_ids 解析为 affected point_ids。

    读 point_write active artifact 的 PointPlan，取 story_id 属于
    affected_stories 的 point_id。point_write 产物不存在时返回空。
    """
    pw = await artifact_dao.get_active(task_id, STAGE_POINT_WRITE)
    if pw is None:
        return []
    try:
        plan = PointPlan.model_validate(pw.payload_dict())
    except Exception:
        return []
    affected_stories = set()
    for si in impact.downstream:
        if si.stage == STAGE_POINT_WRITE:
            affected_stories.update(si.affected_ids)
    return [p.point_id for p in plan.points if p.story_id in affected_stories]


# ---------- 主入口 ----------


async def rollback(ctx: "TaskContext", task_id: str, body: RollbackIn) -> ImpactAnalysis:
    """执行回退协议（dd §11.2）：BEGIN IMMEDIATE 落账 → 派生 thread 起跑。

    返回 ImpactAnalysis 供 API 层回传前端做 ImpactPreview。
    """
    if ctx.daos is None or ctx.daos.artifact is None or ctx.daos.testcase is None:
        raise AppError(
            "rollback 缺少 DAOs（Runner 未注入 artifact/testcase DAO）",
            details={"node": "rollback"},
        )
    task_dao = ctx.daos.task
    artifact_dao = ctx.daos.artifact
    testcase_dao = ctx.daos.testcase
    message_dao = ctx.daos.message

    async with ctx.app.db.immediate_tx():
        # ① 锁任务行 + 状态机校验
        task = await task_dao.get_for_update(task_id)
        if task.status not in ("waiting_confirm", "waiting_input", "completed"):
            raise TaskStateConflict(
                f"当前状态不允许回退：{task.status}",
                details={
                    "current": task.status,
                    "allowed": ["waiting_confirm", "waiting_input", "completed"],
                },
            )

        # ② 取目标产物 + 版本校验
        target = await artifact_dao.get(body.artifact_id)
        if target.task_id != task_id:
            raise AppError(
                "产物不属于该任务",
                details={"artifact_id": body.artifact_id, "task_id": task_id},
            )
        if target.stage_version != body.expected_version:
            raise VersionConflict(
                f"产物版本冲突：期望 v{body.expected_version}，实际 v{target.stage_version}",
                details={"expected": body.expected_version, "actual": target.stage_version},
            )

        old_plan = target.payload_dict()
        new_plan = body.revised_artifact if body.revised_artifact is not None else old_plan
        impact = analyze_impact(target.stage, old_plan, new_plan)

        # ③ 解析下游 affected_point_ids
        if target.stage == STAGE_LINK_IDENTIFY:
            impact.affected_point_ids = await _resolve_affected_points(
                artifact_dao, task_id, impact
            )

        # ④ 生成新 run/thread（在落账前确定，供 inherit_to_run 使用）
        new_run = uuid.uuid4().hex
        new_thread = f"{task.langgraph_thread_id}::run{TaskDAO.run_seq(task.langgraph_thread_id)}"

        # ⑤ 下游产物落账：supersede / inherit
        actions = await artifact_dao.superseded_and_obsolete(
            task_id,
            from_stage=target.stage,
            downstream=impact.downstream,
            new_run=new_run,
        )

        # ⑥ 写入用户修订版本（若带修订）
        entry_plan = old_plan
        if body.revised_artifact is not None:
            v = await artifact_dao.next_version(task_id, target.stage)
            await artifact_dao.supersede(target.id)
            await artifact_dao.put(
                ArtifactRow.create(
                    id=uuid.uuid4().hex,
                    task_id=task_id,
                    stage=target.stage,
                    graph_run_id=new_run,
                    stage_version=v,
                    origin="user_revised",
                    status="active",
                    payload=new_plan,
                    confirmed_by="user",
                )
            )
            entry_plan = new_plan

        # ⑦ 受影响 point 的用例置 obsolete
        await testcase_dao.mark_obsolete_by_points(
            task_id, impact.affected_points()
        )

        # ⑧ task 切新 run/派生 thread + 状态 → running
        await task_dao.start_new_run(
            task_id, graph_run_id=new_run, thread_id=new_thread, stage=target.stage
        )
        await task_dao.update_status(task_id, status="running")

        # ⑨ 回退留痕消息
        await message_dao.put(
            MessageRow.create(
                id=uuid.uuid4().hex,
                conversation_id=task.conversation_id,
                role="system",
                kind="change_request",
                content=f"rollback to {target.stage}",
                task_id=task_id,
                ref_artifact_id=target.id,
                payload={
                    "target_stage": target.stage,
                    "old_version": target.stage_version,
                    "new_run": new_run,
                    "impact_summary": impact.summary,
                    "actions": actions,
                },
            )
        )

    # ⑩ 事务提交后：构建入口 state（AgentPlan + 继承产物）并初始化新 thread
    entry_state = await _build_entry_state(
        ctx, task, new_run, target.stage, entry_plan
    )
    if ctx.app.graphs is not None:
        await ctx.app.graphs.start_run_from_plan(
            new_thread, entry_state=entry_state
        )
    if ctx.emit is not None:
        await ctx.emit(
            "node_start",
            {"node": target.stage, "stage_version": None, "batch_id": None},
        )

    return impact


def _build_rollback_plan(target_stage: str, *, artifacts: dict[str, Any]) -> AgentPlan:
    """构造回退入口 AgentPlan：目标阶段之前 done，目标及之后 pending。"""
    target_kind = STAGE_TO_KIND.get(target_stage, PlanStepKind.COVERAGE_DESIGN)
    reached_target = False
    steps: list[PlanStep] = []
    for i, (stage, kind) in enumerate(_STAGE_KIND_ORDER):
        sid = f"s{i + 1}"
        if not reached_target and kind == target_kind:
            reached_target = True
        if reached_target:
            steps.append(
                PlanStep(
                    step_id=sid,
                    kind=kind,
                    goal=f"rollback:{stage}",
                    status="pending",
                    requires_confirm=kind
                    in (PlanStepKind.COVERAGE_DESIGN, PlanStepKind.POINT_DESIGN),
                )
            )
        else:
            # 上游步：挂已有 artifact 为 done
            art_id = None
            for aid, art in artifacts.items():
                if art.get("kind") in (kind.value, stage) or (
                    kind == PlanStepKind.INTAKE_PARSE and art.get("kind") == "clauses"
                ):
                    art_id = aid
                    break
            steps.append(
                PlanStep(
                    step_id=sid,
                    kind=kind,
                    goal=f"inherited:{stage}",
                    status="done",
                    output_ref=art_id,
                    requires_confirm=False,
                )
            )
    # 评审步：回退后一律 pending（需重新评审）
    for j, kind in enumerate(
        (
            PlanStepKind.REVIEW_COVERAGE,
            PlanStepKind.REVIEW_QUALITY,
            PlanStepKind.REVIEW_ADOPTION,
        ),
        start=len(steps) + 1,
    ):
        steps.append(
            PlanStep(
                step_id=f"s{j}",
                kind=kind,
                goal=kind.value,
                status="pending",
                requires_confirm=True,
            )
        )
    return AgentPlan(
        plan_id=f"plan-rb-{uuid.uuid4().hex[:10]}",
        version=1,
        goal="rollback resume",
        steps=steps,
        status="active",
    )


async def _build_entry_state(
    ctx: "TaskContext",
    task,
    new_run: str,
    target_stage: str,
    entry_plan: dict,
) -> dict:
    """构建回退新 thread 的入口 state（控制环 + 遗留字段镜像）。

    包含 AgentPlan（目标步起 pending）、artifacts、clauses，以及目标阶段
    之前各阶段的 active 产物（供下游能力节点消费）。目标阶段修订产物已在
    DB 落账，节点经 get_active 回放。
    """
    artifact_dao = ctx.daos.artifact if ctx.daos else None
    state: dict[str, Any] = {
        "task_id": task.id,
        "graph_run_id": new_run,
        "workspace_id": task.workspace_id,
        "human_gates": {"link": True, "point": True, "review": True},
        "artifacts": {},
        "plan_cursor": None,
        "reflection_log": [],
        "reflect_counts": {},
    }
    try:
        clauses = task.clauses_obj()
        if clauses:
            state["clauses"] = [c.model_dump() for c in clauses]
    except Exception:
        pass

    artifacts: dict[str, Any] = {}
    if artifact_dao is not None:
        stage_order = [
            STAGE_LINK_IDENTIFY,
            STAGE_POINT_WRITE,
            STAGE_CASE_GENERATE,
        ]
        state_key = {
            STAGE_LINK_IDENTIFY: "link_plan",
            STAGE_POINT_WRITE: "point_plan",
            STAGE_CASE_GENERATE: "case_batch",
        }
        for stage in stage_order:
            if stage == target_stage:
                break
            art = await artifact_dao.get_active(task.id, stage)
            if art is not None:
                state[state_key[stage]] = art.payload_dict()
                artifacts[art.id] = {
                    "kind": art.kind or stage,
                    "version": art.stage_version,
                    "payload_ref": art.id,
                    "confirmed_by": art.confirmed_by or "user",
                    "payload": art.payload_dict(),
                }

        # clauses 作 intake 产物镜像
        if state.get("clauses"):
            cid = f"art-clauses-{task.id[:8]}"
            artifacts[cid] = {
                "kind": "clauses",
                "version": 1,
                "payload_ref": cid,
                "confirmed_by": "user",
                "payload": state["clauses"],
            }

    plan = _build_rollback_plan(target_stage, artifacts=artifacts)
    state["agent_plan"] = plan.model_dump()
    state["artifacts"] = artifacts
    # 入口不注入目标阶段 plan 字段（避免覆盖 DB 回放）；修订已落库
    del entry_plan  # 保留签名兼容；产物以 DB active 为准
    return state
