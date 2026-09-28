"""Control graph: plan → dispatch → execute_step → await_human → reflect."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..state import TaskState
from ..wrap import NodeFn
from .nodes import (
    await_human_node,
    dispatch_node,
    execute_step_node,
    plan_node,
    reflect_node,
    route_after_dispatch,
    route_after_reflect,
)


def build_control_graph(
    checkpointer=None,
    *,
    caps: Mapping[Any, NodeFn] | None = None,
) -> CompiledStateGraph:
    """Compile Plan-Execute control loop.

    ``caps`` optional ``PlanStepKind → node`` overrides for tests / tooling.
    """

    async def _execute(state: dict, config=None) -> dict:
        return await execute_step_node(state, config, nodes=caps)

    g = StateGraph(TaskState)
    g.add_node("plan", plan_node)
    g.add_node("dispatch", dispatch_node)
    g.add_node("execute_step", _execute)
    g.add_node("await_human", await_human_node)
    g.add_node("reflect", reflect_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "dispatch")
    g.add_conditional_edges(
        "dispatch", route_after_dispatch, {"execute": "execute_step", "end": END}
    )
    g.add_edge("execute_step", "await_human")
    g.add_edge("await_human", "reflect")
    g.add_conditional_edges(
        "reflect",
        route_after_reflect,
        {
            "dispatch": "dispatch",
            "plan": "plan",
            "execute": "execute_step",
            "end": END,
        },
    )
    compile_kwargs: dict = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer
    return g.compile(**compile_kwargs)
