"""Retriever node — searches the corpus, and rewrites the query on loop-back.

This node is where the verifier cycle either earns its cost or wastes it.

A cycle is only worth having if each pass differs from the last. If `verify -> retrieve`
re-runs the same query against the same index, it returns the same chunks, produces the
same answer, and gets the same rejection — burning `max_attempts` iterations to reach an
identical conclusion at three times the price. The A/B would then show the Verifier adding
latency and cost for no measurable gain, which is not a finding about verification; it is
a bug in this file.

So on a retry, three things change, in decreasing order of importance:

  1. The QUERY is rewritten from the verifier's specific complaint (`missing_evidence`),
     not merely rephrased. "No passage states the notification deadline" is a search
     instruction; "try again" is not.
  2. `k` is widened (5 -> 12), because the passage may have been ranked just outside the
     window rather than absent.
  3. A repeat query is refused outright — a deterministic guarantee, rather than trusting
     the model to vary its own output.
"""

from __future__ import annotations

import re
from time import perf_counter

from app.config import get_settings
from app.graph.state import AgentState, chunk_from_document
from app.llm import get_chat, model_for
from app.retrieval.store import get_store

# Kept short and imperative. The rewriter's job is to produce a SEARCH QUERY — keywords
# and legal phrasing that will embed near the missing passage — not a polite restatement
# of the question. Models default to the latter, so the instruction is explicit.
_REWRITE_PROMPT = """You rewrite a failed search query for a vector search over \
regulatory documents.

Original question: {question}

Queries already tried (do NOT repeat any of these):
{tried}

What the previous answer was missing: {missing}

Write ONE new search query targeting the missing information. Use the terminology a \
regulation would use, not conversational phrasing. Output only the query text, no \
explanation, no quotes."""


# Reasoning models wrap their chain of thought in <think>...</think> inline in `content`.
# llm.py sets reasoning_format="hidden" so Groq strips it server-side, but this is kept as
# a second line of defence: a model swap, a provider default change, or a dropped kwarg
# would otherwise reintroduce a failure that does not raise. It produced the literal query
# "<think>", which embedded and searched cleanly and returned pure noise while the trace
# recorded rewritten=True — a spinning loop scored as a productive one.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_STRAY_TAG = re.compile(r"</?think>", re.IGNORECASE)

# A search query has no business containing markup or being an essay. Anything failing
# this is treated as a failed rewrite rather than passed to the embedder.
_MAX_QUERY_CHARS = 300


def _clean_query(raw: str) -> str:
    """Extract a usable search query from a model response.

    Takes the LAST non-empty line, not the first: when reasoning leaks through, the
    payload trails the thinking. Returns "" if nothing survives, which the caller treats
    as "no rewrite happened" — visibly, in the trace.
    """
    text = _THINK_BLOCK.sub("", raw)
    # An UNTERMINATED <think> means the response was cut off mid-reasoning and never
    # reached its payload. Stripping just the tag would leave raw chain-of-thought as the
    # search query — the same silent corruption, one step further along. Treat it as no
    # answer at all.
    if _STRAY_TAG.search(text):
        return ""
    text = text.strip()
    lines = [ln.strip().strip('"').strip("`") for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    candidate = lines[-1]
    if len(candidate) > _MAX_QUERY_CHARS or "<" in candidate or ">" in candidate:
        return ""
    return candidate


def should_rewrite(state: AgentState) -> bool:
    """Pure predicate — no LLM, no I/O, so it is unit-testable offline.

    A rewrite is warranted only when a verifier has actually rejected an answer. Checking
    `attempts > 0` alone would rewrite on the first pass of a resumed run; checking the
    verdict alone would rewrite after a PASSING verdict that routed here for another
    reason.
    """
    verdict = state.get("verdict")
    return bool(state.get("attempts", 0) > 0 and verdict and not verdict["grounded"])


# Function words carry no retrieval signal but dominate a natural-language question's
# embedding. Dropping them yields a keyword query that is guaranteed textually different
# from the original while preserving its meaning — the last deterministic rung below.
_STOPWORDS = frozenset("""
a an the is are was were be been being do does did what when where which who whom whose
how why can could may might must shall should will would i you he she it we they me him
her them my your his its our their this that these those there here of in on at to for
with from by as and or but if then than so such about into over under between during
must need needs required require requires have has had must
""".split())

# The verifier phrases gaps as "No retrieved passage states X" / "no passage covers X".
# The useful part is X. Stripping the preamble converts a diagnosis into a query.
_GAP_PREAMBLE = re.compile(
    r"^\s*(?:no\s+(?:retrieved\s+)?(?:passage|excerpt|section|source|chunk)s?\s+"
    r"(?:in\s+the\s+corpus\s+)?(?:that\s+)?"
    r"(?:states?|covers?|mentions?|contains?|specif\w+|address\w+|supports?|provides?)\s*"
    r"(?:the\s+)?)|^\s*unclear;?\s*broaden\s+the\s+search\s+for:?\s*",
    re.IGNORECASE,
)


def _from_missing_evidence(missing: str) -> str:
    """Derive a query from the verifier's gap statement. Deterministic."""
    q = _GAP_PREAMBLE.sub("", missing or "").strip(" .:’'\"")
    return q if len(q) > 8 else ""


def _keywords(question: str) -> str:
    """Content words only. Deterministic, and always differs from the original phrasing."""
    words = [w.strip(".,;:?!\"'()") for w in question.split()]
    kept = [w for w in words if w and w.lower() not in _STOPWORDS and len(w) > 2]
    return " ".join(kept)


def _ask_model(state: AgentState, tried: list[str], insist: bool) -> str:
    verdict = state.get("verdict") or {}
    prompt = _REWRITE_PROMPT.format(
        question=state["question"],
        tried="\n".join(f"- {q}" for q in tried) or "- (none)",
        missing=verdict.get("missing_evidence") or "unclear; broaden the search",
    )
    if insist:
        prompt += (
            "\n\nYour previous attempt was empty or repeated a query above. Output a "
            "DIFFERENT query now. Keywords only. Never reply with nothing."
        )
    # The rewriter runs on the VERIFIER model, not the analyst's. The analyst just produced
    # the answer this query is meant to fix; asking it to diagnose its own retrieval
    # failure invites it to re-derive the same search terms.
    return _clean_query(get_chat("verifier").invoke(prompt).content or "")


def rewrite_query(state: AgentState) -> tuple[str, str]:
    """Produce a query that DIFFERS from every query already tried.

    Returns (query, strategy). Strategy is recorded in the trace so the eval can show
    which rung fired rather than only whether the text changed.

    The previous implementation fell back to `state["search_query"]` whenever the model
    returned nothing usable — which manufactured the exact repeat the no-repeat guard
    exists to prevent. Measured over one full A/B: 6 of 10 loops repeated a query, so a
    majority of the verifier's latency cost bought only a wider `k`.

    Root cause of the empty responses: with reasoning_format="hidden", a model that spends
    its whole output on reasoning returns content="". Not an error, just nothing — so no
    exception handler caught it.

    The ladder below degrades through progressively dumber but progressively more RELIABLE
    strategies. Every rung except the last is guaranteed to differ from what came before,
    because each is derived from different source text.
    """
    tried = state.get("query_history", [])
    seen = {q.lower() for q in tried}
    verdict = state.get("verdict") or {}

    def fresh(q: str) -> bool:
        return bool(q) and q.lower() not in seen

    # 1. Ask the model.
    try:
        if fresh(candidate := _ask_model(state, tried, insist=False)):
            return candidate, "llm"
    except Exception:
        pass

    # 2. Ask again, naming the failure. An empty response is usually a sampling outcome,
    #    and an explicit instruction shifts it — the same reasoning as the structured
    #    output retry in llm.py.
    try:
        if fresh(candidate := _ask_model(state, tried, insist=True)):
            return candidate, "llm-retry"
    except Exception:
        pass

    # 3. Use the verifier's own gap statement as the query. No model call, and it targets
    #    exactly what verification found missing.
    if fresh(candidate := _from_missing_evidence(verdict.get("missing_evidence", ""))):
        return candidate, "missing-evidence"

    # 4. Keyword-reduce the ORIGINAL question. Always textually different from the
    #    natural-language original, and embeds differently by dropping function words.
    if fresh(candidate := _keywords(state["question"])):
        return candidate, "keywords"

    # 5. Genuinely nothing new to try. Return unchanged and SAY SO, so a spinning loop is
    #    visible in the trace instead of being mistaken for a productive one.
    return state["search_query"], "exhausted"


def retrieve(state: AgentState) -> dict:
    """Search the corpus. Returns only the keys this node changes.

    Note what is NOT returned: `attempts` is owned by the verifier, `answer` by the
    analyst. A node that writes fields it does not own makes the graph's data flow
    impossible to follow and turns reducer bugs into action-at-a-distance.
    """
    t0 = perf_counter()
    settings = get_settings()

    retry = should_rewrite(state)
    if retry:
        query, strategy = rewrite_query(state)
        k = settings.k_escalated
        rewritten = query != state["search_query"]
    else:
        query, k, rewritten, strategy = state["search_query"], state["k"], False, None

    # with_score so the Chunk carries a real number. For a cosine strategy pgvector
    # returns DISTANCE (lower is closer), not similarity — recorded as-is rather than
    # silently inverted, so anything reading it sees the same scale the database uses.
    pairs = get_store().similarity_search_with_score(query, k=k)
    chunks = [chunk_from_document(doc, score=score) for doc, score in pairs]

    return {
        "retrieved": chunks,
        "search_query": query,
        "k": k,
        # Appended here, so query_history is written by exactly one node and its ordering
        # is trustworthy as the record of what the cycle actually tried.
        "query_history": [query],
        "trace": [
            {
                "node": "retrieve",
                "ms": round((perf_counter() - t0) * 1000, 1),
                "attempt": state.get("attempts", 0) + 1,
                "k": k,
                "hits": len(chunks),
                "retry": retry,
                # Distinguishes "we retried and changed the query" from "we retried and
                # the rewriter repeated itself". Without this the eval cannot tell a
                # productive loop from a spinning one.
                "rewritten": rewritten,
                # Which rung of the ladder produced this query: llm | llm-retry |
                # missing-evidence | keywords | exhausted. "exhausted" is the only value
                # that means the loop is spinning.
                "strategy": strategy,
                "model": model_for("verifier") if retry else None,
            }
        ],
    }
