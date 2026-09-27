"""coverage_check 节点：程序化覆盖矩阵 + ≤2 轮补充生成（dd §7.5④ §2.6）。

流程（dd §7.5④）：

1. **程序化矩阵**（无 LLM）：对每条 active clause，检查是否有 point 的
   ``clause_ids`` 命中（点覆盖，object_type="point" 行，evidence=点标题）、
   该 point 下是否有 active case（例覆盖，object_type="case" 行，
   evidence=用例标题）；两类命中都不满足的条款进 ``uncovered_clauses``。
2. **补充生成**：uncovered 非空且补充轮次 < ``coverage_max_rounds``
   （runtime_config，默认 2，dd §13.2）时，以未覆盖条款派生**虚拟测试点**
   （``pt-sup{round}-{seq}``，确定性序号取当轮 uncovered 在条款序中的
   1-based 位置）为单元，调一次 :func:`generate_case_batch`
   （case_generate 单批函数，同一检索/LLM/先文件后 DB 提交/trace 路径，
   ``batch_id=sup{round}``），随后重算矩阵。
3. **告警降级**：达上限仍有未覆盖 → ``CoverageMatrix.degraded=true``，
   ``coverage_ready`` 事件带 warnings；任务仍可 completed（tech-design
   §4.2 告警降级）。全覆盖时同样发 coverage_ready（warnings 为空）。

崩溃恢复：coverage artifact 仅在矩阵终态落库；节点在补充轮中途崩溃后
重入时，根据 case_generate artifact.progress 中已提交的 sup 批次数
（commit_case_batch 的 attach_case_ids 与 put_batch 同事务，批要么全
成要么不存在），按"历轮 uncovered 序列"确定性重建虚拟点（``pt-sup{k}-i``
的序号只依赖条款序与历史结果），历史轮零 LLM，仅补跑剩余轮次。同 run
coverage artifact 已带 rows 时直接回放（零 LLM/检索）。

与设计偏离（WP-20 交接单登记）：① 矩阵行只落 covered=True 的命中行
（point/case 对象存在才有行），未覆盖经 uncovered_clauses 表达——
dd §2.6 CoverageRow.covered 字段保留以承载后续 review_export 阶段可能
的人工/失效覆盖行；② 虚拟点 ``story_id=""``、source_entry_ids=[]，其
补充用例经 MD/testcase 行正常落库（point_id 挂虚拟点），检索 scope=None
（无 link/story 归属，仅按 allowed_types 类型过滤）；③ 补充批次的
progress 记录随 commit_case_batch 落在 **case_generate** artifact
（"走同一写入路径"），coverage artifact 只存终态 CoverageMatrix；
④ coverage_max_rounds 从 runtime_config 读取（config 引导行缺失/值
非法时退回默认 2）。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from ...domain import (
    ClauseRef,
    CoverageMatrix,
    CoverageRow,
    LinkPlan,
    PointPlan,
    TestPoint,
)
from ...errors import AppError, TaskCancelled
from ...logging_config import get_logger
from ...graph.constants import STAGE_CASE_GENERATE, STAGE_COVERAGE_CHECK
from ...store.models import ArtifactRow, CaseRow
from .case_generate import generate_case_batch

if TYPE_CHECKING:
    from ...runtime.context import TaskContext

logger = get_logger(__name__)

# dd §13.2 runtime_config.coverage_max_rounds 默认值（上限 2）
COVERAGE_MAX_ROUNDS_DEFAULT = 2

# 补充批次 batch_id 前缀（dd §7.5④：batch_id=sup{round}）
SUPP_BATCH_PREFIX = "sup"

# 虚拟测试点固定标签（angle/method 仅作 LLM 意图与可读标识，不参与落库枚举）
_SUPP_ANGLE = "补充覆盖"
_SUPP_METHOD = "条款兜底"


# ---------- 纯函数 ----------


def build_virtual_points(
    clauses: list[ClauseRef], *, round_no: int
) -> list[TestPoint]:
    """以未覆盖条款为虚拟单元派生测试点（dd §7.5④）。

    point_id=``pt-sup{round_no}-{seq}``，seq 取条款在当轮 uncovered 列表
    中的 1-based 位置（uncovered 按条款序产出，故同状态同序号恒等，崩溃
    重建与现场执行产出同一组虚拟点）；story_id 为空（无链路归属），
    clause_ids 仅本条款。
    """
    out: list[TestPoint] = []
    for seq, c in enumerate(clauses, start=1):
        title_tail = " / ".join(c.title_path) if c.title_path else c.anchor
        out.append(
            TestPoint(
                point_id=f"pt-{SUPP_BATCH_PREFIX}{round_no}-{seq}",
                story_id="",
                title=f"[{_SUPP_ANGLE}] {title_tail or c.clause_id}",
                angle=_SUPP_ANGLE,
                method=_SUPP_METHOD,
                clause_ids=[c.clause_id],
                source_entry_ids=[],
                priority="P1",
            )
        )
    return out


def build_coverage_matrix(
    clauses: list[ClauseRef],
    points: list[TestPoint],
    cases: list[CaseRow],
    *,
    supplemental_rounds: int = 0,
    degraded: bool = False,
) -> CoverageMatrix:
    """程序化覆盖矩阵（dd §7.5④-1，纯函数无 IO）。

    - 仅统计 status=active 条款（deleted 条款不要求覆盖）；
    - 点覆盖：point.clause_ids 命中条款 → covered=True 的 point 行；
    - 例覆盖：该 point 下存在 active case → 每个 active case 一条
      covered=True 的 case 行；条款在任一命中 point 下有 active case
      才视为已覆盖，否则进 uncovered_clauses。
    """
    points_by_clause: dict[str, list[TestPoint]] = {}
    for p in points:
        for cid in dict.fromkeys(p.clause_ids):  # 点内去重，保序
            points_by_clause.setdefault(cid, []).append(p)

    cases_by_point: dict[str, list[CaseRow]] = {}
    for case in cases:
        if case.status == "active":
            cases_by_point.setdefault(case.point_id, []).append(case)

    rows: list[CoverageRow] = []
    uncovered: list[str] = []
    for clause in clauses:
        if clause.status != "active":
            continue
        hits = points_by_clause.get(clause.clause_id, [])
        covered = False
        for p in hits:
            rows.append(
                CoverageRow(
                    clause_id=clause.clause_id,
                    object_type="point",
                    object_id=p.point_id,
                    covered=True,
                    evidence=p.title,
                )
            )
            hit_cases = cases_by_point.get(p.point_id, [])
            for case in hit_cases:
                rows.append(
                    CoverageRow(
                        clause_id=clause.clause_id,
                        object_type="case",
                        object_id=case.id,
                        covered=True,
                        evidence=case.title,
                    )
                )
            if hit_cases:
                covered = True
        if not covered:
            uncovered.append(clause.clause_id)

    return CoverageMatrix(
        rows=rows,
        uncovered_clauses=uncovered,
        supplemental_rounds=supplemental_rounds,
        degraded=degraded,
    )


def matrix_summary(matrix: CoverageMatrix, *, clause_count: int) -> dict:
    """coverage_ready 事件的 matrix_summary（dd §10.4 payload）。"""
    point_rows = sum(1 for r in matrix.rows if r.object_type == "point")
    case_rows = len(matrix.rows) - point_rows
    return {
        "clause_count": clause_count,
        "point_row_count": point_rows,
        "case_row_count": case_rows,
        "covered_clause_count": clause_count - len(matrix.uncovered_clauses),
        "uncovered_count": len(matrix.uncovered_clauses),
        "supplemental_rounds": matrix.supplemental_rounds,
        "degraded": matrix.degraded,
    }


def count_supp_rounds(progress: list[dict]) -> int:
    """从 case_generate artifact.progress 读已提交补充批次数（sup1/sup2…）。

    取最大轮号（补充轮严格顺序执行）；attach_case_ids 与 put_batch 同事务，
    记录存在即该批已完整提交，可作崩溃恢复的确定性起点。
    """
    rounds = 0
    for b in progress or []:
        bid = b.get("batch_id") or ""
        if bid.startswith(SUPP_BATCH_PREFIX):
            suffix = bid[len(SUPP_BATCH_PREFIX):]
            if suffix.isdigit():
                rounds = max(rounds, int(suffix))
    return rounds


# ---------- 节点 ----------


async def coverage_check_node(ctx: "TaskContext", state: dict) -> dict[str, Any]:
    """主图 coverage_check 节点（dd §7.5④）；返回 coverage 状态增量。"""
    if ctx.daos is None or ctx.daos.artifact is None or ctx.daos.testcase is None:
        raise AppError(
            "coverage_check 节点缺少 ctx.daos.artifact/testcase",
            details={"node": STAGE_COVERAGE_CHECK},
        )
    task = ctx.task
    artifact_dao = ctx.daos.artifact
    run_id = task.graph_run_id or ctx.run_id

    # ① 输入：active clauses + 已确认 PointPlan
    clauses = _parse_clauses((state or {}).get("clauses"))
    if not clauses:
        raise AppError(
            "coverage_check 缺少条款索引（intake 未执行或 clauses 为空）",
            details={"node": STAGE_COVERAGE_CHECK},
        )
    plan_dict = (state or {}).get("point_plan")
    if not isinstance(plan_dict, dict) or not plan_dict.get("points"):
        raise AppError(
            "coverage_check 缺少已确认 point_plan（CP2 未确认或 point_write 未执行）",
            details={"node": STAGE_COVERAGE_CHECK},
        )
    try:
        point_plan = PointPlan.model_validate(plan_dict)
    except Exception as e:
        raise AppError(
            "coverage_check 无法解析 point_plan",
            details={"node": STAGE_COVERAGE_CHECK, "error": str(e)},
        ) from e

    # ② 崩溃重放：同 run 已有终态矩阵 → 直接回放（零 LLM/检索）
    existing = await artifact_dao.get_active(task.id, STAGE_COVERAGE_CHECK)
    if existing is not None and existing.graph_run_id == run_id:
        payload = existing.payload_dict()
        if payload.get("rows"):
            logger.info(
                "coverage_check replay existing artifact",
                extra={"task_id": task.id, "artifact_id": existing.id},
            )
            return _coverage_increment(payload, existing.stage_version, state)

    # ③ case_generate 产物版本（矩阵只统计当前版本 active 用例）
    case_art = await artifact_dao.get_active(task.id, STAGE_CASE_GENERATE)
    if case_art is None:
        raise AppError(
            "coverage_check 缺少 case_generate 阶段产物",
            details={"node": STAGE_COVERAGE_CHECK},
        )
    version = case_art.stage_version

    link_plan = _parse_link_plan((state or {}).get("link_plan"))
    story_to_link: dict[str, str] = {}
    if link_plan is not None:
        story_to_link = {s.story_id: s.link_id for s in link_plan.stories}

    max_rounds = await _resolve_max_rounds(ctx)

    # ④ 初始矩阵 + 崩溃恢复：确定性重建历史补充轮（零 LLM）
    clause_map = {c.clause_id: c for c in clauses}
    all_points: list[TestPoint] = list(point_plan.points)
    active_cases = await _list_active_cases(ctx, task.id, version)
    matrix = build_coverage_matrix(clauses, all_points, active_cases)
    rounds_done = count_supp_rounds(case_art.progress_list())
    supp_points: list[TestPoint] = []
    rounds = 0
    while rounds < rounds_done:
        rounds += 1
        uncovered_refs = [clause_map[cid] for cid in matrix.uncovered_clauses]
        vps = build_virtual_points(uncovered_refs, round_no=rounds)
        supp_points.extend(vps)
        all_points = [*point_plan.points, *supp_points]
        matrix = build_coverage_matrix(
            clauses, all_points, active_cases, supplemental_rounds=rounds
        )
    logger.info(
        "coverage_check initial matrix",
        extra={
            "task_id": task.id,
            "uncovered": len(matrix.uncovered_clauses),
            "rounds_rebuilt": rounds_done,
        },
    )

    # ⑤ 补充生成（≤ max_rounds 轮，每轮边界检查取消）
    while matrix.uncovered_clauses and rounds < max_rounds:
        if await ctx.cancelled():
            raise TaskCancelled(
                f"coverage_check 补充生成被取消：round={rounds + 1}"
            )
        rounds += 1
        uncovered_refs = [clause_map[cid] for cid in matrix.uncovered_clauses]
        vps = build_virtual_points(uncovered_refs, round_no=rounds)
        supp_points.extend(vps)
        batch_id = f"{SUPP_BATCH_PREFIX}{rounds}"
        await generate_case_batch(
            ctx,
            vps,
            version=version,
            batch_id=batch_id,
            story_to_link=story_to_link,
        )
        active_cases = await _list_active_cases(ctx, task.id, version)
        all_points = [*point_plan.points, *supp_points]
        matrix = build_coverage_matrix(
            clauses, all_points, active_cases, supplemental_rounds=rounds
        )
        logger.info(
            "coverage_check supplemental round done",
            extra={
                "task_id": task.id,
                "round": rounds,
                "batch_id": batch_id,
                "new_cases": len(active_cases),
                "uncovered": len(matrix.uncovered_clauses),
            },
        )

    # ⑥ 达上限仍有未覆盖 → 告警降级（任务仍可 completed，tech-design §4.2）
    if matrix.uncovered_clauses:
        matrix = matrix.model_copy(update={"degraded": True})

    # ⑦ 落 coverage artifact（事务内复查防重）
    payload = matrix.model_dump()
    async with ctx.app.db.immediate_tx():
        guard = await artifact_dao.get_active(task.id, STAGE_COVERAGE_CHECK)
        if guard is not None and guard.graph_run_id == run_id and guard.payload_dict().get("rows"):
            payload = guard.payload_dict()
            cov_version = guard.stage_version
        else:
            cov_version = (
                guard.stage_version
                if guard is not None and guard.graph_run_id == run_id
                else await artifact_dao.next_version(task.id, STAGE_COVERAGE_CHECK)
            )
            artifact_id = uuid.uuid4().hex
            await artifact_dao.put(
                ArtifactRow.create(
                    id=artifact_id,
                    task_id=task.id,
                    stage=STAGE_COVERAGE_CHECK,
                    graph_run_id=run_id,
                    stage_version=cov_version,
                    payload=payload,
                    origin="system",
                    status="active",
                    confirmed_by=None,
                    progress=[],
                )
            )

    # ⑧ coverage_ready 事件（全覆盖 warnings 为空；降级带未覆盖条款告警）
    warnings: list[dict] = []
    if matrix.degraded:
        warnings.append(
            {
                "reason": "uncovered_after_max_rounds",
                "uncovered_clauses": list(matrix.uncovered_clauses),
                "max_rounds": max_rounds,
            }
        )
    if ctx.emit is not None:
        await ctx.emit(
            "coverage_ready",
            {
                "matrix_summary": matrix_summary(
                    matrix, clause_count=len(clauses)
                ),
                "warnings": warnings,
            },
        )

    logger.info(
        "coverage_check artifact written",
        extra={
            "task_id": task.id,
            "version": cov_version,
            "rows": len(matrix.rows),
            "uncovered": len(matrix.uncovered_clauses),
            "rounds": matrix.supplemental_rounds,
            "degraded": matrix.degraded,
        },
    )
    return _coverage_increment(payload, cov_version, state)


# ---------- 辅助 ----------


def _parse_clauses(data: Any) -> list[ClauseRef]:
    if not isinstance(data, list):
        return []
    out: list[ClauseRef] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        try:
            out.append(ClauseRef.model_validate(raw))
        except Exception:
            continue
    return out


def _parse_link_plan(data: Any) -> LinkPlan | None:
    if not isinstance(data, dict) or not data.get("stories"):
        return None
    try:
        return LinkPlan.model_validate(data)
    except Exception:
        return None


async def _resolve_max_rounds(ctx: "TaskContext") -> int:
    """读 runtime_config.coverage_max_rounds（dd §13.2）；缺省/非法退回默认 2。"""
    try:
        cfg = await ctx.app.config.get()
        val = cfg.runtime_dict().get(
            "coverage_max_rounds", COVERAGE_MAX_ROUNDS_DEFAULT
        )
        rounds = int(val)
        if rounds < 0:
            return COVERAGE_MAX_ROUNDS_DEFAULT
        return rounds
    except Exception:
        return COVERAGE_MAX_ROUNDS_DEFAULT


async def _list_active_cases(
    ctx: "TaskContext", task_id: str, version: int
) -> list[CaseRow]:
    """游标翻页拉全量当前版本 active 用例（节点内部全量统计，用例量级有限）。"""
    dao = ctx.daos.testcase  # type: ignore[union-attr]
    out: list[CaseRow] = []
    cursor: str | None = None
    while True:
        page = await dao.list_by_task(
            task_id,
            status="active",
            review=None,
            version=version,
            cursor=cursor,
            limit=200,
        )
        out.extend(page.items)
        if not page.next_cursor:
            return out
        cursor = page.next_cursor


def _coverage_increment(
    payload: dict, version: int, state: dict | None
) -> dict[str, Any]:
    versions = dict((state or {}).get("current_stage_version") or {})
    versions[STAGE_COVERAGE_CHECK] = version
    return {
        "coverage": payload,
        "current_stage_version": versions,
    }
