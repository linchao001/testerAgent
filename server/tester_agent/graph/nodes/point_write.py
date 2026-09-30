"""point_write 节点：测试点生成（dd §7.5③ §7.4② §2.4 §8.6）。

流程（dd §7.5③）：

1. 输入：已确认 LinkPlan（CP1 放行后 state.link_plan），按 link 分组排序
   得到稳定 story 序列与 ``story_index``（供 point_id 确定性赋值）；
2. 走 §7.3 批次骨架：每批独立调 passage 档检索子图
   （``RETRIEVAL_PRESETS[point_write]``，scope=本批 link_ids/story_ids）、
   每批独立写 trace/snapshot（batch_id 贯穿）；
3. LLM 产出测试点（system.shared + point_write.main 模板）；服务端
   **不信模型自造 point_id**：统一改写为 ``pt-{story 序号}-{批内序号}``
   （dd §7.4②），``source_entry_ids`` 必须是注入白名单子集（不在则剔除
   并记 degraded，与 link_identify 同范式）；
4. 生成后 close_retrieval_trace 两写回填（referenced/hallucinated/weak，
   dd §8.6）；全部批次完成后聚合 PointPlan 写 artifact payload，图随后在
   静态断点 cp2_gate 挂起。

崩溃恢复（dd §7.3）：同 run 已存在 active artifact 时——若 payload 已有
points（完整产物）则直接回放；若 payload 为空（崩溃在批次中途）则重走
run_in_batches，progress 中 done 的批跳过、started/failed 的批重做
（idempotency_nonce 确定性派生保证批 idem 不变）。

澄清：point_write 节点契约表仅 cp2_gate 静态中断、无 interrupt() 澄清流；
LLM 若输出非空 clarifications 按 LLMBadOutput 失败（§7.4⑤ 不带半成品
过检查点）。

与设计偏离（WP-18 交接单登记）：① 新增纯函数 ``finalize_point_plan`` 承担
"point_id 确定性赋值 + source_entry_ids 白名单过滤"；② story 序号取"按
link 分组排序后的 1-based 位置"（DD 文字"story 在 LinkPlan 中的序号"未
明确分组口径，按 §7.5③"按 link 分组排序"取分组后序位，CP2 修订时 LinkPlan
不变则序号稳定）；③ 批内序号取模型输出在该批中的 1-based 位置（同输入
确定性）；④ 知识块渲染复用 link_identify.render_knowledge_block（passage
档注入正文，无需特殊处理）。
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from ...domain import DegradedStep, LinkPlan, PointPlan, TestPoint
from ...errors import AppError, LLMBadOutput
from ...logging_config import get_logger
from ...graph.batch import run_in_batches
from ...graph.constants import BATCH_DEFAULT_SIZE, RETRIEVAL_PRESETS, STAGE_POINT_WRITE
from ...context.retrieval.pipeline import close_retrieval_trace, retrieve_pipeline
from ...prompts.loader import PromptLoader
from ...store.models import ArtifactRow
from .link_identify import render_knowledge_block

if TYPE_CHECKING:
    from ...runtime.context import TaskContext

logger = get_logger(__name__)

_SYSTEM_PROMPT = "system.shared"
_MAIN_PROMPT = "point_write.main"

_POINT_SCHEMA: dict = {
    "type": "object",
    "required": ["points"],
    "properties": {
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["story_id", "title", "angle", "method", "clause_ids"],
                "properties": {
                    "point_id": {"type": "string"},
                    "story_id": {"type": "string"},
                    "title": {"type": "string"},
                    "angle": {"type": "string"},
                    "method": {"type": "string"},
                    "clause_ids": {"type": "array", "items": {"type": "string"}},
                    "source_entry_ids": {
                        "type": "array", "items": {"type": "string"}
                    },
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
                },
            },
        },
        "clarifications": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["question"],
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

_VALID_PRIORITIES = {"P0", "P1", "P2"}


# ---------- 纯函数：story 排序 / 意图 / 消息 ----------


def order_stories_by_link(
    plan: LinkPlan,
) -> tuple[list, dict[str, int]]:
    """按 link 分组排序 stories（dd §7.5③"按 link 分组排序"）。

    返回 (有序 StoryRef 列表, story_id -> 1-based 序号)。links 取
    LinkPlan.links 序；每个 link 的 stories 取其在 LinkPlan.stories 中
    的出现序。未被任何 link 引用的孤儿 story 不纳入（finalize 时若模型
    引用会报悬空）。
    """
    ordered: list = []
    story_by_id = {s.story_id: s for s in plan.stories}
    for link in plan.links:
        for sid in link.story_ids:
            s = story_by_id.get(sid)
            if s is not None:
                ordered.append(s)
    index = {s.story_id: i + 1 for i, s in enumerate(ordered)}
    return ordered, index


def build_point_intent(stories: list) -> str:
    """检索意图：本批 story 标题 + 摘要 + 关联条款 anchor 拼接（dd §8.1 类推）。"""
    parts: list[str] = []
    for s in stories:
        head = f"{s.title}：{s.summary}"
        parts.append(head.strip())
    return "\n".join(parts)


def render_stories_block(stories: list, story_index: dict[str, int]) -> str:
    """渲染本批 stories 块（含 story 序号，供模型引用与 point_id 对齐）。"""
    lines = ["<stories>"]
    for s in stories:
        idx = story_index.get(s.story_id, "?")
        lines.append(
            f'<story id="{s.story_id}" seq="{idx}">\n'
            f"  title: {s.title}\n"
            f"  summary: {s.summary}\n"
            f"  related_clause_ids: {json.dumps(s.related_clause_ids, ensure_ascii=False)}\n"
            f"</story>"
        )
    lines.append("</stories>")
    return "\n".join(lines)


async def _read_clause_texts(
    ctx: "TaskContext", clause_ids: list[str]
) -> dict[str, str]:
    """按需读条款原文（通过 intake 落的 clauses cache 取字节偏移）。"""
    if not clause_ids:
        return {}
    task = ctx.task
    try:
        cache = await ctx.files.read_clauses_cache(task.workspace_id, task.id)
    except Exception:
        # 缓存缺失（可再生）：返回空，由 LLM 凭 anchor 工作（不应阻塞）
        return {}
    offset_map: dict[str, tuple[int, int]] = {}
    for c in cache:
        cid = c.get("clause_id")
        if cid and c.get("start_offset") is not None:
            offset_map[cid] = (int(c["start_offset"]), int(c["end_offset"]))
    out: dict[str, str] = {}
    for cid in clause_ids:
        span = offset_map.get(cid)
        if span is None:
            continue
        try:
            out[cid] = await ctx.files.read_clause(
                task.workspace_id, task.id, span
            )
        except Exception:
            continue
    return out


def render_clauses_block(clause_texts: dict[str, str]) -> str:
    if not clause_texts:
        return ""
    lines = ["<requirement_clauses>"]
    for cid, text in clause_texts.items():
        lines.append(f'<clause id="{cid}">\n{text.strip()}\n</clause>')
    lines.append("</requirement_clauses>")
    return "\n".join(lines)


def render_user_message(
    stories_block: str, clauses_block: str, knowledge_block: str
) -> str:
    parts = [stories_block]
    if clauses_block:
        parts.append(clauses_block)
    parts.append(knowledge_block)
    return "\n".join(parts)


# ---------- 纯函数：point_id 赋值 + 白名单校验（dd §7.4②） ----------


def finalize_point_plan(
    obj: Any,
    *,
    whitelist: set[str],
    story_index: dict[str, int],
) -> tuple[list[TestPoint], list[DegradedStep], list[str]]:
    """LLM 产物 → list[TestPoint]；返回 (points, degraded, 断言 entry_id)。

    - point_id 服务端确定性赋值 ``pt-{story 序号}-{批内位置}``（不信模型）；
    - story_id 必须在 story_index 中（悬空引用 → LLMBadOutput）；
    - source_entry_ids 过滤为白名单子集，非白名单项记 degraded 并剔除
      （同时计入 asserted 供 close_loop 对账 hallucinated）；
    - priority 非法或缺省 → P1。
    """
    if not isinstance(obj, dict):
        raise LLMBadOutput("point_write 输出不是 JSON 对象")
    raw_points = obj.get("points")
    if not isinstance(raw_points, list):
        raise LLMBadOutput("point_write 输出缺少 points 数组")
    # point_write 无澄清流：非空 clarifications 直接失败（§7.4⑤）
    clarifications = obj.get("clarifications")
    if clarifications:
        raise LLMBadOutput("point_write 不支持 clarifications（仅 cp2 确认）")

    degraded: list[DegradedStep] = []
    asserted: list[str] = []
    points: list[TestPoint] = []

    for i, raw in enumerate(raw_points):
        if not isinstance(raw, dict):
            raise LLMBadOutput(f"points[{i}] 不是对象")
        story_id = str(raw.get("story_id") or "").strip()
        if story_id not in story_index:
            raise LLMBadOutput(
                f"points[{i}].story_id 引用了不存在的 story: {story_id!r}"
            )
        title = str(raw.get("title") or "").strip()
        if not title:
            raise LLMBadOutput(f"points[{i}].title 为空")
        angle = _str(raw, "angle", i)
        method = _str(raw, "method", i)
        clause_ids = _str_list(raw, "clause_ids", i)

        # source_entry_ids 白名单过滤
        raw_sources = raw.get("source_entry_ids") or []
        if not isinstance(raw_sources, list):
            raise LLMBadOutput(f"points[{i}].source_entry_ids 不是数组")
        sources: list[str] = []
        for eid in raw_sources:
            if not isinstance(eid, str) or not eid:
                continue
            asserted.append(eid)
            if eid in whitelist:
                sources.append(eid)
            else:
                degraded.append(
                    DegradedStep(
                        step="point_write.source_whitelist",
                        reason=f"entry_not_injected:{eid}",
                        fallback="drop_source_entry",
                    )
                )

        priority = str(raw.get("priority") or "P1")
        if priority not in _VALID_PRIORITIES:
            priority = "P1"

        seq = i + 1
        point_id = f"pt-{story_index[story_id]}-{seq}"
        points.append(
            TestPoint(
                point_id=point_id,
                story_id=story_id,
                title=title,
                angle=angle,
                method=method,
                clause_ids=clause_ids,
                source_entry_ids=sources,
                priority=priority,  # type: ignore[arg-type]
            )
        )

    return points, degraded, asserted


def _str(raw: dict, key: str, i: int) -> str:
    val = raw.get(key)
    if not isinstance(val, str):
        raise LLMBadOutput(f"points[{i}].{key} 必须是字符串")
    return val


def _str_list(raw: dict, key: str, i: int) -> list[str]:
    val = raw.get(key)
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise LLMBadOutput(f"points[{i}].{key} 必须是字符串数组")
    return list(val)


# ---------- 节点 ----------


async def point_write_node(ctx: "TaskContext", state: dict) -> dict[str, Any]:
    """主图 point_write 节点（dd §7.5③）；返回 point_plan 状态增量。"""
    if ctx.daos is None or ctx.daos.artifact is None:
        raise AppError(
            "point_write 节点缺少 ctx.daos.artifact（Runner 未注入 DAOs 组）",
            details={"node": STAGE_POINT_WRITE},
        )
    task = ctx.task
    artifact_dao = ctx.daos.artifact
    run_id = task.graph_run_id or ctx.run_id

    # ① 取已确认 LinkPlan（CP1 放行后注入 state）
    link_plan_dict = (state or {}).get("link_plan")
    if not isinstance(link_plan_dict, dict) or not link_plan_dict.get("stories"):
        raise AppError(
            "point_write 缺少已确认 link_plan（CP1 未确认或 link_identify 未执行）",
            details={"node": STAGE_POINT_WRITE},
        )
    try:
        link_plan = LinkPlan.model_validate(link_plan_dict)
    except Exception as e:
        raise AppError(
            "point_write 无法解析 link_plan",
            details={"node": STAGE_POINT_WRITE, "error": str(e)},
        ) from e

    ordered_stories, story_index = order_stories_by_link(link_plan)
    if not ordered_stories:
        raise AppError(
            "point_write 无可处理 stories（LinkPlan 为空）",
            details={"node": STAGE_POINT_WRITE},
        )

    # ② 崩溃重放：同 run 已有完整产物 → 直接回放（零 LLM/检索）
    existing = await artifact_dao.get_active(task.id, STAGE_POINT_WRITE)
    if existing is not None and existing.graph_run_id == run_id:
        payload = existing.payload_dict()
        if payload.get("points"):
            logger.info(
                "point_write replay existing artifact",
                extra={"task_id": task.id, "artifact_id": existing.id},
            )
            return _plan_increment(payload, existing.stage_version, state)

    # ③ 建 active artifact（progress 写入目标；payload 待批次完成后回填）
    async with ctx.app.db.immediate_tx():
        guard = await artifact_dao.get_active(task.id, STAGE_POINT_WRITE)
        if guard is not None and guard.graph_run_id == run_id:
            # 并发/重入：复用已有 artifact（其 progress 可能已有 done 批）
            artifact_id = guard.id
            version = guard.stage_version
        else:
            version = await artifact_dao.next_version(task.id, STAGE_POINT_WRITE)
            artifact_id = uuid.uuid4().hex
            await artifact_dao.put(
                ArtifactRow.create(
                    id=artifact_id,
                    task_id=task.id,
                    stage=STAGE_POINT_WRITE,
                    graph_run_id=run_id,
                    stage_version=version,
                    payload={},  # 批次完成后回填
                    origin="system",
                    status="active",
                    confirmed_by=None,
                    progress=[],
                )
            )

    # ④ worker：单批检索 + 生成 + finalize + trace 闭环
    clauses_state = (state or {}).get("clauses") or []

    async def worker(ctx_: "TaskContext", batch_id: str, subset: list) -> list[TestPoint]:
        intent = build_point_intent(subset)
        scope = _batch_scope(subset)
        outcome = await retrieve_pipeline(
            ctx_,
            RETRIEVAL_PRESETS[STAGE_POINT_WRITE],
            intent,
            batch_id=batch_id,
            scope=scope,
            stage_version=version,
        )

        # 读本批关联条款原文
        needed_clause_ids: list[str] = []
        for s in subset:
            for cid in s.related_clause_ids:
                if cid not in needed_clause_ids:
                    needed_clause_ids.append(cid)
        clause_texts = await _read_clause_texts(ctx_, needed_clause_ids)

        loader = PromptLoader(custom_dir=ctx_.agent_config.get("prompts_dir"))
        system = (
            loader.load(_SYSTEM_PROMPT).content.strip()
            + "\n\n"
            + loader.load(_MAIN_PROMPT).content.strip()
        )
        from ..tool_gather import append_tool_notes, gather_notes_for_ctx

        user_content = render_user_message(
            render_stories_block(subset, story_index),
            render_clauses_block(clause_texts),
            render_knowledge_block(outcome.items, ctx_.mirror),
        )
        notes = await gather_notes_for_ctx(
            ctx_, "point_write", "查看工作区文件，辅助撰写测试点"
        )
        user_content = append_tool_notes(user_content, notes)
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        result = await ctx_.app.llm.chat(messages, json_schema=_POINT_SCHEMA)
        raw = result.content
        try:
            obj = json.loads(raw)
        except Exception as e:
            raise LLMBadOutput(f"point_write 输出不是合法 JSON: {e}") from e

        whitelist = set(outcome.injected)
        points, plan_degraded, asserted = finalize_point_plan(
            obj, whitelist=whitelist, story_index=story_index,
        )

        asserted_tags = " ".join(f"[ID:{eid}]" for eid in asserted)
        await close_retrieval_trace(ctx_, outcome, [raw, asserted_tags])
        if plan_degraded:
            from ...store.models import TraceDAO

            await TraceDAO(ctx_.app.db).append_degraded(
                outcome.trace_id, plan_degraded
            )
        return points

    # ⑤ 批次执行
    points, cursor = await run_in_batches(
        ctx,
        STAGE_POINT_WRITE,
        ordered_stories,
        worker,
        size=BATCH_DEFAULT_SIZE,
        state=state,
        unit_id=lambda s: s.story_id,
        result_id=lambda p: p.point_id,
    )

    # ⑥ 回填 artifact payload（事务内复查防重）
    plan = PointPlan(points=points)
    async with ctx.app.db.immediate_tx():
        current = await artifact_dao.get(artifact_id)
        if current.payload_dict().get("points"):
            # 并发已回填，以 DB 为准
            final_payload = current.payload_dict()
            final_version = current.stage_version
        else:
            await _update_payload(ctx, artifact_id, plan)
            final_payload = plan.model_dump()
            final_version = version

    logger.info(
        "point_write artifact written",
        extra={
            "task_id": task.id,
            "version": final_version,
            "points": len(plan.points),
            "stories": len(ordered_stories),
        },
    )
    return _plan_increment(final_payload, final_version, state, cursor)


def _batch_scope(stories: list) -> dict:
    link_ids: set[str] = set()
    story_ids: set[str] = set()
    for s in stories:
        link_ids.add(s.link_id)
        story_ids.add(s.story_id)
    return {"link_ids": sorted(link_ids), "story_ids": sorted(story_ids)}


async def _update_payload(
    ctx: "TaskContext", artifact_id: str, plan: PointPlan
) -> None:
    await ctx.app.db.aexecute(
        "UPDATE stage_artifact SET payload = ? WHERE id = ?",
        (json.dumps(plan.model_dump(), ensure_ascii=False), artifact_id),
    )


def _plan_increment(
    plan_dict: dict, version: int, state: dict | None, cursor: dict | None = None
) -> dict[str, Any]:
    versions = dict((state or {}).get("current_stage_version") or {})
    versions[STAGE_POINT_WRITE] = version
    inc: dict[str, Any] = {
        "point_plan": plan_dict,
        "current_stage_version": versions,
    }
    if cursor is not None:
        cursors = dict((state or {}).get("batch_cursor") or {})
        cursors[STAGE_POINT_WRITE] = cursor
        inc["batch_cursor"] = cursors
    return inc
