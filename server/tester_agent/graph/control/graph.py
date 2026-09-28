"""Control graph: plan → dispatch → execute_step → reflect."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..state import TaskState
from .nodes import (
    dispatch_node,
    execute_step_node,
    plan_node,
    reflect_node,
    route_after_dispatch,
    route_after_reflect,
)


def build_control_graph(checkpointer=None, *, caps=None) -> CompiledStateGraph:
    """Compile Plan-Execute control loop.

    ``caps`` reserved for Task 5+ capability injection; unused in stub.
    """
    del caps  # reserved
    g = StateGraph(TaskState)
    g.add_node("plan", plan_node)
    g.add_node("dispatch", dispatch_node)
    g.add_node("execute_step", execute_step_node)
    g.add_node("reflect", reflect_node)
    g.add_edge(START, "plan")
    g.add_edge("plan", "dispatch")
    g.add_conditional_edges(
        "dispatch", route_after_dispatch, {"execute": "execute_step", "end": END}
    )
    g.add_edge("execute_step", "reflect")
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
