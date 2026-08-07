"""Conditional edge functions. Pure — no LLM, no I/O, no clock.

Every decision here reads state and returns a node name. The LLM decision lives INSIDE
the supervisor node and is written to `state["route"]`; this file only reads it.

That separation is what makes the graph's control flow testable with no mocks, no network
and no containers — see tests/test_edges.py. Routing bugs are the expensive kind: they do
not raise, they just send work to the wrong place and degrade answers in ways that look
like model quality problems.
"""

from __future__ import annotations

from langgraph.graph import END

from app.graph.state import AgentState


def route_from_supervisor(state: AgentState) -> str:
    """Switch on the supervisor's forced tool call.

    Reads a Literal-typed field, so an unknown value is a bug rather than a branch. It
    still falls back to retrieval rather than raising: for a compliance assistant, an
    unnecessary search is a trivial cost and answering unrouted from model memory is not.
    """
    route = state.get("route")
    if route in ("retrieve", "act", "answer"):
        return route
    return "retrieve"


def route_after_verify(state: AgentState) -> str:
    """The cycle's exit condition. Four outcomes, in priority order.

    Priority matters. `grounded` is checked BEFORE the attempt cap so a run that succeeds
    on its final permitted attempt is accepted rather than refused — testing the budget
    first would throw away a good answer for arriving late.
    """
    verdict = state.get("verdict")

    # No verdict means the verifier never ran — the no-verifier arm of the A/B, where
    # `analyst` edges straight to END. Defensive: reachable only via a wiring mistake.
    if verdict is None:
        return "finish"

    if verdict["grounded"]:
        # A grounded answer proceeds to the action node only if an action was actually
        # requested. Note this is the ONLY path to `act`: the analyst can never reach it
        # directly, which is what prevents the cycle from firing an n8n webhook twice.
        return "act" if state.get("action_request") else "finish"

    if state.get("attempts", 0) >= _max_attempts(state):
        # Budget spent without grounding. Refuse explicitly rather than emitting the
        # unverified answer — emitting it would defeat having a verifier at all.
        return "refuse"

    return "retrieve"


def route_after_analyze(state: AgentState) -> str:
    """Used only by the no-verifier arm of the A/B. Kept explicit rather than wiring a
    bare edge, so both arms of the experiment are described in the same vocabulary."""
    return "act" if state.get("action_request") else "finish"


def _max_attempts(state: AgentState) -> int:
    """Read from settings, not hardcoded, so the A/B can vary it without touching edges."""
    from app.config import get_settings

    return get_settings().max_attempts


# Mapping from a routing function's return value to graph node names. Declared next to the
# functions so a renamed node is a one-line change in one file, and so the set of legal
# destinations is readable without reconstructing it from build.py.
SUPERVISOR_MAP = {"retrieve": "retrieve", "act": "act", "answer": "respond"}
VERIFY_MAP = {"retrieve": "retrieve", "act": "act", "refuse": "refuse", "finish": END}
ANALYZE_MAP = {"act": "act", "finish": END}
