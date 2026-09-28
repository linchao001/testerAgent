"""case_generate 节点：测试用例批次生成与提交（dd §7.5③ §7.4③ §11.1 §4.2）。

流程（dd §7.5③ / §11.1）：

1. 输入：CP2 确认后的 PointPlan（state.point_plan），单元=active 测试点
   （默认每批 5 点）；
2. 走 §7.3 批次骨架：每批独立调 passage 档检索子图
   （``RETRIEVAL_PRESETS[case_generate]``，allowed_types=[API,DB,DEFECT,BUSINESS]，
   scope=本批 points 的 link_ids/story_ids）、每批独立写 trace/snapshot；
3. LLM 按 ``<point id="…">`` 分段产出用例（system.shared + case_generate.main）；
   服务端不信模型自造 case_id：按 ``uuid5(CASE_ID_NS,
   f"{task_id}|{version}|{batch_id}|{point_id}|{seq}")`` 确定性赋值
   （dd §7.4③），``trace_refs.entry_ids`` 必须是注入白名单子集；
4. **先文件后 DB 提交协议（dd §11.1）**：先原子写每个 case 的 MD 文件，再在
   ``immediate_tx`` 内 ``put_batch``（INSERT OR IGNORE）+ ``sweep_stale_idem``
   （上一轮孤儿行置 obsolete）+ ``artifact.attach_case_ids``（回填 progress）；
5. 全部批次完成后聚合 summary 写 artifact payload，图在静态断点 cp3_gate 挂起
   （CP3 人工确认属后续 WP，本节点只产出）。

崩溃矩阵（dd §11.1）：
- tmp 写盘中断 → 仅留 .tmp，下次维护清理，无 DB 影响；
- rename 后、DB 事务前 → 孤儿 MD 文件，put_batch 重放补行（同 hash 写文件无副作用）；
- DB 事务中 → 事务回滚，文件已成，重跑批次同 hash 无副作用、行重新插入；
- DB 提交后 → 一致。

与设计偏离（WP-19 交接单登记）：① 新增纯函数 ``finalize_cases`` 承担
"case_id 确定性赋值 + entry_ids 白名单过滤"；② LLM 输出采用 ``by_point``
dict（point_id → cases 数组）而非 prompt 中的分段文本，便于服务端按点对齐
seq（prompt 仍要求分段，模型输出结构化 JSON）；③ case_generate 产物主要落
testcase 行而非单一 artifact payload，artifact 仅存 summary + progress；
④ 检索 scope 同时传 link_ids/story_ids（从 state.link_plan 映射 story→link，
无 link_plan 时仅 story_ids）。

WP-20：单批闭环抽出为 :func:`generate_case_batch`，供 coverage_check
以未覆盖条款为虚拟单元补充生成（``batch_id=sup{round}``，dd §7.5④）复用
同一检索/LLM/提交/trace 路径；虚拟单元无 story 归属时 scope=None（不做
归属过滤，仅按 allowed_types 类型过滤）。
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from ...domain import (
    CaseFileContent,
    CaseRecord,
    CaseStep,
    Lineage,
    LinkPlan,
    PointPlan,
    ReviewStatus,
    TestPoint as PointModel,
    TraceRefs,
)
from ...errors import AppError, LLMBadOutput
from ...logging_config import get_logger
from ...graph.batch import run_in_batches
from ...graph.constants import BATCH_DEFAULT_SIZE, RETRIEVAL_PRESETS, STAGE_CASE_GENERATE
from ...graph.retrieval.pipeline import close_retrieval_trace, retrieve_pipeline
from ...prompts.loader import PromptLoader
from ...store.models import ArtifactRow, CaseRow
from .link_identify import render_knowledge_block
from .point_write import _read_clause_texts, render_clauses_block

if TYPE_CHECKING:
    from ...runtime.context import TaskContext

logger = get_logger(__name__)

_SYSTEM_PROMPT = "system.shared"
_MAIN_PROMPT = "case_generate.main"

# case_id 确定性命名空间（dd §7.4③：同 task+version+batch+point+seq 恒等）
CASE_ID_NS = uuid.UUID("1b3f9c2e-7a4d-4e1a-9c2b-8f6d3e1a0b2c")

_VALID_PRIORITIES = {"P0", "P1", "P2"}

_CASE_SCHEMA: dict = {
    "type": "object",
    "required": ["by_point"],
    "properties": {
        "by_point": {
            "type": "object",
            "additionalProperties": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["title", "steps"],
                    "properties": {
                        "title": {"type": "string"},
                        "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
                        "preconditions": {"type": "array", "items": {"type": "string"}},
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["seq", "action"],
                                "properties": {
                                    "seq": {"type": "integer"},
                                    "action": {"type": "string"},
                                    "expect": {"type": "string"},
                                },
                            },
                        },
                        "test_data": {"type": ["string", "null"]},
                        "trace_refs": {
                            "type": "object",
                            "properties": {
                                "clause_ids": {"type": "array", "items": {"type": "string"}},
                                "entry_ids": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                    },
                },
            },
        }
    },
}


# ---------- 纯函数 ----------


def build_case_intent(points: list[PointModel]) -> str:
    """检索意图：本批测试点标题 + 角度 + 方法拼接（dd §8.1 类推）。"""
    parts: list[str] = []
    for p in points:
        parts.append(f"{p.title}（{p.angle}，{p.method}）")
    return "\n".join(parts)


def render_points_block(
    points: list[PointModel], clause_texts: dict[str, str]
) -> str:
    """渲染本批测试点块（每点含其 clause_ids 原文，供模型展开步骤）。"""
    lines = ["<points>"]
    for p in points:
        lines.append(f'<point id="{p.point_id}" story="{p.story_id}" '
                     f'angle="{p.angle}" method="{p.method}">')
        lines.append(f"  title: {p.title}")
        lines.append(f"  clause_ids: {json.dumps(p.clause_ids, ensure_ascii=False)}")
        for cid in p.clause_ids:
            if cid in clause_texts:
                lines.append(f'  <clause id="{cid}">{clause_texts[cid].strip()}</clause>')
        lines.append("</point>")
    lines.append("</points>")
    return "\n".join(lines)


def finalize_cases(
    obj: Any,
    *,
    points: list[PointModel],
    whitelist: set[str],
    task_id: str,
    version: int,
    batch_id: str,
) -> tuple[list[tuple[PointModel, CaseFileContent]], list[dict], list[str]]:
    """LLM 产物 → list[(point, CaseFileContent)]；返回 (cases, degraded, asserted)。

    - case_id 服务端确定性赋值（不信模型）；
    - point_id 必须在本批 points 中（悬空 → LLMBadOutput）；
    - trace_refs.entry_ids 过滤为白名单子集，非白名单记 degraded 并剔除；
    - priority 非法或缺省 → P1；steps 必填且 action 非空。
    """
    if not isinstance(obj, dict):
        raise LLMBadOutput("case_generate 输出不是 JSON 对象")
    by_point = obj.get("by_point")
    if not isinstance(by_point, dict):
        raise LLMBadOutput("case_generate 输出缺少 by_point 对象")

    point_map = {p.point_id: p for p in points}
    out: list[tuple[PointModel, CaseFileContent]] = []
    degraded: list[dict] = []
    asserted: list[str] = []

    for p in points:
        raw_cases = by_point.get(p.point_id)
        if raw_cases is None:
            # 模型未对某点产出用例：允许为空（不报错），由覆盖率检查兜底
            continue
        if not isinstance(raw_cases, list):
            raise LLMBadOutput(f"by_point[{p.point_id}] 不是数组")
        for seq, raw in enumerate(raw_cases):
            if not isinstance(raw, dict):
                raise LLMBadOutput(f"by_point[{p.point_id}][{seq}] 不是对象")
            title = str(raw.get("title") or "").strip()
            if not title:
                raise LLMBadOutput(f"用例 title 为空：point={p.point_id} seq={seq}")
            priority = str(raw.get("priority") or "P1")
            if priority not in _VALID_PRIORITIES:
                priority = "P1"
            preconditions = raw.get("preconditions") or []
            if not isinstance(preconditions, list) or not all(
                isinstance(x, str) for x in preconditions
            ):
                raise LLMBadOutput(f"preconditions 必须是字符串数组：point={p.point_id}")
            raw_steps = raw.get("steps")
            if not isinstance(raw_steps, list) or not raw_steps:
                raise LLMBadOutput(f"steps 不能为空：point={p.point_id}")
            steps: list[CaseStep] = []
            for si, st in enumerate(raw_steps):
                if not isinstance(st, dict):
                    raise LLMBadOutput(f"step[{si}] 不是对象")
                action = str(st.get("action") or "").strip()
                if not action:
                    raise LLMBadOutput(f"step[{si}].action 为空：point={p.point_id}")
                steps.append(
                    CaseStep(
                        seq=int(st.get("seq", si + 1)),
                        action=action,
                        expect=str(st.get("expect") or ""),
                    )
                )
            test_data = raw.get("test_data")
            if test_data is not None and not isinstance(test_data, str):
                raise LLMBadOutput(f"test_data 必须是字符串或 null：point={p.point_id}")

            trace_refs_raw = raw.get("trace_refs") or {}
            if not isinstance(trace_refs_raw, dict):
                raise LLMBadOutput(f"trace_refs 不是对象：point={p.point_id}")
            clause_ids = trace_refs_raw.get("clause_ids") or []
            if not isinstance(clause_ids, list) or not all(
                isinstance(x, str) for x in clause_ids
            ):
                raise LLMBadOutput(f"trace_refs.clause_ids 必须是字符串数组")
            raw_entries = trace_refs_raw.get("entry_ids") or []
            if not isinstance(raw_entries, list) or not all(
                isinstance(x, str) for x in raw_entries
            ):
                raise LLMBadOutput(f"trace_refs.entry_ids 必须是字符串数组")
            entry_ids: list[str] = []
            for eid in raw_entries:
                asserted.append(eid)
                if eid in whitelist:
                    entry_ids.append(eid)
                else:
                    degraded.append(
                        {
                            "step": "case_generate.source_whitelist",
                            "reason": f"entry_not_injected:{eid}",
                            "fallback": "drop_source_entry",
                        }
                    )

            case_id = uuid.uuid5(
                CASE_ID_NS, f"{task_id}|{version}|{batch_id}|{p.point_id}|{seq}"
            ).hex
            content = CaseFileContent(
                case_id=case_id,
                point_id=p.point_id,
                stage_version=version,
                title=title,
                priority=priority,  # type: ignore[arg-type]
                preconditions=list(preconditions),
                steps=steps,
                test_data=test_data,
                trace_refs=TraceRefs(
                    clause_ids=list(clause_ids),
                    entry_ids=entry_ids,
                    point_ids=[p.point_id],
                ),
            )
            out.append((p, content))

    return out, degraded, asserted


# ---------- 先文件后 DB 提交协议（dd §11.1） ----------


async def commit_case_batch(
    ctx: "TaskContext",
    batch_id: str,
    cases: list[tuple[PointModel, CaseFileContent]],
    version: int,
) -> list[CaseRow]:
    """dd §11.1：先原子写每个 case 的 MD 文件，再 immediate_tx 内
    put_batch + sweep_stale_idem + attach_case_ids。返回写入的 CaseRow 列表。"""
    if ctx.daos is None or ctx.daos.testcase is None or ctx.daos.artifact is None:
        raise RuntimeError("commit_case_batch 缺少 daos.testcase/artifact")

    task = ctx.task
    rows: list[CaseRow] = []
    for point, content in cases:
        written = await ctx.files.write_case(task.workspace_id, task.id, version, content)
        record = CaseRecord(
            case_id=content.case_id,
            point_id=content.point_id,
            stage_version=version,
            lineage=Lineage(root_case_id=content.case_id),
            status="active",  # type: ignore[arg-type]
            review_status=ReviewStatus.PENDING,
            file_path=written.file_path,
            content_hash=written.content_hash,
            title=content.title,
            trace_refs=content.trace_refs,
        )
        rows.append(
            CaseRow.from_record(task.id, record, batch_id=batch_id)
        )

    case_ids = [r.id for r in rows]
    async with ctx.app.db.immediate_tx():
        await ctx.daos.testcase.put_batch(rows)
        await ctx.daos.testcase.sweep_stale_idem(
            task.id, version, batch_id, keep=case_ids
        )
        await ctx.daos.artifact.attach_case_ids(
            task.id, STAGE_CASE_GENERATE, version, batch_id, case_ids
        )
    return rows


# ---------- 单批闭环（case_generate worker；coverage_check 补充生成复用，dd §7.5④） ----------


async def generate_case_batch(
    ctx: "TaskContext",
    points: list[PointModel],
    *,
    version: int,
    batch_id: str,
    story_to_link: dict[str, str] | None = None,
) -> list[CaseRow]:
    """case_generate 单批函数：passage 档检索 → LLM 生成 → finalize →
    先文件后 DB 提交 → trace 闭环。返回本批写入的 CaseRow 列表。

    case_generate_node 的 run_in_batches worker 与 coverage_check 的补充
    生成（``batch_id=sup{round}``，虚拟单元=未覆盖条款派生测试点）共用本
    函数，保证"走同一写入路径"（dd §7.5④）。前置：case_generate 阶段的
    active artifact 已存在（attach_case_ids 的 progress 写入目标）。
    """
    task = ctx.task
    intent = build_case_intent(points)
    scope = _batch_scope(points, story_to_link or {})
    outcome = await retrieve_pipeline(
        ctx,
        RETRIEVAL_PRESETS[STAGE_CASE_GENERATE],
        intent,
        batch_id=batch_id,
        scope=scope,
        stage_version=version,
    )
    needed: list[str] = []
    for p in points:
        for cid in p.clause_ids:
            if cid not in needed:
                needed.append(cid)
    clause_texts = await _read_clause_texts(ctx, needed)

    loader = PromptLoader(custom_dir=ctx.agent_config.get("prompts_dir"))
    system = (
        loader.load(_SYSTEM_PROMPT).content.strip()
        + "\n\n"
        + loader.load(_MAIN_PROMPT).content.strip()
    )
    from ..tool_gather import append_tool_notes, gather_notes_for_ctx

    user_content = "\n".join([
        render_points_block(points, clause_texts),
        render_knowledge_block(outcome.items, ctx.mirror),
    ])
    notes = await gather_notes_for_ctx(
        ctx, "case_generate", "查看工作区文件，辅助生成测试用例"
    )
    user_content = append_tool_notes(user_content, notes)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    result = await ctx.app.llm.chat(messages, json_schema=_CASE_SCHEMA)
    try:
        obj = json.loads(result.content)
    except Exception as e:
        raise LLMBadOutput(f"case_generate 输出不是合法 JSON: {e}") from e

    cases, degraded, asserted = finalize_cases(
        obj, points=points, whitelist=set(outcome.injected),
        task_id=task.id, version=version, batch_id=batch_id,
    )
    rows = await commit_case_batch(ctx, batch_id, cases, version)

    asserted_tags = " ".join(f"[ID:{eid}]" for eid in asserted)
    await close_retrieval_trace(ctx, outcome, [result.content, asserted_tags])
    if degraded:
        from ...store.models import TraceDAO

        await TraceDAO(ctx.app.db).append_degraded(outcome.trace_id, degraded)
    return rows


# ---------- 节点 ----------


async def case_generate_node(ctx: "TaskContext", state: dict) -> dict[str, Any]:
    """主图 case_generate 节点（dd §7.5③）；返回 state 增量。"""
    if ctx.daos is None or ctx.daos.artifact is None or ctx.daos.testcase is None:
        raise AppError(
            "case_generate 节点缺少 ctx.daos.artifact/testcase",
            details={"node": STAGE_CASE_GENERATE},
        )
    task = ctx.task
    artifact_dao = ctx.daos.artifact
    testcase_dao = ctx.daos.testcase
    run_id = task.graph_run_id or ctx.run_id

    # ① 取已确认 PointPlan（CP2 放行后 state.point_plan）
    plan_dict = (state or {}).get("point_plan")
    if not isinstance(plan_dict, dict) or not plan_dict.get("points"):
        raise AppError(
            "case_generate 缺少已确认 point_plan（CP2 未确认或 point_write 未执行）",
            details={"node": STAGE_CASE_GENERATE},
        )
    try:
        point_plan = PointPlan.model_validate(plan_dict)
    except Exception as e:
        raise AppError(
            "case_generate 无法解析 point_plan",
            details={"node": STAGE_CASE_GENERATE, "error": str(e)},
        ) from e

    active_points = [p for p in point_plan.points]
    if not active_points:
        raise AppError(
            "case_generate 无可处理测试点",
            details={"node": STAGE_CASE_GENERATE},
        )

    # story → link 映射（检索 scope 用），link_plan 可能仍在 state
    link_plan = _parse_link_plan((state or {}).get("link_plan"))
    story_to_link: dict[str, str] = {}
    if link_plan is not None:
        story_to_link = {s.story_id: s.link_id for s in link_plan.stories}

    # ② 崩溃重放：同 run 已有完整产物（progress 全部 done 且 payload 有 case_count）
    existing = await artifact_dao.get_active(task.id, STAGE_CASE_GENERATE)
    if existing is not None and existing.graph_run_id == run_id:
        payload = existing.payload_dict()
        if payload.get("case_count") and _all_batches_done(existing.progress_list()):
            logger.info(
                "case_generate replay existing artifact",
                extra={"task_id": task.id, "artifact_id": existing.id},
            )
            return _case_increment(payload, existing.stage_version, state)

    # ③ 建 active artifact
    async with ctx.app.db.immediate_tx():
        guard = await artifact_dao.get_active(task.id, STAGE_CASE_GENERATE)
        if guard is not None and guard.graph_run_id == run_id:
            artifact_id = guard.id
            version = guard.stage_version
        else:
            version = await artifact_dao.next_version(task.id, STAGE_CASE_GENERATE)
            artifact_id = uuid.uuid4().hex
            await artifact_dao.put(
                ArtifactRow.create(
                    id=artifact_id, task_id=task.id, stage=STAGE_CASE_GENERATE,
                    graph_run_id=run_id, stage_version=version, payload={},
                    origin="system", status="active", confirmed_by=None, progress=[],
                )
            )

    # ④ worker：单批闭环（generate_case_batch；coverage_check 补充生成同路径）
    async def worker(ctx_: "TaskContext", batch_id: str, subset: list) -> list[CaseRow]:
        return await generate_case_batch(
            ctx_,
            subset,
            version=version,
            batch_id=batch_id,
            story_to_link=story_to_link,
        )

    # ⑤ 批次执行
    all_rows, cursor = await run_in_batches(
        ctx,
        STAGE_CASE_GENERATE,
        active_points,
        worker,
        size=BATCH_DEFAULT_SIZE,
        state=state,
        unit_id=lambda p: p.point_id,
        result_id=lambda r: r.id,
    )

    # ⑥ 回填 artifact payload（summary）
    case_ids = [r.id for r in all_rows]
    payload = {"case_count": len(case_ids), "case_ids": case_ids}
    async with ctx.app.db.immediate_tx():
        current = await artifact_dao.get(artifact_id)
        if not current.payload_dict().get("case_count"):
            await ctx.app.db.aexecute(
                "UPDATE stage_artifact SET payload = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), artifact_id),
            )
        else:
            payload = current.payload_dict()

    logger.info(
        "case_generate artifact written",
        extra={"task_id": task.id, "version": version, "cases": len(case_ids)},
    )
    return _case_increment(payload, version, state, cursor)


# ---------- 辅助 ----------


def _parse_link_plan(data: Any) -> LinkPlan | None:
    if not isinstance(data, dict) or not data.get("stories"):
        return None
    try:
        return LinkPlan.model_validate(data)
    except Exception:
        return None


def _batch_scope(
    points: list[PointModel], story_to_link: dict[str, str]
) -> dict | None:
    """本批检索 scope：story_ids + 映射到的 link_ids。

    虚拟单元（coverage_check 补充生成）无 story 归属（story_id 为空）：
    两集合均空时返回 None——管线不做归属裁决，仅按 allowed_types 类型
    过滤（不能传空列表白名单，否则会把全部候选 filtered_scope 掉）。
    """
    link_ids: set[str] = set()
    story_ids: set[str] = set()
    for p in points:
        if not p.story_id:
            continue
        story_ids.add(p.story_id)
        lid = story_to_link.get(p.story_id)
        if lid:
            link_ids.add(lid)
    if not link_ids and not story_ids:
        return None
    return {"link_ids": sorted(link_ids), "story_ids": sorted(story_ids)}


def _all_batches_done(progress: list[dict]) -> bool:
    if not progress:
        return False
    return all(b.get("status") == "done" for b in progress)


def _case_increment(
    payload: dict, version: int, state: dict | None, cursor: dict | None = None
) -> dict[str, Any]:
    versions = dict((state or {}).get("current_stage_version") or {})
    versions[STAGE_CASE_GENERATE] = version
    inc: dict[str, Any] = {
        "case_count": payload.get("case_count", 0),
        "case_ids": list(payload.get("case_ids") or []),
        "current_stage_version": versions,
    }
    if cursor is not None:
        cursors = dict((state or {}).get("batch_cursor") or {})
        cursors[STAGE_CASE_GENERATE] = cursor
        inc["batch_cursor"] = cursors
    return inc
