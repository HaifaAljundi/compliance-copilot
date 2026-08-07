"""Reducer semantics for AgentState.

Offline: no model, no database, no containers. Run with `pytest -m 'not integration'`.

These tests exist because the reducer choices in state.py are asserted in comments, and a
comment is not a guarantee. The `retrieved` field in particular is the difference between
a verifier loop that gets cheaper and sharper each pass and one that gets more expensive
and worse — and the failure is invisible, since an accumulating list still produces
plausible answers, just slower and with stale evidence mixed in.

A real two-node StateGraph is compiled here rather than calling reducers directly, so what
is tested is LangGraph's actual merge behaviour, not my model of it.
"""

from langgraph.graph import END, START, StateGraph

from app.graph.state import AgentState, Chunk, initial_state


def _chunk(cid: str) -> Chunk:
    return Chunk(
        chunk_id=cid, doc_id="d", section="Article 1", section_title="t", title="T",
        issuer="i", domain="dp", url="u", text=f"body {cid}", score=0.1,
    )


def _run_two_passes() -> AgentState:
    """Simulate a verifier loop-back: two retrievals in one run."""

    def first(state: AgentState):
        return {
            "retrieved": [_chunk("a1"), _chunk("a2")],
            "query_history": ["original query"],
            "trace": [{"node": "retrieve", "attempt": 1}],
            "attempts": 1,
        }

    def second(state: AgentState):
        # What the retriever returns on loop-back after a failed verification.
        return {
            "retrieved": [_chunk("b1")],
            "query_history": ["rewritten query"],
            "trace": [{"node": "retrieve", "attempt": 2}],
            "attempts": 2,
        }

    g = StateGraph(AgentState)
    g.add_node("first", first)
    g.add_node("second", second)
    g.add_edge(START, "first")
    g.add_edge("first", "second")
    g.add_edge("second", END)

    return g.compile().invoke(initial_state("q", k=5))


def test_retrieved_is_replaced_not_accumulated():
    """The reducer decision that keeps the cycle from degrading.

    If this ever fails, the graph still "works" — it just carries every previous chunk set
    forward, so each retry costs more, and the weak chunks that already failed
    verification are re-submitted to the analyst alongside the new ones.
    """
    out = _run_two_passes()
    ids = [c["chunk_id"] for c in out["retrieved"]]
    assert ids == ["b1"], (
        f"expected only the second pass's chunks, got {ids}. `retrieved` must use the "
        "default overwrite reducer — an `operator.add` annotation would accumulate."
    )


def test_query_history_accumulates():
    """The counterpart: this one MUST grow, or 'don't repeat a query' is unenforceable."""
    out = _run_two_passes()
    assert out["query_history"] == ["original query", "rewritten query"]


def test_trace_accumulates_across_nodes():
    """Every eval number is derived from trace; losing entries loses the measurement."""
    out = _run_two_passes()
    assert [t["attempt"] for t in out["trace"]] == [1, 2]


def test_scalars_take_last_write():
    out = _run_two_passes()
    assert out["attempts"] == 2


def test_initial_state_populates_every_field():
    """Guards against the KeyError-deep-inside-a-superstep failure mode.

    Compared against AgentState's annotations rather than a hand-written list, so adding a
    field to the schema without adding it to the factory fails here instead of at runtime.
    """
    state = initial_state("what is consent?", k=5)
    missing = set(AgentState.__annotations__) - set(state)
    assert not missing, f"initial_state() does not populate: {sorted(missing)}"


def test_original_question_is_preserved_separately_from_search_query():
    """The verifier grades against `question`; the loop rewrites `search_query`.

    Collapsing these into one field lets a drifting rewrite produce an answer that is
    perfectly grounded in a question the user never asked.
    """
    state = initial_state("what is consent?", k=5)
    assert state["question"] == state["search_query"] == "what is consent?"
    state["search_query"] = "consent freely given demonstrate controller"
    assert state["question"] == "what is consent?"
