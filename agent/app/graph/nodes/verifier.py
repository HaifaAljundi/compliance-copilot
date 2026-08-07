"""Verifier node — per-claim groundedness, and the diagnosis that drives the retry.

The failure this file is designed against is SILENT. A verifier that approves ~97% of
what it sees produces a graph that looks like it works, an A/B with a flat delta, and no
indication which of those is the bug. Four structural choices push against it:

  1. A DIFFERENT MODEL from the analyst. config.py refuses to start otherwise. A model
     grading its own output is not an independent check.

  2. PER-CLAIM ENTAILMENT, never holistic. "Is claim C entailed by this passage, yes/no"
     is a far harder question to rubber-stamp than "is this answer good". Holistic
     grading is where approval rates go to 97%.

  3. THE JUDGE DOES NOT SEE THE QUESTION. It sees one claim and one passage. Given the
     question, a model reasons toward the answer being reasonable — it fills gaps from
     its own knowledge and calls the result "supported". Withholding the question removes
     the material it would use to do that.

  4. THE DETERMINISTIC CHECKS RAN FIRST. analyst.py already discarded fabricated chunk
     ids and non-verbatim quotes. This node never spends a token on those.

`missing_evidence` is produced by a SEPARATE call that does see the question — it has to,
since "what is missing" is only meaningful relative to what was asked. That call runs only
on failure, and its output is a search instruction for the retriever, not a judgement.
"""

from __future__ import annotations

from time import perf_counter
from typing import Literal

from pydantic import BaseModel, Field

from app.graph.state import AgentState, Verdict
from app.llm import invoke_structured, model_for

# No question, no answer, no document title — one claim, one passage. Everything omitted
# here is material the judge would otherwise use to talk itself into agreement.
_ENTAIL_PROMPT = """Passage:
\"\"\"{passage}\"\"\"

Claim: {claim}

Is the claim stated by the passage? Answer only about what the passage literally says. \
Do not use outside knowledge. If the passage merely relates to the topic without stating \
the claim, that is NOT support."""


# NOTE: "yes"/"no" strings, NOT booleans, throughout these schemas.
#
# qwen/qwen3.6-27b emits `"supported": "true"` — a STRING — where a schema declares a
# boolean, and Groq rejects the tool call server-side with
#   tool call validation failed: `/supported`: expected boolean, but got string
# The rejection happens before any Python runs, so a Pydantic coercing validator cannot
# recover it. A string enum is what the model reliably produces; the conversion to bool
# happens here, where it is cheap and visible.
#
# This surfaced as every claim being marked unsupported (the fail-closed path below), i.e.
# a verifier that refused everything rather than one that approved everything. Safer of
# the two failure directions, but equally wrong.
class _Entailment(BaseModel):
    supported: Literal["yes", "no"] = Field(
        description="'yes' only if the passage literally states the claim."
    )
    reason: str = Field(description="One short sentence.")


class _Gap(BaseModel):
    missing_evidence: str = Field(
        description=(
            "What specific material is absent, phrased so it can be used as a search "
            "query. E.g. 'no passage states the deadline for notifying a breach'."
        )
    )
    addresses_question: Literal["yes", "no"] = Field(
        description="'yes' if the answer responds to the question asked."
    )


_GAP_PROMPT = """Question: {question}

Answer given: {answer}

Sections retrieved: {sections}

Some claims in this answer were not supported by the retrieved passages: {unsupported}

State precisely what evidence is missing, in terms a search engine could act on."""


def _judge_claim(claim: str, passage: str) -> tuple[bool, str, dict]:
    """One claim against one passage. Fail CLOSED on error.

    An exception here must not silently become "supported" — that would turn a transient
    API failure into a hallucination passing verification.
    """
    try:
        r, usage = invoke_structured(
            "verifier", _Entailment,
            _ENTAIL_PROMPT.format(passage=passage[:4000], claim=claim),
        )
        return r.supported == "yes", r.reason, usage
    except Exception as exc:
        return False, f"verification failed: {type(exc).__name__}", {}


def verify(state: AgentState) -> dict:
    """Grade the answer. Returns a verdict and an incremented attempt count."""
    t0 = perf_counter()
    citations = state.get("citations", [])
    chunks = {c["chunk_id"]: c for c in state.get("retrieved", [])}
    attempts = state.get("attempts", 0) + 1

    # An answer with no surviving citations is ungrounded by construction. This is a
    # common real case rather than an edge case: it is what remains after the analyst's
    # deterministic check discards every fabricated citation.
    if not citations:
        verdict = Verdict(
            grounded=False,
            unsupported_claims=["answer carries no verifiable citation"],
            missing_evidence=(
                f"No retrieved passage supports an answer to: {state['question']}"
            ),
        )
        return {
            "verdict": verdict,
            "attempts": attempts,
            "trace": [{"node": "verify", "ms": round((perf_counter() - t0) * 1000, 1),
                       "attempt": attempts, "claims": 0, "grounded": False,
                       "model": model_for("verifier")}],
        }

    unsupported: list[str] = []
    tok_in = tok_out = 0
    for cit in citations:
        chunk = chunks.get(cit["chunk_id"])
        if chunk is None:
            unsupported.append(cit["claim"])
            continue
        ok, _reason, usage = _judge_claim(cit["claim"], chunk["text"])
        tok_in += usage.get("prompt_tokens", 0)
        tok_out += usage.get("completion_tokens", 0)
        if not ok:
            unsupported.append(cit["claim"])

    grounded = not unsupported
    missing = ""
    addresses = True

    if not grounded:
        try:
            gap, gap_usage = invoke_structured(
                "verifier", _Gap,
                _GAP_PROMPT.format(
                    question=state["question"],
                    answer=state["answer"][:2000],
                    sections=", ".join(sorted({c["section"] for c in state["retrieved"]})),
                    unsupported="; ".join(unsupported[:5]),
                )
            )
            tok_in += gap_usage.get("prompt_tokens", 0)
            tok_out += gap_usage.get("completion_tokens", 0)
            missing, addresses = gap.missing_evidence, gap.addresses_question == "yes"
        except Exception:
            # Degraded but still actionable: the retriever widens k and the no-repeat
            # guard still forces a different pass.
            missing = f"unclear; broaden the search for: {state['question']}"

    verdict = Verdict(
        grounded=grounded, unsupported_claims=unsupported, missing_evidence=missing
    )
    return {
        "verdict": verdict,
        "attempts": attempts,
        "trace": [
            {
                "node": "verify",
                "ms": round((perf_counter() - t0) * 1000, 1),
                "attempt": attempts,
                "model": model_for("verifier"),
                "claims": len(citations),
                "unsupported": len(unsupported),
                "grounded": grounded,
                "addresses_question": addresses,
                "prompt_tokens": tok_in,
                "completion_tokens": tok_out,
            }
        ],
    }


def build_refusal(state: AgentState) -> dict:
    """Terminal answer when the loop is exhausted without grounding.

    This is a FEATURE for compliance questions, not an error path. Without it, exhausting
    `max_attempts` would force the graph to emit the unverified answer anyway — which
    defeats the entire point of having a verifier. Saying "the corpus does not support
    this" is the only honest terminal state for an ungroundable question.
    """
    tried = state.get("query_history", [])
    sections = sorted({c["section"] for c in state.get("retrieved", [])})[:5]
    verdict = state.get("verdict") or {}
    return {
        "answer": (
            "I cannot answer this from the available sources.\n\n"
            f"After {state.get('attempts', 0)} retrieval attempts, no passage in the "
            "corpus supports a grounded answer.\n"
            + (
                f"Gap identified: {verdict.get('missing_evidence')}\n"
                if verdict.get("missing_evidence")
                else ""
            )
            + (f"Closest material reviewed: {', '.join(sections)}.\n" if sections else "")
            + f"Searches tried: {len(tried)}."
        ),
        "citations": [],
        "trace": [{"node": "refuse", "attempts": state.get("attempts", 0),
                   "queries_tried": len(tried)}],
    }
