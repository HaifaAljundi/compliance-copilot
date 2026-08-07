"""The graph's state schema — the contract every node reads and writes.

In LangGraph a node is a function `state -> partial update`. It returns a dict containing
ONLY the keys it changed, and LangGraph merges that into the running state using each
field's reducer. Nodes never mutate state in place and never see each other directly.

That indirection is what makes the eval A/B in evals/run_ab.py a one-argument change:
because `analyst` and `verifier` communicate only through these fields, the same node
functions can be wired into a linear graph or a cyclic one without touching either.

The reducer choice per field is the load-bearing design decision here, not the types.
"""

from __future__ import annotations

import uuid
from operator import add
from typing import Annotated, Literal, TypedDict

from langchain_core.documents import Document
from langgraph.graph.message import add_messages

# Where the supervisor can send control. A Literal rather than a bare str so a typo in a
# routing decision fails at the conditional edge instead of silently falling through to a
# default branch — a routing bug that degrades answers is far harder to spot than one that
# raises.
Route = Literal["retrieve", "act", "answer"]


class Chunk(TypedDict):
    """One retrieved passage. Mirrors the metadata written by ingest.py."""

    chunk_id: str       # stable across re-ingestion: sha256(doc_id|section_label|ordinal)
    doc_id: str
    section: str        # "Article 12" / "Schedule 1, Paragraph 1" / "Section 3.2.1"
    section_title: str
    title: str          # full document title, for a human-readable citation
    issuer: str
    domain: str         # "data-protection" | "aml-sanctions" | "aml-kyc"
    url: str
    text: str
    score: float


class Citation(TypedDict):
    """One atomic claim tied to the passage that supports it.

    Structured this way so groundedness is checkable WITHOUT an LLM first:
      - does `chunk_id` exist in state["retrieved"]?          (fabricated source)
      - is `quote` a literal substring of that chunk's text?   (fabricated quote)
    Both are free, deterministic, and catch a large share of hallucinations before any
    judge model forms an opinion. Only claims surviving those checks are worth spending
    a verifier call on.
    """

    claim: str
    chunk_id: str
    quote: str


class Verdict(TypedDict):
    """The verifier's structured output."""

    grounded: bool
    unsupported_claims: list[str]
    # The single most important field in this file. It is the verifier's SPECIFIC
    # complaint ("no passage states the notification deadline"), and it is what the
    # retriever turns into a rewritten query on loop-back. Without it the cycle re-runs
    # the same search, gets the same chunks, and rejects the same answer — three times the
    # cost for an identical conclusion. A cycle is only worth having if each pass differs.
    missing_evidence: str


class AgentState(TypedDict):
    """State flowing through the graph.

    Reducers, and why:

    `add_messages`  — appends, and replaces by message id. The standard chat reducer.
    `add` (operator)— list concatenation. Used ONLY for genuine accumulators.
    default         — last write wins. Used for everything representing "current value".
    """

    # --- conversation -----------------------------------------------------
    messages: Annotated[list, add_messages]

    # --- the question -----------------------------------------------------
    # The user's ORIGINAL wording, never rewritten. `search_query` drifts as the loop
    # rewrites it; the verifier must grade against this, or a rewrite that wanders off
    # topic produces an answer that is well-grounded in the wrong question — which scores
    # as a success on every metric except usefulness.
    question: str

    # --- routing ----------------------------------------------------------
    # Written by the supervisor's forced tool call; read by a pure conditional edge.
    route: Route

    # --- retrieval --------------------------------------------------------
    # The query actually sent to the vector store. Separate from `question` precisely so
    # the cycle has something to change.
    search_query: str

    # ACCUMULATOR. Every query tried, in order. Two jobs: it enforces "pass N must differ
    # from pass N-1" deterministically (cheaper and more reliable than trusting the model
    # to vary), and it is the evidence in a demo that the loop actually did something.
    query_history: Annotated[list[str], add]

    # Retrieval breadth. In state rather than a constant so the loop can widen it (5 -> 12)
    # on a failed verification — one of the three levers that make a retry productive.
    k: int

    # OVERWRITE, deliberately NOT `add`. This is the reducer mistake that quietly ruins a
    # cyclic RAG graph: with `add`, every loop-back appends another chunk set, so pass 3
    # carries three times the context, costs three times as much, and re-admits the weak
    # chunks that already failed verification in pass 1 — actively making each retry worse
    # than the last. Each retrieval REPLACES the working set.
    retrieved: list[Chunk]

    # --- answer -----------------------------------------------------------
    answer: str
    citations: list[Citation]

    # --- verification -----------------------------------------------------
    verdict: Verdict | None

    # Graceful loop bound. Distinct from LangGraph's `recursion_limit`, which raises
    # GraphRecursionError — an exception, not an answer. On exhausting `attempts` the graph
    # emits an explicit refusal, which for a compliance question is a CORRECT outcome, not
    # a failure. Also the A/B lever: the no-verifier arm simply never increments it.
    attempts: int

    # Bounds the analyst's inner ReAct loop. Nested inside the outer verifier cycle an
    # unbounded tool loop multiplies: 3 attempts x unbounded is unbounded.
    tool_calls_used: int

    # --- side effects -----------------------------------------------------
    # Minted once per run. Serves three roles at once, deliberately the same value:
    # the n8n webhook idempotency key, the trace correlation id, and the eval row key.
    # One id means a webhook delivery can be traced back to the exact retrieval that
    # justified it.
    run_id: str

    # The proposed n8n call, held in state BEFORE it fires so `interrupt()` can show a
    # human exactly what is about to happen. Separating request from result is what makes
    # "proposed but not yet approved" a representable state.
    action_request: dict | None
    action_result: dict | None

    # --- observability ----------------------------------------------------
    # ACCUMULATOR. One entry per node execution:
    #   {"node", "ms", "model", "prompt_tokens", "completion_tokens", "attempt"}
    # Every number in the eval — p95 latency, cost per query, per-node breakdown — is
    # computed from this. Instrumenting up front rather than retrofitting is the
    # difference between having those numbers on day 2 and not having them.
    trace: Annotated[list[dict], add]


def chunk_from_document(doc: Document, score: float = 0.0) -> Chunk:
    """Adapt a LangChain Document into the state's Chunk shape.

    An explicit adapter rather than passing Documents around: it pins the retriever's
    output to the fields citations actually need, so a metadata key renamed in ingest.py
    breaks HERE, loudly, instead of surfacing later as an empty citation field.
    """
    m = doc.metadata
    return Chunk(
        chunk_id=m.get("chunk_id", ""),
        doc_id=m.get("doc_id", ""),
        section=m.get("section", ""),
        section_title=m.get("section_title", ""),
        title=m.get("title", ""),
        issuer=m.get("issuer", ""),
        domain=m.get("domain", ""),
        url=m.get("url", ""),
        text=doc.page_content,
        score=score,
    )


def initial_state(question: str, *, k: int, run_id: str | None = None) -> AgentState:
    """Build a complete starting state.

    Every field is populated, including the ones nodes will overwrite. LangGraph tolerates
    partial input, but a node that reads a key no node has written yet raises KeyError at
    runtime — deep inside a graph execution, where the traceback names the reducer rather
    than the missing field. Filling everything up front trades a little verbosity for
    failures that point at their cause.
    """
    return AgentState(
        messages=[],
        question=question,
        route="retrieve",
        search_query=question,   # first pass searches the question as asked
        query_history=[],        # appended by the retriever, so the first query is recorded once
        k=k,
        retrieved=[],
        answer="",
        citations=[],
        verdict=None,
        attempts=0,
        tool_calls_used=0,
        run_id=run_id or str(uuid.uuid4()),
        action_request=None,
        action_result=None,
        trace=[],
    )
