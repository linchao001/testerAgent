"""SP-2 Spike 验证（dd §7.1 §7.6 §11.2；WBS SP-2）。

锁定版本：langgraph 1.2.12 / langgraph-checkpoint 4.2.0 /
langgraph-checkpoint-sqlite 3.1.1。

验证四个 go/no-go 点（结论：全部 GO，WP-15 可走图原生路径）：
1. 节点内 ``interrupt()`` 函数式挂起（任意节点、payload 可取）；
2. ``Command(resume=...)`` 恢复（resume 值作为 interrupt() 调用返回值，支持多轮）；
3. confirm(modify)：暂停位 ``aupdate_state`` 写修订 payload 后 ``ainvoke(None)``；
4. 派生 thread 续跑（回退 §11.2）：新 thread_id +
   ``aupdate_state(..., as_node=目标阶段的前驱节点)`` 注入入口 state，
   图从目标阶段重跑、经真实边重新到达 gate 时 interrupt_before 正常生效。

另固化两条生产发现（WP-15/22/23 直接引用）：
- 跨"进程"恢复：新建 saver 连接同一 checkpoints.db、同 thread_id，
  暂停态完整可读、ainvoke(None) 可续跑（崩溃恢复前提成立）；
- 图已结束（无待处理中断）时 ``Command(resume=...)`` 为静默 no-op，不报错
  → answer API 必须在应用层做 waiting_input 状态机前置校验。
"""

from __future__ import annotations

from typing import TypedDict

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

pytestmark = pytest.mark.asyncio


class _State(TypedDict, total=False):
    log: list[str]
    plan: str
    answers: list[str]


# ---------- 镜像 dd §7.1 拓扑的最小图 ----------


async def _intake(state: _State) -> dict:
    log = state.get("log", []) + ["intake"]
    ans = interrupt({"q": "q1"})
    return {"log": log + [f"intake:{ans}"], "answers": [ans]}


async def _link_id(state: _State) -> dict:
    log = state.get("log", []) + ["link_id"]
    ans = interrupt({"q": "q2"})
    return {
        "log": log + [f"link_id:{ans}"],
        "answers": state.get("answers", []) + [ans],
    }


async def _cp1_gate(state: _State) -> dict:
    return {"log": state.get("log", []) + ["cp1_gate"]}


async def _point(state: _State) -> dict:
    plan = state.get("plan", "v1")
    return {"log": state.get("log", []) + [f"point:{plan}"]}


async def _cp2_gate(state: _State) -> dict:
    return {"log": state.get("log", []) + ["cp2_gate"]}


async def _case_gen(state: _State) -> dict:
    return {"log": state.get("log", []) + ["case_gen"]}


async def _coverage(state: _State) -> dict:
    return {"log": state.get("log", []) + ["coverage"]}


def _build_graph():
    g = StateGraph(_State)
    g.add_node("intake", _intake)
    g.add_node("link_id", _link_id)
    g.add_node("cp1_gate", _cp1_gate)
    g.add_node("point_write", _point)
    g.add_node("cp2_gate", _cp2_gate)
    g.add_node("case_generate", _case_gen)
    g.add_node("coverage_check", _coverage)
    g.add_edge(START, "intake")
    g.add_edge("intake", "link_id")
    g.add_edge("link_id", "cp1_gate")
    g.add_edge("cp1_gate", "point_write")
    g.add_edge("point_write", "cp2_gate")
    g.add_edge("cp2_gate", "case_generate")
    g.add_edge("case_generate", "coverage_check")
    g.add_edge("coverage_check", END)
    return g


@pytest.fixture
async def saver_conn(tmp_path):
    """长生命周期 saver（lifespan 同款构造：aiosqlite 直连 + AsyncSqliteSaver）。"""
    conn = await aiosqlite.connect(str(tmp_path / "checkpoints.db"), check_same_thread=False)
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    yield saver
    await conn.close()


@pytest.fixture
def app(saver_conn):
    return _build_graph().compile(
        checkpointer=saver_conn, interrupt_before=["cp1_gate", "cp2_gate"]
    )


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


# ---------- 验证点 1/2：interrupt() 挂起 + Command(resume=) 多轮恢复 ----------


async def test_interrupt_suspend_and_command_resume(app):
    cfg = _cfg("t1")

    # 首轮：挂在 intake 节点内部（非 interrupt_before 的静态 gate）
    result = await app.ainvoke({"log": []}, cfg)
    assert "__interrupt__" in result
    state = await app.aget_state(cfg)
    assert state.next == ("intake",)
    assert state.tasks[0].interrupts[0].value == {"q": "q1"}

    # resume 值作为 interrupt() 调用返回值；继续后挂在 link_id 的第二轮 interrupt
    await app.ainvoke(Command(resume="A1"), cfg)
    state = await app.aget_state(cfg)
    assert state.next == ("link_id",)
    assert state.tasks[0].interrupts[0].value == {"q": "q2"}

    # 第二轮恢复：真实边到达 cp1_gate，静态 interrupt_before 生效
    await app.ainvoke(Command(resume="A2"), cfg)
    state = await app.aget_state(cfg)
    assert state.next == ("cp1_gate",)

    # 放行 cp1：point_write 跑完后再次停在 cp2_gate
    await app.ainvoke(None, cfg)
    state = await app.aget_state(cfg)
    assert state.next == ("cp2_gate",)

    # 放行 cp2：跑到 END；节点执行序与两轮 answers 完整
    await app.ainvoke(None, cfg)
    state = await app.aget_state(cfg)
    assert state.next == ()
    assert state.values["log"] == [
        "intake", "intake:A1", "link_id", "link_id:A2",
        "cp1_gate", "point:v1", "cp2_gate", "case_gen", "coverage",
    ]
    assert state.values["answers"] == ["A1", "A2"]


# ---------- 验证点 3：confirm(modify) —— 暂停位 update_state 后 invoke(None) ----------


async def test_modify_update_state_before_gate(app):
    cfg = _cfg("t2")
    await app.ainvoke({"log": []}, cfg)
    await app.ainvoke(Command(resume="A1"), cfg)
    await app.ainvoke(Command(resume="A2"), cfg)
    state = await app.aget_state(cfg)
    assert state.next == ("cp1_gate",)

    # 用户在 CP1 修改产物：写修订 payload（dd §7.6：先 API 落 artifact，再 update_state）
    await app.aupdate_state(cfg, {"plan": "v2-revised"})
    state = await app.aget_state(cfg)
    assert state.next == ("cp1_gate",)  # 暂停位不变
    assert state.values["plan"] == "v2-revised"

    # resume 后下游节点读到修订后的 plan
    await app.ainvoke(None, cfg)  # 过 cp1 -> point_write -> 停 cp2
    await app.ainvoke(None, cfg)  # 过 cp2 -> END
    state = await app.aget_state(cfg)
    assert state.next == ()
    assert "point:v2-revised" in state.values["log"]


# ---------- 生产发现 A：跨进程（新 saver 同库同 thread）恢复 ----------


async def test_resume_after_reopen_saver(tmp_path):
    db_path = tmp_path / "checkpoints.db"

    conn = await aiosqlite.connect(str(db_path), check_same_thread=False)
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    app = _build_graph().compile(
        checkpointer=saver, interrupt_before=["cp1_gate", "cp2_gate"]
    )
    cfg = _cfg("t3")
    await app.ainvoke({"log": []}, cfg)
    await app.ainvoke(Command(resume="A1"), cfg)
    await app.ainvoke(Command(resume="A2"), cfg)
    await app.ainvoke(None, cfg)  # 停在 cp2_gate
    await conn.close()

    # 模拟进程重启：全新连接指向同一文件
    conn2 = await aiosqlite.connect(str(db_path), check_same_thread=False)
    saver2 = AsyncSqliteSaver(conn2)
    await saver2.setup()
    app2 = _build_graph().compile(
        checkpointer=saver2, interrupt_before=["cp1_gate", "cp2_gate"]
    )
    state = await app2.aget_state(cfg)
    assert state.next == ("cp2_gate",)
    assert "point:v1" in state.values["log"]
    await app2.ainvoke(None, cfg)
    state = await app2.aget_state(cfg)
    assert state.next == ()
    assert state.values["log"][-1] == "coverage"
    await conn2.close()


# ---------- 生产发现 B：无待处理中断时 resume 静默 no-op（须应用层守卫） ----------


async def test_resume_without_pending_interrupt_is_noop(app):
    cfg = _cfg("t4")
    await app.ainvoke({"log": []}, cfg)
    await app.ainvoke(Command(resume="A1"), cfg)
    await app.ainvoke(Command(resume="A2"), cfg)
    await app.ainvoke(None, cfg)
    await app.ainvoke(None, cfg)
    state = await app.aget_state(cfg)
    assert state.next == ()

    # 不抛异常、状态不变 → answer/confirm API 必须先校验 waiting_* 状态
    result = await app.ainvoke(Command(resume="late"), cfg)
    assert result.get("log", [])[-1:] == ["coverage"] or "__interrupt__" not in result
    state2 = await app.aget_state(cfg)
    assert state2.values["log"] == state.values["log"]


# ---------- 验证点 4：派生 thread 续跑（回退 §11.2） ----------


async def test_derived_thread_rerun_from_target_stage(app):
    """回退 = 新 thread_id + as_node=目标阶段前驱；不拷贝旧 checkpoint、不继承历史。"""
    # 旧 thread 先完整跑过一遍（建立"上一 run"现场）
    old_cfg = _cfg("t5")
    await app.ainvoke({"log": [], "plan": "v1"}, old_cfg)
    await app.ainvoke(Command(resume="A1"), old_cfg)
    await app.ainvoke(Command(resume="A2"), old_cfg)
    await app.ainvoke(None, old_cfg)
    await app.ainvoke(None, old_cfg)
    old_state = await app.aget_state(old_cfg)
    assert old_state.next == ()

    # 回退到 point_write 重跑：as_node 取其前驱 cp1_gate，入口 state 只注入修订产物
    new_cfg = _cfg("t5::run2")
    await app.aupdate_state(
        new_cfg,
        {"log": ["rollback-seed"], "plan": "v2-revised"},
        as_node="cp1_gate",
    )
    state = await app.aget_state(new_cfg)
    assert state.next == ("point_write",)
    assert state.values["log"] == ["rollback-seed"]  # 不继承旧 thread 历史

    # invoke(None)：point_write 重跑，经真实边到达 cp2_gate，interrupt_before 重新生效
    await app.ainvoke(None, new_cfg)
    state = await app.aget_state(new_cfg)
    assert state.next == ("cp2_gate",)
    assert state.values["log"] == ["rollback-seed", "point:v2-revised"]

    # 旧 thread 状态不受派生 thread 影响
    old_after = await app.aget_state(old_cfg)
    assert "point:v1" in old_after.values["log"]
    assert "point:v2-revised" not in old_after.values["log"]


async def test_derived_thread_gate_positioning(app):
    """as_node=gate 自身：合成 checkpoint 的挂起节点不再触发 interrupt_before，
    invoke(None) 直接续跑其后继（"从 gate 之后续跑"定位口径，WP-24 知悉）。"""
    cfg = _cfg("t6::run1")
    await app.aupdate_state(cfg, {"log": ["seed"]}, as_node="cp1_gate")
    state = await app.aget_state(cfg)
    assert state.next == ("point_write",)
    # point_write 真实到达 cp2_gate 仍会断住
    await app.ainvoke(None, cfg)
    state = await app.aget_state(cfg)
    assert state.next == ("cp2_gate",)


# ---------- 附：saver setup 幂等（启动重复执行不报错，对齐业务库迁移口径） ----------


async def test_saver_setup_idempotent(saver_conn):
    await saver_conn.setup()
    await saver_conn.setup()
