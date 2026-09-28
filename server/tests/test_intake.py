"""WP-16 intake 条款切分（dd §7.5① §4.3 §20.2）。

验收口径（WBS #16）：
- "重算 clause_id 稳定"：同一文档重复切分恒等；恢复轮跳过 LLM 直接取回
  挂起凭据中的同一组问题（chat_calls 不增）；
- "无标题文档 root 兜底"：空文档 / 仅 H1 文档 / 无 ATX 标题文档 → root。

另覆盖：多级 id/title_path、同级序号（不同父重计）、围栏/引用块标题忽略、
字节偏移与 read_clause 对拍（中文 UTF-8）、ATX 闭合序列、歧义 interrupt/
answer 恢复、ambiguity_check 关闭、LLM 坏输出节点失败、挂起凭据幂等重入、
clauses 缓存文件可再生、task.clauses 中文 JSON 往返。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester_agent.errors import AppError, NotFoundError
from tester_agent.graph.nodes import intake_node, split_clauses
from tester_agent.graph.state import TaskState
from tester_agent.graph.wrap import wrap
from tester_agent.runtime.context import AppContext, DAOs, TaskContext
from tester_agent.store.db import Database, run_migrations
from tester_agent.store.models import (
    ConfigDAO,
    MessageDAO,
    MessageRow,
    TaskDAO,
    TaskRow,
    WorkspaceDAO,
    WorkspaceRow,
)
from tester_agent.store.workspace_files import FileStore

from fakes import FakeLLM

WS = "ws1"
TASK = "task1"
CONV = "conv1"

MD = (
    "# 支付系统\n"
    "\n"
    "总体说明。\n"
    "\n"
    "## 支付流程\n"
    "\n"
    "创建订单后发起支付，支持余额与银行卡。\n"
    "\n"
    "### 退款\n"
    "\n"
    "超时未发货自动全额退款。\n"
    "\n"
    "### 超时处理\n"
    "\n"
    "支付超时 30 分钟自动关闭订单。\n"
    "\n"
    "## 对账\n"
    "\n"
    "T+1 生成对账单。\n"
)

ANSWERS = [{"id": "q-1", "answer": "入口为 App 内 H5"}, {"id": "q-2", "answer": "仅国内手机号"}]


# ---------- 切分器纯函数 ----------


def test_split_ids_title_paths_and_sibling_numbers():
    spans = split_clauses(MD)
    assert [(s.clause_id, s.level) for s in spans] == [
        ("h2-1", 2), ("h2-1-h3-1", 3), ("h2-1-h3-2", 3), ("h2-2", 2),
    ]
    assert spans[0].title_path == ["支付流程"]
    assert spans[1].title_path == ["支付流程", "退款"]
    assert spans[3].title_path == ["对账"]


def test_split_sibling_renumber_under_different_parent():
    md = "## A\n\n### 子\n\nx\n\n## B\n\n### 子\n\ny\n"
    assert [s.clause_id for s in split_clauses(md)] == [
        "h2-1", "h2-1-h3-1", "h2-2", "h2-2-h3-1",
    ]


def test_split_ignores_fence_and_quote_headings():
    md = (
        "## A\n\n```python\n## 代码块内不算\n~~~\n也不是\n```\n\n"
        "> ## 引用内不算\n> 正文\n\n正文 A。\n\n## B\n\n内容。\n"
    )
    spans = split_clauses(md)
    assert [s.clause_id for s in spans] == ["h2-1", "h2-2"]
    # 围栏与引用行归属所在条款正文，偏移回读含原行
    raw = md.encode("utf-8")
    body = raw[spans[0].start_offset : spans[0].end_offset].decode()
    assert "## 代码块内不算" in body and "> ## 引用内不算" in body


async def test_split_offsets_roundtrip_with_read_clause(tmp_path):
    store = FileStore(tmp_path / "data")
    await store.save_requirement(WS, TASK, MD)
    spans = split_clauses(MD)
    for s in spans:
        text = await store.read_clause(WS, TASK, (s.start_offset, s.end_offset))
        # 偏移寻址原文（含标题行、去尾部空白），且条款文本与切分输入一致
        assert text == text.rstrip()
        assert s.text_hash
    # 中文标题行首字节对齐：首个条款恰从 "## 支付流程" 行首开始
    assert MD.encode("utf-8")[spans[0].start_offset :].startswith("## 支付流程".encode())


def test_split_root_fallback():
    for md in ("", "\n\n", "# 只有 H1\n\n正文。\n", "无任何标题的纯段落。"):
        (span,) = split_clauses(md)
        assert span.clause_id == "root"
        assert span.level == 0 and span.title_path == []
    # 仅 H1 文档：root 覆盖去尾空白后的全文
    md1 = "# 标题\n\n正文。"
    (span,) = split_clauses(md1)
    assert (span.start_offset, span.end_offset) == (0, len(md1.encode("utf-8")))


def test_split_recompute_stable_and_hash_changes():
    assert [s.model_dump() for s in split_clauses(MD)] == [
        s.model_dump() for s in split_clauses(MD)
    ]  # 重算 clause_id/hash/偏移全部稳定
    # 标题结构不变、正文变化：ID 稳定、hash 变化
    changed = MD.replace("创建订单后发起支付，支持余额与银行卡。", "创建订单后发起支付。")
    old = {s.clause_id: s.text_hash for s in split_clauses(MD)}
    new = {s.clause_id: s.text_hash for s in split_clauses(changed)}
    assert set(old) == set(new)
    assert old["h2-1"] != new["h2-1"]


def test_split_closing_hash_h1_and_level_jump():
    spans = split_clauses("## foo ##\n\nx\n\n## C#\n\ny\n\n#### 深层\n\nz\n")
    assert [(s.clause_id, s.title_path) for s in spans] == [
        ("h2-1", ["foo"]), ("h2-2", ["C#"]), ("h2-2-h4-1", ["C#", "深层"]),
    ]
    # H1 与 7 个 # 均非切分边界，归入后续条款正文
    spans = split_clauses("# 一级\n\n####### 七个\n\n## 真标题\n\n内容")
    assert [s.clause_id for s in spans] == ["h2-1"]


def test_split_anchor_first_32_chars():
    long_line = "## " + "很" * 60
    (span,) = split_clauses(long_line + "\n\n正文")
    assert len(span.anchor) == 32 and span.anchor.startswith("## 很")


# ---------- 节点图级测试 ----------


@dataclass
class Stack:
    ctx: TaskContext
    db: Database
    store: FileStore
    llm: FakeLLM
    saver: AsyncSqliteSaver
    conn: aiosqlite.Connection


async def _link_identify_passthrough(ctx, state) -> dict:
    return {}


def _graph(stack: Stack):
    """单测专用：仅挂 intake 节点（生产拓扑为控制环）。"""
    g = StateGraph(TaskState)
    g.add_node("intake", wrap(intake_node, name="intake"))
    g.add_edge(START, "intake")
    g.add_edge("intake", END)
    return g.compile(checkpointer=stack.saver)


def _cfg(stack: Stack, thread: str = "th-1") -> dict:
    return {"configurable": {"thread_id": thread, "ctx": stack.ctx}}


@pytest.fixture()
async def make_stack(tmp_path):
    handles: list[tuple[Database, aiosqlite.Connection]] = []

    async def build(
        *, md: str = MD, llm: FakeLLM | None = None, agent_config: dict | None = None
    ) -> Stack:
        db_path = tmp_path / f"app-{len(handles)}.db"
        run_migrations(db_path)
        db = Database(db_path)

        await WorkspaceDAO(db).create(
            WorkspaceRow.create(id=WS, name="ws-name", kb_config={"kb_id": "KB1", "options": {}})
        )
        await db.aexecute(
            "INSERT INTO conversation (id, workspace_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (CONV, WS, "t", "2026-09-26T00:00:00.000Z", "2026-09-26T00:00:00.000Z"),
        )
        await TaskDAO(db).create(
            TaskRow.create(
                id=TASK, conversation_id=CONV, workspace_id=WS, status="running",
                current_stage="intake", langgraph_thread_id="thread-1",
                graph_run_id="run-1", snapshot_level="off",
            )
        )
        store = FileStore(tmp_path / "data")
        await store.save_requirement(WS, TASK, md)
        llm = llm or FakeLLM([json.dumps({"clarifications": []})])
        app = AppContext(
            db=db, file_store=store, llm=llm, reme_factory=None, config=ConfigDAO(db)
        )
        ctx = TaskContext(
            app=app, task=await TaskDAO(db).get(TASK), run_id="run-1",
            files=store, reader=None, snapshot_level="off", mirror=None,
            agent_config=agent_config if agent_config is not None else {},
            daos=DAOs(task=TaskDAO(db), message=MessageDAO(db)),
        )
        conn = await aiosqlite.connect(
            str(tmp_path / f"ckpt-{len(handles)}.db"), check_same_thread=False
        )
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        handles.append((db, conn))
        return Stack(ctx=ctx, db=db, store=store, llm=llm, saver=saver, conn=conn)

    yield build
    for db, conn in handles:
        db.close()
        await conn.close()


async def _task_clauses(stack: Stack) -> list[dict]:
    row = await TaskDAO(stack.db).get(TASK)
    return json.loads(row.clauses)


def _ambig(questions: list[dict]) -> str:
    return json.dumps({"clarifications": questions}, ensure_ascii=False)


async def test_intake_no_ambiguity_persists_and_proceeds(make_stack):
    stack = await make_stack()
    graph = _graph(stack)
    result = await graph.ainvoke({}, _cfg(stack))

    state = await graph.aget_state(_cfg(stack))
    assert state.next == ()  # intake 完成
    refs = result["clauses"]
    assert [c["clause_id"] for c in refs] == ["h2-1", "h2-1-h3-1", "h2-1-h3-2", "h2-2"]
    assert result["clarification_questions"] == []
    assert stack.llm.chat_calls == 1  # 仅歧义检测一次
    # task.clauses 与状态一致（无偏移字段）
    db_refs = await _task_clauses(stack)
    assert db_refs == refs
    assert all("start_offset" not in c and "end_offset" not in c for c in db_refs)
    # 缓存文件含偏移、中文不转义
    cache = await stack.store.read_clauses_cache(WS, TASK)
    assert [c["clause_id"] for c in cache] == [c["clause_id"] for c in refs]
    expect_start = MD.encode("utf-8").index("## 支付流程".encode())
    assert cache[0]["start_offset"] == expect_start and cache[0]["end_offset"] > expect_start
    raw_file = stack.store.root / "workspaces" / WS / TASK / "requirement.clauses.json"
    assert "支付流程".encode() in raw_file.read_bytes()
    # 无澄清 message
    assert await MessageDAO(stack.db).list_by_task(TASK, kind="clarification_qa") == []


async def test_intake_ambiguity_interrupts_with_pending_message(make_stack):
    questions = [
        {"question": "需求缺少触发入口：用户从哪里发起支付？", "options": ["App", "Web"]},
        {"question": "缺少前置数据约束：账户需要实名吗？", "options": []},
    ]
    stack = await make_stack(llm=FakeLLM([_ambig(questions)]))
    graph = _graph(stack)
    await graph.ainvoke({}, _cfg(stack))

    assert stack.llm.chat_calls == 1
    state = await graph.aget_state(_cfg(stack))
    assert state.next == ("intake",)  # 函数式中断挂在 intake 节点内
    payload = state.tasks[0].interrupts[0].value
    assert payload["node"] == "intake"
    assert [q["id"] for q in payload["questions"]] == ["q-1", "q-2"]
    assert payload["questions"][0]["options"] == ["App", "Web"]
    # 切分已先于提问落库（task.clauses + 缓存文件）
    assert len(await _task_clauses(stack)) == 4
    assert len(await stack.store.read_clauses_cache(WS, TASK)) == 4
    # 提问 message 落库（恢复凭据：answered=false）
    msgs = await MessageDAO(stack.db).list_by_task(TASK, kind="clarification_qa")
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.role == "assistant" and msg.task_id == TASK
    mp = json.loads(msg.payload)
    assert mp["answered"] is False and mp["questions"] == payload["questions"]
    assert "触发入口" in msg.content


async def test_intake_resume_skips_llm_and_fills_answers(make_stack):
    questions = [{"question": "缺少入口描述？", "options": []}]
    stack = await make_stack(llm=FakeLLM([_ambig(questions), _ambig(questions)]))
    graph = _graph(stack)
    cfg = _cfg(stack)
    await graph.ainvoke({}, cfg)
    assert stack.llm.chat_calls == 1

    await graph.ainvoke(Command(resume=ANSWERS), cfg)
    # 关键（验收"重算 clause_id 稳定"的恢复侧）：恢复轮跳过 LLM，问题取自挂起凭据
    assert stack.llm.chat_calls == 1
    state = await graph.aget_state(cfg)
    assert state.next == ()
    assert state.values["clarification_questions"][0]["id"] == "q-1"
    assert len(state.values["clauses"]) == 4
    # 答复回填进挂起 message（dd §7.6 "回填 questions.answer"）
    msgs = await MessageDAO(stack.db).list_by_task(TASK, kind="clarification_qa")
    assert len(msgs) == 1
    mp = json.loads(msgs[0].payload)
    assert mp["answered"] is True and mp["answers"] == ANSWERS


async def test_intake_reinterrupt_with_pending_message_pre_seeded(make_stack):
    """挂起凭据已存在但无 pending interrupt（节点在 interrupt 前崩溃重入）：
    跳过 LLM、以同一组问题重新挂起。"""
    seeded = [{"id": "q-9", "question": "历史未答复问题？", "options": ["A"]}]
    stack = await make_stack()  # LLM 脚本为空：任何调用都会 LLMBadOutput
    await MessageDAO(stack.db).put(
        MessageRow.create(
            id="msg-seed-1", conversation_id=CONV, role="assistant",
            kind="clarification_qa", content="历史未答复问题？", task_id=TASK,
            payload={"questions": seeded, "answered": False},
        )
    )
    graph = _graph(stack)
    await graph.ainvoke({}, _cfg(stack))
    assert stack.llm.chat_calls == 0
    state = await graph.aget_state(_cfg(stack))
    assert state.next == ("intake",)
    assert state.tasks[0].interrupts[0].value["questions"] == seeded


async def test_intake_ambiguity_disabled(make_stack):
    stack = await make_stack(agent_config={"ambiguity_check": False})
    graph = _graph(stack)
    await graph.ainvoke({}, _cfg(stack))
    assert stack.llm.chat_calls == 0  # 检测关闭：零 LLM 调用
    state = await graph.aget_state(_cfg(stack))
    assert state.next == ()
    assert await MessageDAO(stack.db).list_by_task(TASK, kind="clarification_qa") == []


async def test_intake_bad_llm_output_fails_node(make_stack):
    stack = await make_stack(llm=FakeLLM(["不是 JSON", '{"clarifications": "坏类型"}']))
    graph = _graph(stack)
    with pytest.raises(AppError) as ei:
        await graph.ainvoke({}, _cfg(stack))
    assert ei.value.code == "LLM_BAD_OUTPUT"
    # 绝不带半成品澄清过检查点：图未前进、无提问 message
    state = await graph.aget_state(_cfg(stack))
    assert state.next == ("intake",)
    assert await MessageDAO(stack.db).list_by_task(TASK, kind="clarification_qa") == []


async def test_intake_missing_daos_raises_clear_error(make_stack):
    stack = await make_stack()
    stack.ctx.daos = None
    with pytest.raises(AppError, match="daos"):
        await intake_node(stack.ctx, {})


async def test_intake_clauses_cache_regen_on_rerun(make_stack):
    """缓存可再生：删除后重算，内容恒等（clause_id 稳定，dd §4.3）。"""
    stack = await make_stack(llm=FakeLLM([json.dumps({"clarifications": []})] * 2))
    await intake_node(stack.ctx, {})
    cache1 = await stack.store.read_clauses_cache(WS, TASK)
    (stack.store.root / "workspaces" / WS / TASK / "requirement.clauses.json").unlink()
    with pytest.raises(NotFoundError):
        await stack.store.read_clauses_cache(WS, TASK)
    await intake_node(stack.ctx, {})
    assert await stack.store.read_clauses_cache(WS, TASK) == cache1


async def test_task_clauses_chinese_json_roundtrip(make_stack):
    stack = await make_stack()
    refs = (await intake_node(stack.ctx, {}))["clauses"]
    db_refs = await _task_clauses(stack)
    assert db_refs == refs
    assert db_refs[0]["title_path"] == ["支付流程"]
    assert db_refs[0]["anchor"].startswith("## 支付流程")


async def test_clauses_cache_read_missing_raises(make_stack):
    stack = await make_stack()
    with pytest.raises(NotFoundError):
        await stack.store.read_clauses_cache(WS, TASK)
