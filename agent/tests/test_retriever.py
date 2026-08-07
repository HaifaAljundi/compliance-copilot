"""Retriever logic that must hold without a model or a database.

Everything here is offline. The functions tested are the ones whose failure is SILENT —
they do not raise, they just make the verifier cycle stop working while every signal
continues to report success.
"""

import pytest

from app.graph.nodes.retriever import _clean_query, should_rewrite
from app.graph.state import Verdict, initial_state


def _state(attempts: int = 0, verdict: Verdict | None = None):
    s = initial_state("q", k=5)
    s["attempts"] = attempts
    s["verdict"] = verdict
    return s


def _verdict(grounded: bool) -> Verdict:
    return Verdict(grounded=grounded, unsupported_claims=[], missing_evidence="x")


class TestShouldRewrite:
    def test_no_rewrite_on_first_pass(self):
        assert should_rewrite(_state()) is False

    def test_no_rewrite_without_a_verdict(self):
        """A resumed run can have attempts > 0 with no verdict yet."""
        assert should_rewrite(_state(attempts=2)) is False

    def test_no_rewrite_after_a_passing_verdict(self):
        """Routing here for another reason must not trigger a pointless rewrite."""
        assert should_rewrite(_state(attempts=1, verdict=_verdict(True))) is False

    def test_rewrite_after_a_failing_verdict(self):
        assert should_rewrite(_state(attempts=1, verdict=_verdict(False))) is True


class TestCleanQuery:
    """Regression tests for an observed silent corruption.

    qwen/qwen3.6-27b returned "\\n<think>\\nHere's a thinking process:..." and naive
    first-line parsing produced the literal query "<think>". That embedded fine, searched
    fine, returned pure noise, and the trace recorded rewritten=True — a spinning loop
    indistinguishable from a productive one in every metric.
    """

    def test_strips_a_complete_think_block(self):
        raw = "<think>\nreasoning here\n</think>\nbreach notification deadline"
        assert _clean_query(raw) == "breach notification deadline"

    def test_takes_the_last_line_not_the_first(self):
        """Reasoning precedes the payload, so the first line is the wrong end."""
        raw = "<think>x</think>\npreamble\nactual query terms"
        assert _clean_query(raw) == "actual query terms"

    def test_unterminated_think_block_yields_nothing(self):
        """Cut off mid-reasoning: stripping the tag alone would make the reasoning itself
        the search query — the same bug one step further along."""
        assert _clean_query("<think>reasoning that never closes") == ""

    def test_rejects_residual_markup(self):
        assert _clean_query("<tool_call>{}</tool_call>") == ""

    def test_rejects_an_essay(self):
        assert _clean_query("word " * 200) == ""

    @pytest.mark.parametrize("raw", ["", "   ", "\n\n"])
    def test_empty_input_yields_nothing(self, raw):
        assert _clean_query(raw) == ""

    def test_strips_surrounding_quotes_and_backticks(self):
        assert _clean_query('"breach notification"') == "breach notification"
        assert _clean_query("`breach notification`") == "breach notification"


class TestRewriteLadder:
    """The rewriter must never return a query already tried.

    Measured before this ladder existed: 6 of 10 verifier loops repeated a query, because
    the failure path returned `state["search_query"]` — manufacturing the exact repeat the
    no-repeat guard was written to prevent. A majority of the Verifier's latency cost
    bought only a wider k.
    """

    def test_gap_statement_becomes_a_query(self):
        from app.graph.nodes.retriever import _from_missing_evidence

        assert (
            _from_missing_evidence("No retrieved passage states the VAT registration threshold.")
            == "VAT registration threshold"
        )

    def test_generic_gap_preamble_is_stripped(self):
        from app.graph.nodes.retriever import _from_missing_evidence

        got = _from_missing_evidence("unclear; broaden the search for: breach deadlines")
        assert got == "breach deadlines"

    def test_too_short_a_gap_is_rejected(self):
        from app.graph.nodes.retriever import _from_missing_evidence

        assert _from_missing_evidence("No passage states it.") == ""

    def test_keywords_always_differ_from_the_question(self):
        from app.graph.nodes.retriever import _keywords

        q = "What is the VAT registration threshold for businesses in the UAE?"
        kw = _keywords(q)
        assert kw != q and kw == "VAT registration threshold businesses UAE"

    def test_ladder_falls_through_to_deterministic_rungs(self, monkeypatch):
        """With the model returning nothing, the ladder must still produce a NEW query."""
        import app.graph.nodes.retriever as R
        from app.graph.state import Verdict, initial_state

        monkeypatch.setattr(R, "_ask_model", lambda *a, **k: "")

        s = initial_state("What is the VAT registration threshold in the UAE?", k=5)
        s["attempts"] = 1
        s["query_history"] = [s["question"]]
        s["verdict"] = Verdict(
            grounded=False,
            unsupported_claims=[],
            missing_evidence="No retrieved passage states the VAT registration threshold amount.",
        )
        query, strategy = R.rewrite_query(s)
        assert strategy == "missing-evidence"
        assert query.lower() not in {q.lower() for q in s["query_history"]}

    def test_ladder_reaches_keywords_when_the_gap_is_useless(self, monkeypatch):
        import app.graph.nodes.retriever as R
        from app.graph.state import Verdict, initial_state

        monkeypatch.setattr(R, "_ask_model", lambda *a, **k: "")
        s = initial_state("When must a controller notify a personal data breach?", k=5)
        s["attempts"] = 1
        s["query_history"] = [s["question"], "personal data breach notify"]
        s["verdict"] = Verdict(grounded=False, unsupported_claims=[], missing_evidence="n/a")
        query, strategy = R.rewrite_query(s)
        assert strategy == "keywords"
        assert query.lower() not in {q.lower() for q in s["query_history"]}

    def test_exhausted_is_reported_not_disguised(self, monkeypatch):
        """When there is genuinely nothing new, say so — a spinning loop must be visible."""
        import app.graph.nodes.retriever as R
        from app.graph.state import Verdict, initial_state

        monkeypatch.setattr(R, "_ask_model", lambda *a, **k: "")
        s = initial_state("Consent rules", k=5)
        s["attempts"] = 1
        s["verdict"] = Verdict(grounded=False, unsupported_claims=[], missing_evidence="x")
        # Every deterministic rung already tried.
        s["query_history"] = ["Consent rules", "Consent rules", R._keywords("Consent rules")]
        query, strategy = R.rewrite_query(s)
        assert strategy == "exhausted"
        assert query == s["search_query"]

    def test_model_failure_does_not_crash_the_ladder(self, monkeypatch):
        import app.graph.nodes.retriever as R
        from app.graph.state import Verdict, initial_state

        def boom(*a, **k):
            raise RuntimeError("groq down")

        monkeypatch.setattr(R, "_ask_model", boom)
        s = initial_state("What identification is required for a natural person?", k=5)
        s["attempts"] = 1
        s["query_history"] = [s["question"]]
        s["verdict"] = Verdict(
            grounded=False, unsupported_claims=[],
            missing_evidence="No passage covers identification requirements for natural persons.",
        )
        query, strategy = R.rewrite_query(s)
        assert strategy == "missing-evidence"
        assert query
