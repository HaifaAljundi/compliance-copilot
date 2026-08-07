"""Retrieval metrics. No LLM involved — deterministic, free, and fast.

Measured FIRST, before any graph work. If recall@5 is poor, no prompt engineering
recovers it: the analyst cannot cite a chunk the retriever never returned, and every
downstream number (groundedness, hallucination rate) would be measuring the wrong
failure. Tuning a supervisor prompt against a retrieval problem is the classic way to
lose a day.

    python -m evals.metrics                  # recall@3/5/10 over gold.jsonl
    python -m evals.metrics --k 5 --verbose  # per-question, with misses explained
"""

from __future__ import annotations

import argparse

from app.retrieval.store import get_store
from evals.gold import GoldItem, load_gold


def retrieve_sections(question: str, k: int) -> list[tuple[str, str]]:
    """Return (doc_id, section) for the top-k chunks. Sections, not chunk ids.

    Deliberate: a section is split into several chunks, and any of them is a legitimate
    hit for a question about that section. Scoring on chunk_id would punish the retriever
    for returning the second half of the correct article, which is not an error.
    """
    hits = get_store().similarity_search(question, k=k)
    return [(h.metadata.get("doc_id", ""), h.metadata.get("section", "")) for h in hits]


def recall_at_k(items: list[GoldItem], k: int) -> tuple[float, list[tuple[GoldItem, bool, list]]]:
    """Fraction of answerable questions whose gold section appears in the top k.

    Unanswerable questions are excluded — they have no gold section by construction, so
    including them would silently inflate or deflate the score depending on how you
    counted them. They are scored separately, by refusal behaviour, once the graph exists.
    """
    scored = []
    answerable = [i for i in items if i.answerable]
    for item in answerable:
        got = retrieve_sections(item.question, k)
        hit = any(sec in item.gold_sections for _doc, sec in got)
        scored.append((item, hit, got))
    rate = sum(1 for _i, h, _g in scored if h) / len(scored) if scored else 0.0
    return rate, scored


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=None, help="single k (default: sweep 3/5/10)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    items = load_gold()
    n_ans = sum(1 for i in items if i.answerable)
    print(f"gold set: {len(items)} questions ({n_ans} answerable, {len(items) - n_ans} not)\n")

    for k in [args.k] if args.k else [3, 5, 10]:
        rate, scored = recall_at_k(items, k)
        flag = "PASS" if rate >= 0.7 else "BELOW TARGET"
        hits = sum(1 for _i, h, _g in scored if h)
        print(f"recall@{k:<3} {rate:.0%}   ({hits}/{len(scored)})  {flag}")
        if args.verbose:
            for item, hit, got in scored:
                if hit and not args.verbose:
                    continue
                mark = "ok  " if hit else "MISS"
                print(f"   {mark} {item.id}  want {item.gold_sections}")
                if not hit:
                    print(f"        got  {[s for _d, s in got]}")
            print()


if __name__ == "__main__":
    main()
