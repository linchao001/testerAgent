"""intake 节点：需求条款确定性切分 + 歧义澄清（dd §7.5① §4.3 §20.2）。

流程（dd §7.5①）：

1. 确定性切分：按 Markdown ATX 标题（``##``~``######``）切分，忽略代码块/
   引用块内的标题行；无有效标题的文档整体为一个 clause（clause_id=``root``）；
2. clause_id 规则：标题路径各层的同级序号拼接（``h2-1-h3-2``），同级序号按
   "同标题层级、同父路径下出现次序" 1 起计数——同一文档重算结果恒等；
3. 每条款算 text_hash（规范化原文 sha1），写 ``requirement.clauses.json``
   （含字节偏移，可再生缓存）+ clauses 索引写 task 行（不含偏移与正文）；
4. 歧义检测（agent.config.ambiguity_check 可关，默认开）：LLM 判断需求是否
   缺少关键测试信息，有则 interrupt() 挂澄清（waiting_input）。

澄清恢复协议（dd §7.6 answer 行 + SP-2 结论）：

- 挂起前把问题落库为 message(role=assistant, kind=clarification_qa,
  payload={"questions": [...], "answered": false})——既是会话可见的提问
  记录，也是恢复轮的"已问过"凭据：节点被 ``Command(resume=answers)`` 重入
  时**跳过 LLM** 直接取回同一组问题，避免恢复轮再次 LLM 调用导致问题漂移
  或恢复失败；
- resume 值为答复列表 ``[{"id","answer"}, ...]``（与 questions 的 id 对应，
  WP-26 answer API 构造）；interrupt() 返回后节点把 answers 回填进该
  message（answered=true）——即 dd "回填 questions.answer"；
- 答复以 message 落库（answer API 写 QA 记录）后重跑 intake：需求原文未变，
  确定性切分保证条款 ID 保持（tech-design §3.2 / §4.2①）。

与设计偏离（WP-16 交接单登记）：§7.5① 未写问题落库凭据与恢复轮跳过 LLM 的
机制，本节点以"未答复的 clarification_qa message"作为挂起凭据实现；
ClauseSpan 为 ClauseRef + 字节偏移的运行期扩展（dd §4.3 许可，不入 DB）。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import TYPE_CHECKING, Any

from langgraph.types import interrupt

from ...domain import ClauseRef
from ...errors import AppError, LLMBadOutput
from ...logging_config import get_logger
from ...store.models import MessageRow

if TYPE_CHECKING:
    from ...runtime.context import TaskContext

logger = get_logger(__name__)

# ---------- 条款切分（纯函数，dd §7.5① 1-3） ----------

# ATX 标题：##~######（dd 明确 # 不参与切分）；标题非空
_HEADING_RE = re.compile(r"^(#{2,6})[ \t]+(.+?)[ \t]*$")
# 围栏代码块开始/结束行（缩进 ≤3 空格；``` 或 ~~~，长 ≥3）
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
# ATX 闭合序列（CommonMark：标题尾部 " ##"）
_CLOSING_HASH_RE = re.compile(r"[ \t]+#+[ \t]*$")
# 字节层尾部空白（UTF-8 多字节字符不含 ASCII 空白字节，按字节 rstrip 安全）
_TRAILING_WS = b" \t\r\n\x0b\x0c"

_ANCHOR_CHARS = 32
_ROOT_CLAUSE_ID = "root"


class ClauseSpan(ClauseRef):
    """ClauseRef + 原文字节偏移（dd §4.3 运行期扩展字段，不入 DB）。

    ``[start_offset, end_offset)`` 寻址 requirement.md 的 UTF-8 字节区间
    （含本条款标题行、去尾部空白），供 :meth:`FileStore.read_clause` 按需
    读条款原文。
    """

    start_offset: int
    end_offset: int

    def to_ref(self) -> dict[str, Any]:
        """task.clauses 索引形态（剥离运行期偏移字段）。"""
        return self.model_dump(exclude={"start_offset", "end_offset"})


def split_clauses(md: str) -> list[ClauseSpan]:
    """按 ATX 标题确定性切分需求正文（dd §7.5① 1-3）。

    - 边界：行首 ``##``~``######``（忽略围栏代码块与引用块内的行）；
      每个有效标题产出一个条款，条款正文 = 标题行起、至下一个层级 ≤ 自身
      的标题行止（含更深层级子标题的独立条款），去尾部空白；
    - 无有效标题（含空文档、仅 H1 文档）→ 单一 ``root`` 条款覆盖全文；
    - 输出按文档序；同一输入恒等输出（重算 clause_id 稳定）。
    """
    raw = md.encode("utf-8")
    boundaries = _find_headings(md, raw)
    if not boundaries:
        content = raw.rstrip(_TRAILING_WS)
        return [_make_span(_ROOT_CLAUSE_ID, 0, [], content, start=0)]

    spans: list[ClauseSpan] = []
    stack: list[tuple[int, str, str]] = []  # (level, clause_id 链, title)
    counter: dict[tuple[str, int], int] = {}

    for i, (start, level, title) in enumerate(boundaries):
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_chain = stack[-1][1] if stack else ""
        key = (parent_chain, level)
        counter[key] = counter.get(key, 0) + 1
        suffix = f"h{level}-{counter[key]}"
        clause_id = f"{parent_chain}-{suffix}" if parent_chain else suffix
        title_path = [s[2] for s in stack] + [title]
        stack.append((level, clause_id, title))

        end_raw = len(raw)
        for j in range(i + 1, len(boundaries)):
            if boundaries[j][1] <= level:
                end_raw = boundaries[j][0]
                break
        content = raw[start:end_raw].rstrip(_TRAILING_WS)
        spans.append(_make_span(clause_id, level, title_path, content, start=start))
    return spans


def _find_headings(md: str, raw: bytes) -> list[tuple[int, int, str]]:
    """扫描有效标题行，返回 [(字节起始偏移, level, title)]（文档序）。

    逐行推进字节偏移（UTF-8）；围栏代码块（```/~~~）与引用块（>）内的
    标题行不作为边界。
    """
    out: list[tuple[int, int, str]] = []
    offset = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in md.split("\n"):
        line_start = offset
        offset += len(line.encode("utf-8")) + 1  # +1 为换行；末行多计不越界使用

        m = _FENCE_RE.match(line)
        if m:
            ch, ln = m.group(1)[0], len(m.group(1))
            rest = line[m.end() :].strip()
            if not in_fence:
                in_fence, fence_char, fence_len = True, ch, ln
            elif ch == fence_char and ln >= fence_len and not rest:
                in_fence = False
            continue
        if in_fence:
            continue
        if line.lstrip().startswith(">"):
            continue

        hm = _HEADING_RE.match(line.rstrip())
        if hm:
            title = _CLOSING_HASH_RE.sub("", hm.group(2)).strip()
            if title:
                out.append((line_start, len(hm.group(1)), title))
    return out


def _root_span(content: bytes) -> ClauseSpan:
    return _make_span(_ROOT_CLAUSE_ID, 0, [], content, start=0)


def _make_span(
    clause_id: str, level: int, title_path: list[str], content: bytes, *, start: int
) -> ClauseSpan:
    text = content.decode("utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return ClauseSpan(
        clause_id=clause_id,
        level=level,
        title_path=title_path,
        anchor=" ".join(text.split())[:_ANCHOR_CHARS],
        text_hash=hashlib.sha1(normalized.encode("utf-8")).hexdigest(),
        status="active",
        start_offset=start,
        end_offset=start + len(content),
    )


# ---------- 歧义检测（dd §7.5① 4 / §20.2） ----------

_AMBIGUITY_PROMPT = "intake.ambiguity"
_AMBIGUITY_SCHEMA = {
    "type": "object",
    "required": ["clarifications"],
    "properties": {
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
        }
    },
}


async def intake_node(ctx: "TaskContext", state: dict) -> dict[str, Any]:
    """主图 intake 节点（dd §7.5①）；切分落库 + 歧义澄清 interrupt。

    幂等：切分确定性、缓存/索引重写恒等、恢复轮凭 message 凭据跳过 LLM。
    返回状态增量：``clauses``（ClauseRef dict 列表）与
    ``clarification_questions``。
    """
    if ctx.daos is None:
        raise AppError(
            "intake 节点缺少 ctx.daos（Runner 未注入 DAOs 组）",
            details={"node": "intake"},
        )
    task = ctx.task
    ws = task.workspace_id

    md = await ctx.files.read_requirement(ws, task.id)
    spans = split_clauses(md)
    refs = [s.to_ref() for s in spans]

    # 落缓存文件（含偏移，可再生）+ task.clauses 索引（dd §4.3 / §7.5① 3）
    await ctx.files.save_clauses_cache(ws, task.id, [s.model_dump() for s in spans])
    await ctx.daos.task.update_clauses(task.id, refs)

    questions = await _resolve_questions(ctx, spans)
    if questions:
        answers = interrupt({"node": "intake", "questions": questions})
        await _fill_answers(ctx, answers)
        logger.info(
            "intake clarification answered",
            extra={"task_id": task.id, "questions": len(questions)},
        )

    return {"clauses": refs, "clarification_questions": questions}


async def _resolve_questions(ctx: "TaskContext", spans: list[ClauseSpan]) -> list[dict]:
    """确定本轮澄清问题：恢复轮取挂起凭据，新轮按开关做 LLM 歧义检测。"""
    pending = await _find_pending_questions(ctx)
    if pending is not None:
        return pending  # 恢复/重入轮：跳过 LLM，问题与挂起时一致
    if not bool(ctx.agent_config.get("ambiguity_check", True)):
        return []
    questions = await _detect_ambiguity(ctx, spans)
    if questions:
        await _record_questions(ctx, questions)
    return questions


async def _find_pending_questions(ctx: "TaskContext") -> list[dict] | None:
    """取最近一条未答复的 clarification_qa message（挂起凭据）；无则 None。"""
    msgs = await ctx.daos.message.list_by_task(ctx.task.id, kind="clarification_qa")
    for msg in reversed(msgs):
        payload = _parse_payload(msg.payload)
        if not payload.get("answered") and payload.get("questions"):
            return _normalize_questions(payload["questions"], strict=False)
    return None


async def _detect_ambiguity(ctx: "TaskContext", spans: list[ClauseSpan]) -> list[dict]:
    """LLM 歧义检测（dd §20.2）：输入条款标题与摘要，输出澄清问题列表。"""
    from ...prompts.loader import PromptLoader

    tpl = PromptLoader(custom_dir=ctx.agent_config.get("prompts_dir")).load(
        _AMBIGUITY_PROMPT
    )
    clause_index = [
        {"clause_id": s.clause_id, "title_path": s.title_path, "anchor": s.anchor}
        for s in spans
    ]
    from ..tool_gather import append_tool_notes, gather_notes_for_ctx

    user_content = (
        "<requirement_clauses>\n"
        + json.dumps(clause_index, ensure_ascii=False)
        + "\n</requirement_clauses>"
    )
    notes = await gather_notes_for_ctx(
        ctx, "intake", "查看需求条款并识别歧义所需上下文"
    )
    user_content = append_tool_notes(user_content, notes)
    result = await ctx.app.llm.chat(
        [
            {"role": "system", "content": tpl.content.strip()},
            {"role": "user", "content": user_content},
        ],
        json_schema=_AMBIGUITY_SCHEMA,
    )
    return _parse_clarifications(result.content)


def _parse_clarifications(content: str) -> list[dict]:
    """解析 LLM 输出（§7.4⑤：结构非法即节点失败，绝不带半成品过检查点）。"""
    try:
        obj = json.loads(content)
    except Exception as e:
        raise LLMBadOutput(f"intake 歧义检测输出不是合法 JSON: {e}") from e
    if not isinstance(obj, dict) or not isinstance(obj.get("clarifications"), list):
        raise LLMBadOutput("intake 歧义检测输出缺少 clarifications 数组")
    return _normalize_questions(obj["clarifications"], strict=True)


def _normalize_questions(items: list, *, strict: bool) -> list[dict]:
    """归一为 [{"id","question","options"}]；strict 供 LLM 输出（非法即失败），
    非 strict 供挂起凭据回读（容忍历史数据）。"""
    out: list[dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            if strict:
                raise LLMBadOutput(f"clarifications[{i}] 不是对象")
            continue
        question = str(item.get("question", "")).strip()
        if not question:
            if strict:
                raise LLMBadOutput(f"clarifications[{i}].question 为空")
            continue
        options = [str(o).strip() for o in item.get("options") or []]
        out.append(
            {
                "id": str(item.get("id") or f"q-{i + 1}"),
                "question": question,
                "options": [o for o in options if o],
            }
        )
    return out


async def _record_questions(ctx: "TaskContext", questions: list[dict]) -> None:
    """提问落库：会话可见的提问记录 + 恢复轮凭据（answered=false）。"""
    task = ctx.task
    msg = MessageRow.create(
        id=uuid.uuid4().hex,
        conversation_id=task.conversation_id,
        role="assistant",
        kind="clarification_qa",
        content="\n".join(q["question"] for q in questions),
        task_id=task.id,
        payload={"questions": questions, "answered": False},
    )
    await ctx.daos.message.put(msg)


async def _fill_answers(ctx: "TaskContext", answers: Any) -> None:
    """恢复轮：把答复回填进挂起凭据 message（dd §7.6 "回填 questions.answer"）。"""
    msgs = await ctx.daos.message.list_by_task(ctx.task.id, kind="clarification_qa")
    for msg in reversed(msgs):
        payload = _parse_payload(msg.payload)
        if not payload.get("answered") and payload.get("questions"):
            payload["answered"] = True
            payload["answers"] = answers
            await ctx.daos.message.update_payload(msg.id, payload)
            return
    logger.warning(
        "intake 恢复轮未找到待回填的澄清 message（可能被并发答复）",
        extra={"task_id": ctx.task.id},
    )


def _parse_payload(raw: str) -> dict:
    try:
        obj = json.loads(raw) if raw else {}
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}
