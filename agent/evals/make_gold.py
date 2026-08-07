"""Generate gold questions FROM chunk text, then verify each one retrieves its source.

Why not hand-write them: the first 14 questions in gold.jsonl were written by reading
SECTION TITLES, and scored recall@5 = 100%. That number is optimistic by construction —
a question written from the title "Consent" naturally contains the words that retrieve
the chunk titled "Consent". It proves the pipeline works; it does not measure retrieval.

Questions generated from the BODY text are harder, because the body says things the title
does not. A question about "the controller must demonstrate consent was freely given"
shares far less surface vocabulary with its section heading.

Two filters make the output usable as ground truth:

  1. SELF-RETRIEVAL. A generated question is kept only if the chunk it was written from
     appears in its own top-10. A question its source cannot retrieve is ambiguous or
     badly worded — that is a question defect, not a retriever defect, and mixing the two
     makes the metric uninterpretable.

  2. NO ANAPHORA. Questions containing "this section", "the above", "it states" are
     dropped: they are only answerable with the chunk already in hand, which is not the
     task being measured.

    python -m evals.make_gold --per-doc 10 --out evals/gold_generated.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

from sqlalchemy import text as sql

from app.config import VECTOR_TABLE
from app.llm import get_chat
from app.retrieval.store import get_store, get_sync_engine

# Phrases that make a question unanswerable without the source already in front of you.
_ANAPHORA = (
    "this section", "this article", "the above", "the following", "it states",
    "the passage", "the excerpt", "this provision", "the text", "mentioned",
)

# A question naming its own answer's location is trivially retrievable and measures
# nothing. Rejected outright rather than edited: stripping the locator after the fact left
# dangling clauses ("...have of the DIFC Data Protection Law to obtain...") that are worse
# than the original, because they make retrieval fail for a grammatical reason.
_LOCATOR = re.compile(
    r"\b(?:article|section|paragraph|schedule)\s*\d", re.IGNORECASE
)


# NOTE: a PLAIN invoke, not with_structured_output.
#
# A schema whose only field is one string gives the model no reason to call a tool — it
# simply answers in prose, and Groq rejects the request with "Tool choice is required,
# but model did not call a tool". All 33 generations failed this way before the switch.
#
# Forced tool calling earns its cost when the output has SHAPE (several fields, a list,
# an enum). For "return one sentence", it is the wrong instrument: it adds a failure mode
# and buys nothing that stripping the response does not.


_PROMPT = """Passage from {issuer}, {section} ({section_title}):

\"\"\"{text}\"\"\"

Write ONE question that this passage answers. Requirements:
- Self-contained: a reader who has never seen this passage must understand it.
- Natural: how a compliance officer would actually phrase it.
- Specific enough that the answer is in this passage, not any passage.
- NEVER mention an article, section, paragraph or schedule NUMBER. Someone asking this
  question does not know where the answer lives — that is what they are asking. A question
  containing "Article 35" tells the retriever exactly where to look and makes the eval
  measure nothing.
- Do not name the law or the issuing body either. Ask about the SUBSTANCE."""


def sample_chunks(per_doc: int, seed: int = 7) -> list[dict]:
    """Sample chunks, one per section, biased toward substantial ones.

    One per section because two questions from the same section measure the same
    retrieval path twice and inflate apparent coverage.
    """
    rng = random.Random(seed)
    with get_sync_engine().connect() as conn:
        rows = conn.execute(
            sql(
                f"select metadata->>'doc_id' d, metadata->>'section' s, "
                f"metadata->>'section_title' st, metadata->>'issuer' iss, "
                f"metadata->>'chunk_id' cid, text "
                f"from {VECTOR_TABLE} where length(text) > 400"
            )
        ).fetchall()

    by_doc: dict[str, dict[str, list]] = {}
    for r in rows:
        by_doc.setdefault(r.d, {}).setdefault(r.s, []).append(r)

    picked = []
    for doc, sections in by_doc.items():
        keys = sorted(sections)
        rng.shuffle(keys)
        for sec in keys[:per_doc]:
            r = sections[sec][0]
            picked.append({"doc_id": r.d, "section": r.s, "section_title": r.st,
                           "issuer": r.iss, "chunk_id": r.cid, "text": r.text})
    return picked


def generate(chunk: dict) -> str | None:
    """One question per chunk. Returns None on any failure — reported, never silent."""
    try:
        msg = get_chat("analyst").invoke(
            _PROMPT.format(
                issuer=chunk["issuer"], section=chunk["section"],
                section_title=chunk["section_title"], text=chunk["text"][:2500],
            )
        )
    except Exception as exc:
        print(f"     generation error: {type(exc).__name__}: {str(exc)[:90]}")
        return None

    lines = [ln.strip().strip('"') for ln in (msg.content or "").splitlines() if ln.strip()]
    if not lines:
        return None
    # The question is the last non-empty line: any preamble ("Here is a question:")
    # precedes it, and reasoning_format="hidden" has already removed <think> blocks.
    q = lines[-1]
    return q if q.endswith("?") and 20 < len(q) < 300 else None


def self_retrieves(question: str, chunk: dict, k: int = 10) -> bool:
    """Does the source section come back in this question's own top-k?"""
    hits = get_store().similarity_search(question, k=k)
    return any(h.metadata.get("section") == chunk["section"]
               and h.metadata.get("doc_id") == chunk["doc_id"] for h in hits)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-doc", type=int, default=10)
    ap.add_argument("--out", type=Path, default=Path("evals/gold_generated.jsonl"))
    args = ap.parse_args()

    chunks = sample_chunks(args.per_doc)
    print(f"sampled {len(chunks)} chunks across {len({c['doc_id'] for c in chunks})} documents\n")

    kept, dropped_anaphora, dropped_retrieval, dropped_gen, dropped_locator = [], 0, 0, 0, 0
    for i, chunk in enumerate(chunks, 1):
        q = generate(chunk)
        if not q:
            dropped_gen += 1
            continue
        if any(p in q.lower() for p in _ANAPHORA):
            dropped_anaphora += 1
            print(f"  {i:>2} DROP (anaphora)  {q[:66]}")
            continue
        if _LOCATOR.search(q):
            dropped_locator += 1
            print(f"  {i:>2} DROP (locator)   {q[:66]}")
            continue
        if not self_retrieves(q, chunk):
            dropped_retrieval += 1
            print(f"  {i:>2} DROP (no self-hit) {q[:64]}")
            continue
        kept.append({
            "id": f"gen-{chunk['doc_id'][:6]}-{len(kept):02d}",
            "question": q,
            "doc_id": chunk["doc_id"],
            "gold_sections": [chunk["section"]],
            "answerable": True,
            "expected_facts": [],
        })
        print(f"  {i:>2} keep  {chunk['section']:<26} {q[:56]}")

    args.out.write_text("\n".join(json.dumps(r) for r in kept) + "\n", encoding="utf-8")
    print(f"\nkept {len(kept)}  |  dropped: {dropped_anaphora} anaphora, "
          f"{dropped_locator} locator, {dropped_retrieval} no-self-hit, "
          f"{dropped_gen} generation-failed")
    print(f"wrote {args.out}")
    # Reported, not hidden: the drop rate is itself a signal. A high no-self-hit rate
    # means either the generator writes vague questions or retrieval is weaker than the
    # hand-written set suggested.


if __name__ == "__main__":
    main()
