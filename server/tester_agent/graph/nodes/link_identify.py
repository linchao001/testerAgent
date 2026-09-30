"""link_identify 节点：链路识别（dd §7.5② §7.4① §2.3 §20.3 §8.6）。

流程（dd §7.5②）：

1. 子图 index_line 档检索（``RETRIEVAL_PRESETS[link_identify]``：
   allowed_types=[LINK_INDEX]、inject_limit=200 硬上限，scope 为空——
   CP1 之前尚无已确认 link/story 集合）；意图文本 = 条款标题路径 + anchor
   拼接（dd §8.1）；
2. LLM 产出 LinkPlan（system.shared + link_identify.main 模板）；服务端
   **不信模型自造的临时 ID**：新增项统一改写为 ``new-link-{n}/new-story-{n}``
   （dd §7.4①），hit 项 ``entry_id`` 必须在注入白名单（outcome.injected）中，
   不在则降级 hit=False 并记 degraded（trace.degraded，dd §7.5②）；
3. 生成后 close_retrieval_trace 两写回填（referenced/hallucinated/weak，
   dd §8.6）；写 artifact(active, origin=system, confirmed_by=null)，
   图随后在静态断点 cp1_gate 挂起（Runner 置 waiting_confirm）。

澄清（dd §7.2 表"interrupt() 提问；cp1_gate"，R32 任意节点可挂 waiting_input）：
LLM 输出 clarifications 非空时，先把问题落库为
message(role=assistant, kind=clarification_qa,
payload={"node":"link_identify","questions","answered":false})——兼作会话
可见记录与恢复凭据（与 WP-16 intake 同范式）——再 interrupt()；恢复轮节点
重入时凭"未答复 message"立即重新挂起（不重跑检索/LLM），resume 拿到答复后
带答复做第二次生成；答复后仍提问按 LLM_BAD_OUTPUT 失败（不允许多轮挂起）。

与设计偏离（WP-17 交接单登记，均不碰冻结契约）：
- 新增纯函数 finalize_link_plan 承担"临时 ID 改写 + 白名单校验 + 归属回填"，
  hit 项的稳定 link_id/story_id 在索引镜像可用时以镜像为准（服务端权威），
  镜像为空（§8.4 降级）才退回模型回填值；entry_version 一律由注入条目回填
  （模型 schema 无该字段）；
- 白名单降级步骤经 TraceDAO.append_degraded（dd §3.3 冻结三方法之外的
  增量方法，WP-16 先例）并入同一阶段 trace 行；
- "需求前 N 字摘要"N 在 S3 标定前取模块常量 500；
- 崩溃重放：同 run 已存在 active artifact 时节点直接回放该产物（零 LLM/
  检索调用），避免节点重入产生重复版本。
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from langgraph.types import interrupt

from ...domain import DegradedStep, LinkPlan
from ...errors import AppError, LLMBadOutput
from ...logging_config import get_logger
from ...graph.constants import RETRIEVAL_PRESETS, STAGE_LINK_IDENTIFY
from ...context.retrieval.pipeline import close_retrieval_trace, retrieve_pipeline
from ...prompts.loader import PromptLoader
from ...store.models import ArtifactRow, MessageRow
from .intake import _parse_payload  # 复用凭据 JSON 容错解析（同包内私有约定）

if TYPE_CHECKING:
    from ...adapters.reme import IndexMirror
    from ...runtime.context import TaskContext

logger = get_logger(__name__)

# S3 未定 N 前的需求摘要长度占位（dd §7.4① "需求前 N 字摘要（S3 定 N）"）
_REQ_SUMMARY_CHARS = 500

_SYSTEM_PROMPT = "system.shared"
_MAIN_PROMPT = "link_identify.main"

_LINK_SCHEMA: dict = {
    "type": "object",
    "required": ["links", "stories", "new_suggestions"],
    "properties": {
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["link_id", "title", "summary", "hit", "confidence"],
                "properties": {
                    "link_id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "hit": {"type": "boolean"},
                    "entry_id": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
            },
        },
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "story_id", "link_id", "title", "summary", "hit",
                    "confidence", "rationale", "related_clause_ids",
                ],
                "properties": {
                    "story_id": {"type": "string"},
                    "link_id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "hit": {"type": "boolean"},
                    "entry_id": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                    "related_clause_ids": {
                        "type": "array", "items": {"type": "string"}
                    },
                },
            },
        },
        "new_suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "suggested_link_title", "suggested_story_title",
                    "reason", "source_clause_ids",
                ],
                "properties": {
                    "suggested_link_title": {"type": "string"},
                    "suggested_story_title": {"type": "string"},
                    "reason": {"type": "string"},
                    "source_clause_ids": {
                        "type": "array", "items": {"type": "string"}
                    },
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


# ---------- 纯函数：意图 / 知识块 ----------


def build_link_intent(clauses: list[dict]) -> str:
    """检索意图：需求条款标题路径 + anchor 拼接（dd §8.1）。"""
    parts: list[str] = []
    for c in clauses:
        title_path = " / ".join(c.get("title_path") or [])
        anchor = (c.get("anchor") or "").strip()
        line = f"{title_path}：{anchor}" if title_path else anchor
        line = line.strip("：").strip()
        if line:
            parts.append(line)
    return "\n".join(parts)


def render_knowledge_block(items: list, mirror: "IndexMirror | None") -> str:
    """渲染注入知识块：每条带 [ID]，镜像可用时附类型与归属链路（dd §20.3）。

    items 为 assemble 后按 position 排序的 InjectedItem；index_line 档
    passage = 标题 + 一句话摘要（镜像为空时仅标题，ops_b 已退化）。
    """
    lines = ["<knowledge>"]
    use_meta = mirror is not None and not mirror.is_empty
    for it in items:
        head = f"[ID:{it.entry_id}]"
        if use_meta:
            meta = mirror.meta(it.entry_id)
            if meta is not None:
                kind = "链路" if meta.kind == "link" else "故事"
                attr = f"（类型：link_index；{kind}：{meta.link_id}"
                if meta.story_id:
                    attr += f"；故事ID：{meta.story_id}"
                head += attr + "）"
        body = (it.passage or it.title or "").strip()
        lines.append(f"{head}\n{body}")
    lines.append("</knowledge>")
    return "\n".join(lines)


def render_user_message(
    clauses: list[dict], requirement_summary: str, knowledge_block: str
) -> str:
    """link_identify 生成调用 user 消息（dd §7.4① 输入三件套）。"""
    clause_index = [
        {
            "clause_id": c.get("clause_id"),
            "title_path": c.get("title_path") or [],
            "anchor": c.get("anchor") or "",
        }
        for c in clauses
    ]
    return (
        "<requirement_clauses>\n"
        + json.dumps(clause_index, ensure_ascii=False)
        + "\n</requirement_clauses>\n"
        + "<requirement_summary>\n"
        + requirement_summary
        + "\n</requirement_summary>\n"
        + knowledge_block
    )


# ---------- 纯函数：临时 ID 改写 + 白名单校验（dd §7.4① §7.5②） ----------


def finalize_link_plan(
    obj: Any,
    *,
    whitelist: set[str],
    entry_versions: dict[str, str],
    mirror: "IndexMirror | None" = None,
) -> tuple[LinkPlan, list[DegradedStep], list[str]]:
    """LLM 产物 → 冻结 LinkPlan；返回 (plan, 降级步骤, 模型断言命中的 entry_id)。

    - 新增（hit=False）与白名单外的"伪命中"：link/story ID 服务端改写为
      ``new-link-{n}/new-story-{n}``（不信模型自造临时 ID）；
    - 真命中：entry_version 由注入条目回填；稳定 ID 优先取镜像权威值，
      镜像缺失时退回模型回填值；
    - story.link_id 经"模型 link_id → 最终 link_id"映射改写，悬空引用直接
      LLM_BAD_OUTPUT（结构非法，§7.4⑤ 不带半成品过检查点）；
    - 第三返回值为模型在 hit 项上断言的全部 entry_id（校验前），供
      close_loop 对账 referenced/hallucinated。
    """
    if not isinstance(obj, dict):
        raise LLMBadOutput("link_identify 输出不是 JSON 对象")
    raw_links = obj.get("links")
    raw_stories = obj.get("stories")
    raw_suggestions = obj.get("new_suggestions") or []
    if not isinstance(raw_links, list) or not isinstance(raw_stories, list):
        raise LLMBadOutput("link_identify 输出缺少 links/stories 数组")
    if not isinstance(raw_suggestions, list):
        raise LLMBadOutput("link_identify 输出 new_suggestions 不是数组")

    degraded: list[DegradedStep] = []
    asserted: list[str] = []

    # ① links：白名单裁决 + 临时 ID 改写
    final_links = []
    link_id_map: dict[str, str] = {}
    new_link_no = 0
    for i, raw in enumerate(raw_links):
        _require_obj(raw, "links", i)
        title = _require_title(raw, "links", i)
        summary = _str_field(raw, "summary", "links", i)
        confidence = _num_field(raw, "confidence", "links", i)
        raw_link_id = str(raw.get("link_id") or "").strip()
        hit = bool(raw.get("hit"))
        eid = raw.get("entry_id")
        if hit and isinstance(eid, str) and eid:
            asserted.append(eid)

        if hit and isinstance(eid, str) and eid in whitelist:
            final_id = _authoritative_link_id(mirror, eid, raw_link_id)
            link = _link_ref(
                final_id, title, summary, hit=True,
                entry_id=eid, entry_version=entry_versions.get(eid),
                confidence=confidence,
            )
        else:
            if hit:
                bad = eid if isinstance(eid, str) and eid else "(empty)"
                degraded.append(
                    DegradedStep(
                        step="link_identify.entry_whitelist",
                        reason=f"entry_not_injected:{bad}",
                        fallback="downgrade_hit_to_new",
                    )
                )
            new_link_no += 1
            final_id = f"new-link-{new_link_no}"
            link = _link_ref(
                final_id, title, summary, hit=False,
                entry_id=None, entry_version=None, confidence=confidence,
            )
        final_links.append(link)
        # 模型自造 ID 与最终 ID 都登记映射，供 stories 归属改写
        if raw_link_id:
            link_id_map[raw_link_id] = final_id
        link_id_map[final_id] = final_id

    # ② stories：归属映射 + 同样的裁决/改写
    final_stories = []
    new_story_no = 0
    link_story_ids: dict[str, list[str]] = {l.link_id: [] for l in final_links}
    for i, raw in enumerate(raw_stories):
        _require_obj(raw, "stories", i)
        title = _require_title(raw, "stories", i)
        summary = _str_field(raw, "summary", "stories", i)
        confidence = _num_field(raw, "confidence", "stories", i)
        rationale = _str_field(raw, "rationale", "stories", i)
        clause_ids = _str_list(raw, "related_clause_ids", "stories", i)
        parent_raw = str(raw.get("link_id") or "").strip()
        parent = link_id_map.get(parent_raw)
        if parent is None:
            raise LLMBadOutput(
                f"stories[{i}].link_id 引用了不存在的 link: {parent_raw!r}"
            )
        hit = bool(raw.get("hit"))
        eid = raw.get("entry_id")
        if hit and isinstance(eid, str) and eid:
            asserted.append(eid)

        if hit and isinstance(eid, str) and eid in whitelist:
            story_id = _authoritative_story_id(mirror, eid, str(raw.get("story_id") or ""))
            story = _story_ref(
                story_id, parent, title, summary, hit=True,
                entry_id=eid, entry_version=entry_versions.get(eid),
                confidence=confidence, rationale=rationale, clause_ids=clause_ids,
            )
        else:
            if hit:
                bad = eid if isinstance(eid, str) and eid else "(empty)"
                degraded.append(
                    DegradedStep(
                        step="link_identify.entry_whitelist",
                        reason=f"entry_not_injected:{bad}",
                        fallback="downgrade_hit_to_new",
                    )
                )
            new_story_no += 1
            story_id = f"new-story-{new_story_no}"
            story = _story_ref(
                story_id, parent, title, summary, hit=False,
                entry_id=None, entry_version=None, confidence=confidence,
                rationale=rationale, clause_ids=clause_ids,
            )
        final_stories.append(story)
        link_story_ids[parent].append(story_id)

    for link in final_links:
        link.story_ids = list(link_story_ids[link.link_id])

    suggestions = _build_suggestions(raw_suggestions)
    return (
        LinkPlan(links=final_links, stories=final_stories,
                 new_suggestions=suggestions),
        degraded,
        asserted,
    )


def _link_ref(link_id, title, summary, *, hit, entry_id, entry_version, confidence):
    from ...domain import LinkRef

    return LinkRef(
        link_id=link_id, title=title, summary=summary, hit=hit,
        entry_id=entry_id, entry_version=entry_version, confidence=confidence,
    )


def _story_ref(
    story_id, link_id, title, summary, *, hit, entry_id, entry_version,
    confidence, rationale, clause_ids,
):
    from ...domain import StoryRef

    return StoryRef(
        story_id=story_id, link_id=link_id, title=title, summary=summary,
        hit=hit, entry_id=entry_id, entry_version=entry_version,
        confidence=confidence, rationale=rationale,
        related_clause_ids=list(clause_ids),
    )


def _build_suggestions(raw: list) -> list:
    from ...domain import NewLinkSuggestion

    out = []
    for i, item in enumerate(raw):
        _require_obj(item, "new_suggestions", i)
        try:
            out.append(
                NewLinkSuggestion(
                    suggested_link_title=str(item.get("suggested_link_title") or ""),
                    suggested_story_title=str(item.get("suggested_story_title") or ""),
                    reason=str(item.get("reason") or ""),
                    source_clause_ids=_str_list(
                        item, "source_clause_ids", "new_suggestions", i
                    ),
                )
            )
        except Exception as e:  # pragma: no cover - 显式字段均已归一，理论不可达
            raise LLMBadOutput(f"new_suggestions[{i}] 结构非法: {e}") from e
    return out


def _authoritative_link_id(
    mirror: "IndexMirror | None", entry_id: str, raw_link_id: str
) -> str:
    if mirror is not None and not mirror.is_empty:
        meta = mirror.meta(entry_id)
        if meta is not None and meta.link_id:
            return meta.link_id
    return raw_link_id or entry_id


def _authoritative_story_id(
    mirror: "IndexMirror | None", entry_id: str, raw_story_id: str
) -> str:
    if mirror is not None and not mirror.is_empty:
        meta = mirror.meta(entry_id)
        if meta is not None and meta.story_id:
            return meta.story_id
    return raw_story_id.strip() or entry_id


def _require_obj(raw: Any, name: str, i: int) -> dict:
    if not isinstance(raw, dict):
        raise LLMBadOutput(f"{name}[{i}] 不是对象")
    return raw


def _require_title(raw: dict, name: str, i: int) -> str:
    title = str(raw.get("title") or "").strip()
    if not title:
        raise LLMBadOutput(f"{name}[{i}].title 为空")
    return title


def _str_field(raw: dict, key: str, name: str, i: int) -> str:
    val = raw.get(key)
    if not isinstance(val, str):
        raise LLMBadOutput(f"{name}[{i}].{key} 必须是字符串")
    return val


def _num_field(raw: dict, key: str, name: str, i: int) -> float:
    val = raw.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise LLMBadOutput(f"{name}[{i}].{key} 必须是数字")
    return float(val)


def _str_list(raw: dict, key: str, name: str, i: int) -> list[str]:
    val = raw.get(key)
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise LLMBadOutput(f"{name}[{i}].{key} 必须是字符串数组")
    return list(val)


# ---------- 节点 ----------


async def link_identify_node(ctx: "TaskContext", state: dict) -> dict[str, Any]:
    """主图 link_identify 节点（dd §7.5②）；返回 link_plan 状态增量。"""
    if ctx.daos is None or ctx.daos.artifact is None:
        raise AppError(
            "link_identify 节点缺少 ctx.daos.artifact（Runner 未注入 DAOs 组）",
            details={"node": STAGE_LINK_IDENTIFY},
        )
    task = ctx.task
    artifact_dao = ctx.daos.artifact
    run_id = task.graph_run_id or ctx.run_id

    clauses = (state or {}).get("clauses") or [
        c.model_dump() for c in task.clauses_obj()
    ]
    if not clauses:
        raise AppError(
            "link_identify 缺少 clauses（intake 未执行或 task.clauses 为空）",
            details={"node": STAGE_LINK_IDENTIFY},
        )

    # 崩溃重放：同 run 已写 active 产物 → 直接回放（零检索/LLM，不造重复版本）
    existing = await artifact_dao.get_active(task.id, STAGE_LINK_IDENTIFY)
    if existing is not None and existing.graph_run_id == run_id:
        logger.info(
            "link_identify replay existing artifact",
            extra={"task_id": task.id, "artifact_id": existing.id},
        )
        return _plan_increment(existing.payload_dict(), existing.stage_version, state)

    version = await artifact_dao.next_version(task.id, STAGE_LINK_IDENTIFY)

    # 澄清恢复凭据（与 intake 同范式；payload.node 区分节点）
    answers: Any = None
    pending = await _find_pending_questions(ctx)
    if pending is not None:
        answers = interrupt({"node": STAGE_LINK_IDENTIFY, "questions": pending})
        await _fill_answers(ctx, answers)

    # ① 子图 index_line 档检索（CP1 前无确认集合，scope=None）
    intent = build_link_intent(clauses)
    outcome = await retrieve_pipeline(
        ctx,
        RETRIEVAL_PRESETS[STAGE_LINK_IDENTIFY],
        intent,
        scope=None,
        stage_version=version,
    )

    # ② LLM 产出 LinkPlan（必要时挂澄清，答复后二次生成）
    loader = PromptLoader(custom_dir=ctx.agent_config.get("prompts_dir"))
    system = (
        loader.load(_SYSTEM_PROMPT).content.strip()
        + "\n\n"
        + loader.load(_MAIN_PROMPT).content.strip()
    )
    from ..tool_gather import append_tool_notes, gather_notes_for_ctx

    requirement_md = await ctx.files.read_requirement(task.workspace_id, task.id)
    user_content = render_user_message(
        clauses,
        requirement_md[:_REQ_SUMMARY_CHARS],
        render_knowledge_block(outcome.items, ctx.mirror),
    )
    notes = await gather_notes_for_ctx(
        ctx, "link_identify", "查看工作区需求与知识，辅助识别业务链路"
    )
    user_content = append_tool_notes(user_content, notes)
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    raw_content, obj = await _generate(ctx, messages, had_pending=answers is not None)

    # ③ 服务端改写 + 白名单校验
    versions = {it.entry_id: it.entry_version for it in outcome.items}
    plan, plan_degraded, asserted = finalize_link_plan(
        obj,
        whitelist=set(outcome.injected),
        entry_versions=versions,
        mirror=ctx.mirror,
    )

    # ④ 引用闭环两写（dd §8.6）：模型断言 ID 渲染为标签文本，使裸 entry_id
    #    字段也能进入 referenced/hallucinated 对账（raw JSON 同时传入兜底）。
    asserted_tags = " ".join(f"[ID:{eid}]" for eid in asserted)
    await close_retrieval_trace(ctx, outcome, [raw_content, asserted_tags])
    if plan_degraded:
        await _trace_dao(ctx).append_degraded(outcome.trace_id, plan_degraded)

    # ⑤ artifact 落库（active / system / confirmed_by=null；事务内复查防重）
    async with ctx.app.db.immediate_tx():
        guard = await artifact_dao.get_active(task.id, STAGE_LINK_IDENTIFY)
        if guard is not None and guard.graph_run_id == run_id:
            return _plan_increment(guard.payload_dict(), guard.stage_version, state)
        artifact_id = uuid.uuid4().hex
        await artifact_dao.put(
            ArtifactRow.create(
                id=artifact_id,
                task_id=task.id,
                stage=STAGE_LINK_IDENTIFY,
                graph_run_id=run_id,
                stage_version=version,
                payload=plan,
                origin="system",
                status="active",
                confirmed_by=None,
            )
        )

    logger.info(
        "link_identify artifact written",
        extra={
            "task_id": task.id,
            "version": version,
            "links": len(plan.links),
            "stories": len(plan.stories),
            "downgraded": len(plan_degraded),
        },
    )
    return _plan_increment(plan.model_dump(), version, state)


def _plan_increment(
    plan_dict: dict, version: int, state: dict | None
) -> dict[str, Any]:
    versions = dict((state or {}).get("current_stage_version") or {})
    versions[STAGE_LINK_IDENTIFY] = version
    return {"link_plan": plan_dict, "current_stage_version": versions}


def _trace_dao(ctx: "TaskContext"):
    from ...store.models import TraceDAO

    return TraceDAO(ctx.app.db)


# ---------- 生成 + 澄清 ----------


async def _generate(
    ctx: "TaskContext", messages: list[dict], *, had_pending: bool
) -> tuple[str, dict]:
    """首次生成；clarifications 非空则挂澄清并用答复二次生成。

    had_pending=True 表示本轮是恢复轮（凭据已在节点入口 interrupt 取答复）：
    首次调用若仍只输出问题，按 LLM_BAD_OUTPUT 失败，不允许多轮挂起。
    """
    result = await ctx.app.llm.chat(messages, json_schema=_LINK_SCHEMA)
    raw = result.content
    obj = _parse_object(raw)
    questions = _questions(obj)
    if not questions:
        return raw, obj
    if had_pending:
        raise LLMBadOutput("link_identify 在澄清答复后仍输出 clarifications")

    await _record_questions(ctx, questions)
    answers = interrupt({"node": STAGE_LINK_IDENTIFY, "questions": questions})
    await _fill_answers(ctx, answers)

    messages.append(
        {"role": "assistant", "content": json.dumps({"clarifications": questions},
                                                    ensure_ascii=False)}
    )
    messages.append(
        {
            "role": "user",
            "content": (
                "<clarification_answers>\n"
                + json.dumps(answers, ensure_ascii=False)
                + "\n</clarification_answers>\n"
                "请基于以上答复输出完整 LinkPlan（同一 JSON Schema）。"
            ),
        }
    )
    result2 = await ctx.app.llm.chat(messages, json_schema=_LINK_SCHEMA)
    raw2 = result2.content
    obj2 = _parse_object(raw2)
    if _questions(obj2):
        raise LLMBadOutput("link_identify 在澄清答复后仍输出 clarifications")
    return raw2, obj2


def _parse_object(content: str) -> dict:
    try:
        obj = json.loads(content)
    except Exception as e:
        raise LLMBadOutput(f"link_identify 输出不是合法 JSON: {e}") from e
    if not isinstance(obj, dict):
        raise LLMBadOutput("link_identify 输出不是 JSON 对象")
    return obj


def _questions(obj: dict) -> list[dict]:
    items = obj.get("clarifications")
    if not items:
        return []
    if not isinstance(items, list):
        raise LLMBadOutput("link_identify 输出 clarifications 不是数组")
    out: list[dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise LLMBadOutput(f"clarifications[{i}] 不是对象")
        question = str(item.get("question") or "").strip()
        if not question:
            raise LLMBadOutput(f"clarifications[{i}].question 为空")
        options = [str(o).strip() for o in item.get("options") or []]
        out.append(
            {
                "id": str(item.get("id") or f"q-{i + 1}"),
                "question": question,
                "options": [o for o in options if o],
            }
        )
    return out


# ---------- 澄清凭据 message（payload.node=link_identify 与 intake 区分） ----------


async def _find_pending_questions(ctx: "TaskContext") -> list[dict] | None:
    msgs = await ctx.daos.message.list_by_task(
        ctx.task.id, kind="clarification_qa"
    )
    for msg in reversed(msgs):
        payload = _parse_payload(msg.payload)
        if (
            not payload.get("answered")
            and payload.get("node") == STAGE_LINK_IDENTIFY
            and payload.get("questions")
        ):
            return list(payload["questions"])
    return None


async def _record_questions(ctx: "TaskContext", questions: list[dict]) -> None:
    task = ctx.task
    msg = MessageRow.create(
        id=uuid.uuid4().hex,
        conversation_id=task.conversation_id,
        role="assistant",
        kind="clarification_qa",
        content="\n".join(q["question"] for q in questions),
        task_id=task.id,
        payload={
            "node": STAGE_LINK_IDENTIFY,
            "questions": questions,
            "answered": False,
        },
    )
    await ctx.daos.message.put(msg)


async def _fill_answers(ctx: "TaskContext", answers: Any) -> None:
    msgs = await ctx.daos.message.list_by_task(
        ctx.task.id, kind="clarification_qa"
    )
    for msg in reversed(msgs):
        payload = _parse_payload(msg.payload)
        if (
            not payload.get("answered")
            and payload.get("node") == STAGE_LINK_IDENTIFY
            and payload.get("questions")
        ):
            payload["answered"] = True
            payload["answers"] = answers
            await ctx.daos.message.update_payload(msg.id, payload)
            return
    logger.warning(
        "link_identify 恢复轮未找到待回填的澄清 message（可能被并发答复）",
        extra={"task_id": ctx.task.id},
    )
