"""Graph control flow. Offline — no model, no database, no containers.

Routing bugs do not raise. They send work to the wrong node and degrade answers in a way
that reads as a model-quality problem, so they are among the most expensive defects here
and among the cheapest to test — because edges.py contains no LLM call at all.
"""

import pytest

from app.graph.edges import (
    SUPERVISOR_MAP,
    VERIFY_MAP,
    route_after_analyze,
    route_after_verify,
    route_from_supervisor,
)
from app.graph.state import Verdict, initial_state


def _s(**kw):
    s = initial_state("q", k=5)
    s.update(kw)
    return s


def _v(grounded: bool) -> Verdict:
    return Verdict(grounded=grounded, unsupported_claims=[], missing_evidence="gap")


class TestSupervisorRouting:
    @pytest.mark.parametrize("route", ["retrieve", "act", "answer"])
    def test_passes_through_valid_routes(self, route):
        assert route_from_supervisor(_s(route=route)) == route

    @pytest.mark.parametrize("bad", ["", None, "RETRIEVE", "search", "delete_everything"])
    def test_falls_back_to_retrieve_on_anything_invalid(self, bad):
        """An unnecessary search is trivially cheap. Answering a regulatory question
        unrouted, from model memory, is not."""
        assert route_from_supervisor(_s(route=bad)) == "retrieve"


class TestVerifyRouting:
    def test_grounded_without_an_action_finishes(self):
        assert route_after_verify(_s(verdict=_v(True), attempts=1)) == "finish"

    def test_grounded_with_an_action_proceeds_to_act(self):
        s = _s(verdict=_v(True), attempts=1, action_request={"action": "file_report"})
        assert route_after_verify(s) == "act"

    def test_ungrounded_with_budget_remaining_loops_back(self):
        assert route_after_verify(_s(verdict=_v(False), attempts=1)) == "retrieve"

    def test_ungrounded_at_the_cap_refuses(self):
        """Emitting the unverified answer instead would defeat having a verifier."""
        assert route_after_verify(_s(verdict=_v(False), attempts=3)) == "refuse"

    def test_grounded_is_checked_before_the_attempt_cap(self):
        """An answer that succeeds on the LAST permitted attempt must be accepted.

        Checking the budget first would discard a good answer for arriving late — a bug
        that would show up in the eval as the verifier reducing answer rate for no reason.
        """
        s = _s(verdict=_v(True), attempts=99, action_request=None)
        assert route_after_verify(s) == "finish"

    def test_missing_verdict_finishes_rather_than_looping(self):
        """Defensive: only reachable via a wiring mistake, but a wiring mistake that
        looped forever would be far worse than one that stops."""
        assert route_after_verify(_s(verdict=None)) == "finish"


class TestActionReachability:
    """The double-fire guard, expressed as a property of the graph rather than a comment.

    In the verify arm, `act` must be reachable ONLY from a passing verdict. If the analyst
    or the supervisor could reach it directly, a verifier loop-back would re-run it and
    POST the n8n webhook twice — downstream, two tickets or two filings.

    Asserting this on route_after_verify alone is not enough: the destination MAPS are the
    other half of the wiring, and a map entry pointing at `act` opens an entrance no
    routing-function test can see.
    """

    def test_act_requires_a_grounded_verdict(self):
        s = _s(verdict=_v(False), attempts=1, action_request={"action": "file_report"})
        assert route_after_verify(s) != "act"

    def test_act_requires_an_actual_request(self):
        assert route_after_verify(_s(verdict=_v(True), attempts=1)) != "act"

    def test_no_verifier_arm_still_gates_on_a_request(self):
        assert route_after_analyze(_s()) == "finish"
        assert route_after_analyze(_s(action_request={"action": "x"})) == "act"

    def test_supervisor_has_no_edge_to_act(self):
        """The supervisor decides from the raw question, before any retrieval has run.

        An edge from there to `act` would deliver an approval request carrying no answer
        and no evidence — the rubber-stamp the interrupt exists to prevent — and would
        also bypass the verifier entirely.
        """
        assert "act" not in SUPERVISOR_MAP.values()

    def test_supervisors_act_decision_routes_through_retrieval(self):
        assert route_from_supervisor(_s(route="act")) == "act"
        assert SUPERVISOR_MAP["act"] == "retrieve"

    def test_verify_is_the_only_map_that_reaches_act_in_the_verify_arm(self):
        assert VERIFY_MAP["act"] == "act"
