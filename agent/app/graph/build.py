"""Graph assembly. The ONLY file that knows the graph's shape.

`build_graph(verify=...)` is the experiment's control surface. Both arms of the A/B use
IDENTICAL node functions, the same retriever, the same models and the same temperature —
only the edges differ:

    verify=False   supervise -> retrieve -> analyze -> END
    verify=True    supervise -> retrieve -> analyze -> verify -+-> act -> END
                                    ^                          |-> refuse -> END
                                    +--------------------------+ (not grounded,
                                                                  attempts remaining)

Because nodes communicate only through state, that rewiring needs no change inside any
node. If the two arms shared code less cleanly, a measured difference could always be
argued away as an implementation difference rather than an effect of verification.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graph.edges import (
    ANALYZE_MAP,
    SUPERVISOR_MAP,
    VERIFY_MAP,
    route_after_analyze,
    route_after_verify,
    route_from_supervisor,
)
from app.graph.nodes.action import act
from app.graph.nodes.analyst import analyze
from app.graph.nodes.responder import respond
from app.graph.nodes.retriever import retrieve
from app.graph.nodes.supervisor import supervise
from app.graph.nodes.verifier import build_refusal, verify
from app.graph.state import AgentState


def build_graph(*, verify_enabled: bool = True, checkpointer=None):
    """Compile the graph.

    Args:
        verify_enabled: wire the Verifier node and its cycle. False is the A/B control arm.
        checkpointer: required for the human-approval interrupt and for /resume. Optional
            so the eval harness can run without persisting hundreds of throwaway threads.
    """
    g = StateGraph(AgentState)

    g.add_node("supervise", supervise)
    g.add_node("retrieve", retrieve)
    g.add_node("analyze", analyze)
    g.add_node("act", act)
    g.add_node("respond", respond)

    g.add_edge(START, "supervise")
    # "answer" routes to `respond`, NOT to END. Routing it to END produced a completed
    # run with an empty answer string — no node on that path wrote one.
    g.add_conditional_edges("supervise", route_from_supervisor, SUPERVISOR_MAP)
    g.add_edge("respond", END)
    g.add_edge("retrieve", "analyze")

    if verify_enabled:
        g.add_node("verify", verify)
        g.add_node("refuse", build_refusal)
        g.add_edge("analyze", "verify")
        # THE CYCLE. `verify` can send control back to `retrieve`, which LangGraph runs
        # happily and forever if nothing stops it. Two independent bounds: `attempts`
        # (checked in route_after_verify, exits gracefully via `refuse`) and
        # `recursion_limit` at invoke time (raises — a backstop, not a control).
        g.add_conditional_edges("verify", route_after_verify, VERIFY_MAP)
        g.add_edge("refuse", END)
    else:
        g.add_conditional_edges("analyze", route_after_analyze, ANALYZE_MAP)

    g.add_edge("act", END)
    return g.compile(checkpointer=checkpointer)
