"""Analyst node — reasons over retrieved chunks, calls tools, emits answer + citations.

Built as a HAND-ROLLED ReAct loop (model -> tool_calls? -> execute -> model) rather than
`create_agent`. Two reasons beyond "the brief asked for it":

  * The loop is bounded by `tool_calls_used` in state. This node runs INSIDE the verifier
    cycle, so an unbounded inner loop multiplies: 3 outer attempts x unbounded inner is
    unbounded. A prebuilt agent hides that budget.
  * The final step is forced into a structured schema. A ReAct agent's natural output is
    prose, and prose cannot be checked against chunk ids.

Two phases inside one node:
  1. Optional tool use — the analyst may pull a full section or run a targeted search
     when the retrieved excerpts are insufficient.
  2. A single forced structured call producing the answer and its citations.

Citations are then validated DETERMINISTICALLY here, before any verifier sees them. A
fabricated chunk_id or a quote that is not literally present in its chunk is caught for
free — no model, no tokens, no judgement. Only claims surviving that are worth spending
a verifier call on, which is what keeps the Verifier's cost defensible in the A/B.
"""

from __future__ import annotations

from time import perf_counter

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from typing import Literal

from pydantic import BaseModel, Field

from app.config import get_settings
from app.graph.state import AgentState, Chunk, Citation, chunk_from_document
from app.llm import get_chat, invoke_structured, model_for, usage_of
from app.retrieval.store import get_store

_SYSTEM = """You are a compliance analyst. Answer ONLY from the provided excerpts.

Rules:
- Every factual claim must come from an excerpt. Never use outside knowledge.
- If the excerpts do not answer the question, say so plainly. An honest "the provided \
sources do not cover this" is a CORRECT answer, not a failure.
- Quote the source text verbatim when citing it. Do not paraphrase inside a quote.
- Cite using the excerpt's chunk_id.

You may call tools to retrieve more material before answering, but only if the excerpts \
are genuinely insufficient."""


class _Citation(BaseModel):
    claim: str = Field(description="One atomic factual assertion made in the answer.")
    chunk_id: str = Field(description="chunk_id of the excerpt supporting this claim.")
    quote: str = Field(description="VERBATIM span from that excerpt. Copy exactly.")


class _Answer(BaseModel):
    answer: str = Field(description="The answer, in prose, citing sections by name.")
    citations: list[_Citation] = Field(
        default_factory=list, description="One entry per factual claim."
    )
    # String enum, not bool — see the note in verifier.py. gpt-oss-120b happens to emit
    # booleans correctly, but keeping both schemas in the same style means swapping the
    # analyst model cannot silently reintroduce the failure.
    sufficient: Literal["yes", "no"] = Field(
        description="'no' if the excerpts do not support a complete answer."
    )


@tool
def search_corpus(query: str, k: int = 5) -> str:
    """Search the compliance corpus for additional passages."""
    hits = get_store().similarity_search(query, k=min(k, 10))
    return "\n\n".join(
        f"[{h.metadata.get('chunk_id')}] {h.metadata.get('section')} — "
        f"{h.metadata.get('section_title')}\n{h.page_content}"
        for h in hits
    ) or "No results."


@tool
def fetch_section(doc_id: str, section: str) -> str:
    """Fetch the full text of a specific section, e.g. doc_id='difc-dp-law-5-2020',
    section='Article 12'. Use when an excerpt appears truncated mid-provision."""
    from sqlalchemy import text as sql

    from app.config import VECTOR_TABLE
    from app.retrieval.store import get_sync_engine

    with get_sync_engine().connect() as conn:
        rows = conn.execute(
            sql(
                f"select text from {VECTOR_TABLE} "
                f"where metadata->>'doc_id' = :d and metadata->>'section' = :s "
                f"order by (metadata->>'chunk_index')::int"
            ),
            {"d": doc_id, "s": section},
        ).fetchall()
    return " ".join(r[0] for r in rows) if rows else f"No section {section!r} in {doc_id!r}."


TOOLS = [search_corpus, fetch_section]
_TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def format_excerpts(chunks: list[Chunk]) -> str:
    """Render chunks for the prompt.

    chunk_id is shown FIRST and in brackets because it is the token the model must copy
    into a citation. Burying it after the prose measurably increases fabricated ids.
    """
    return "\n\n".join(
        f"[{c['chunk_id']}] {c['issuer']} — {c['title']}\n"
        f"{c['section']}: {c['section_title']}\n{c['text']}"
        for c in chunks
    )


def validate_citations(
    citations: list[Citation], chunks: list[Chunk]
) -> tuple[list[Citation], list[str]]:
    """Deterministic groundedness. No LLM. Returns (valid, problems).

    Catches the two cheapest-to-detect hallucination classes outright:
      * fabricated source — chunk_id not among the retrieved chunks
      * fabricated quote  — quote text not literally present in that chunk

    Quote matching normalises whitespace only. Nothing else: any looser comparison (case
    folding, punctuation stripping, fuzzy ratio) would start accepting paraphrase as
    verbatim, which is precisely the thing being tested for.
    """
    by_id = {c["chunk_id"]: c for c in chunks}
    valid: list[Citation] = []
    problems: list[str] = []

    for cit in citations:
        chunk = by_id.get(cit["chunk_id"])
        if chunk is None:
            problems.append(f"fabricated chunk_id {cit['chunk_id']!r} for claim: {cit['claim'][:70]}")
            continue
        needle = " ".join(cit["quote"].split())
        haystack = " ".join(chunk["text"].split())
        if needle and needle not in haystack:
            problems.append(
                f"quote not found in {cit['chunk_id']} for claim: {cit['claim'][:70]}"
            )
            continue
        valid.append(cit)
    return valid, problems


def analyze(state: AgentState) -> dict:
    """Run the ReAct loop, then produce a validated structured answer."""
    t0 = perf_counter()
    settings = get_settings()
    chunks = state["retrieved"]

    if not chunks:
        # Never invent an answer with no evidence. Reaching here means retrieval failed;
        # saying so is both correct and the signal the verifier needs.
        return {
            "answer": "No source material was retrieved for this question.",
            "citations": [],
            "trace": [{"node": "analyze", "ms": round((perf_counter() - t0) * 1000, 1),
                       "tool_calls": 0, "no_evidence": True}],
        }

    llm_tools = get_chat("analyst").bind_tools(TOOLS)
    messages = [
        SystemMessage(content=_SYSTEM),
        HumanMessage(
            content=f"Question: {state['question']}\n\nExcerpts:\n{format_excerpts(chunks)}"
        ),
    ]

    # --- phase 1: bounded ReAct loop -------------------------------------
    used = state.get("tool_calls_used", 0)
    budget = settings.max_tool_calls
    extra: list[Chunk] = []

    tok_in = tok_out = 0
    while used < budget:
        ai: AIMessage = llm_tools.invoke(messages)
        u = usage_of(ai); tok_in += u["prompt_tokens"]; tok_out += u["completion_tokens"]
        messages.append(ai)
        if not ai.tool_calls:
            break
        for call in ai.tool_calls:
            fn = _TOOLS_BY_NAME.get(call["name"])
            result = fn.invoke(call["args"]) if fn else f"Unknown tool {call['name']!r}."
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
            used += 1
        if used >= budget:
            # Say so explicitly. Silently truncating a tool loop produces an answer built
            # on partial evidence with no indication that anything was cut off.
            messages.append(HumanMessage(content="Tool budget exhausted. Answer with what you have."))
            break

    # Chunks pulled by search_corpus during the loop must join `retrieved`, or a citation
    # to one of them fails validation as a "fabricated" id — the model did see it.
    if used > state.get("tool_calls_used", 0):
        seen = {c["chunk_id"] for c in chunks}
        for msg in messages:
            if isinstance(msg, ToolMessage):
                for line in str(msg.content).splitlines():
                    if line.startswith("[") and "]" in line:
                        cid = line[1 : line.index("]")]
                        if cid and cid not in seen:
                            seen.add(cid)
        extra = _rehydrate(seen - {c["chunk_id"] for c in chunks})

    # --- phase 2: forced structured answer -------------------------------
    all_chunks = chunks + extra

    # A FRESH conversation, deliberately not `messages`.
    #
    # Continuing the ReAct history here fails: that history ends with the model answering
    # in prose, so it keeps answering in prose, and Groq rejects the request outright with
    # "Tool choice is required, but model did not call a tool". Forced-tool output and a
    # free-text conversation do not mix — the history's own momentum defeats the
    # constraint.
    #
    # Phase 1's job was to GATHER evidence, and its product is `all_chunks`. Passing that
    # forward instead of the transcript is both what makes the structured call reliable
    # and what keeps the prompt short: tool call/response pairs are pure overhead once
    # their retrieved text is in hand.
    result, final_usage = invoke_structured(
        "analyst", _Answer,
        [
            SystemMessage(content=_SYSTEM),
            HumanMessage(
                content=(
                    f"Question: {state['question']}\n\n"
                    f"Excerpts:\n{format_excerpts(all_chunks)}\n\n"
                    "Give the final answer with one citation per factual claim. "
                    "Each quote must be copied verbatim from an excerpt."
                )
            ),
        ]
    )
    raw = [Citation(claim=c.claim, chunk_id=c.chunk_id, quote=c.quote) for c in result.citations]
    valid, problems = validate_citations(raw, all_chunks)

    return {
        "answer": result.answer,
        "citations": valid,
        "retrieved": all_chunks if extra else chunks,
        "tool_calls_used": used,
        "trace": [
            {
                "node": "analyze",
                "ms": round((perf_counter() - t0) * 1000, 1),
                "model": model_for("analyst"),
                "tool_calls": used - state.get("tool_calls_used", 0),
                "claims": len(raw),
                # Citations rejected by the deterministic check. A non-zero value here is
                # a hallucination the graph caught for free, before any verifier ran.
                "citations_rejected": len(problems),
                "self_reported_sufficient": result.sufficient == "yes",
                "prompt_tokens": tok_in,
                "completion_tokens": tok_out,
            }
        ],
    }


def _rehydrate(chunk_ids: set[str]) -> list[Chunk]:
    """Load chunks the analyst discovered via tools, so their citations validate."""
    if not chunk_ids:
        return []
    from sqlalchemy import text as sql

    from app.config import VECTOR_TABLE
    from app.retrieval.store import get_sync_engine

    with get_sync_engine().connect() as conn:
        rows = conn.execute(
            sql(
                f"select text, metadata from {VECTOR_TABLE} "
                f"where metadata->>'chunk_id' = any(:ids)"
            ),
            {"ids": list(chunk_ids)},
        ).fetchall()

    from langchain_core.documents import Document

    return [chunk_from_document(Document(page_content=r[0], metadata=r[1])) for r in rows]
