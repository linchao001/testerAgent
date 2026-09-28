"""Control graph topology and stub run."""

import pytest

from tester_agent.graph.control.graph import build_control_graph


def test_control_graph_compiles_and_has_nodes():
    g = build_control_graph(checkpointer=None)
    names = set(g.get_graph().nodes)
    for n in ("plan", "dispatch", "execute_step", "await_human", "reflect"):
        assert n in names


@pytest.mark.asyncio
async def test_stub_run_completes():
    g = build_control_graph()
    out = await g.ainvoke(
        {
            "task_id": "t1",
            "graph_run_id": "r1",
            "workspace_id": "w1",
            "human_gates": {"link": False, "point": False, "review": False},
        }
    )
    assert out["agent_plan"]["status"] == "completed"
