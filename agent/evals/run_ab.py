"""A/B the graph with and without the Verifier node.

Both arms run IDENTICAL node functions, the same retriever, the same models, the same
temperature, over the same gold set, in the same session. Only the edges differ — see
build_graph(verify_enabled=...). That shared-code property is what makes a measured
difference attributable to verification rather than to two different implementations.

Paired per question, not two independent averages: with ~40 questions, between-question
variance dwarfs the effect, and unpaired means would mostly measure which questions
landed in which run.

    python -m evals.run_ab                 # both arms, writes results.csv
    python -m evals.run_ab --limit 6       # smoke test

Cost note: this makes 2 x N graph runs, and the verified arm can loop up to max_attempts.
--limit exists so the harness itself can be debugged without paying for a full sweep.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.graph.build import build_graph
from app.graph.state import initial_state
from evals.gold import GoldItem, load_gold

RESULTS = Path(__file__).parent / "results.csv"

# Groq pricing, USD per 1M tokens. Approximate and provider-published — the ABSOLUTE
# numbers matter far less than the ratio between arms, which is what the A/B reports.
PRICE = {
    "openai/gpt-oss-120b": (0.15, 0.75),
    "qwen/qwen3.6-27b": (0.29, 0.59),
}


@dataclass
class RunResult:
    id: str
    arm: str
    answerable: bool
    refused: bool
    grounded: bool
    citations: int
    claims: int
    citations_rejected: int
    attempts: int
    distinct_queries: int
    total_queries: int
    latency_ms: float
    cost_usd: float
    errored: bool = False
    answer: str = ""
    trace: list[dict] = field(default_factory=list)
    # Retained for the post-hoc judge. Underscored: not written to the CSV.
    _citations: list = field(default_factory=list)
    _chunks: list = field(default_factory=list)


def _cost(trace: list[dict]) -> float:
    """Sum per-call token cost from the trace.

    Embeddings are Ollama-local and free, so they contribute latency but no cost — worth
    stating, because a reader comparing this to a hosted-embedding setup would otherwise
    assume the embedding spend was omitted by mistake.
    """
    total = 0.0
    for entry in trace:
        model = entry.get("model")
        if not model or model not in PRICE:
            continue
        pin, pout = PRICE[model]
        total += (entry.get("prompt_tokens", 0) / 1e6) * pin
        total += (entry.get("completion_tokens", 0) / 1e6) * pout
    return total


def run_one(graph, item: GoldItem, arm: str, k: int, recursion_limit: int) -> RunResult:
    t0 = time.perf_counter()
    try:
        out = graph.invoke(
            initial_state(item.question, k=k), {"recursion_limit": recursion_limit}
        )
    except Exception as exc:
        # A crashed run is recorded, not dropped. Silently omitting failures biases the
        # arm that crashes more — which would most likely be the verified one, since it
        # makes strictly more model calls.
        return RunResult(item.id, arm, item.answerable, refused=False, grounded=False,
                         citations=0, claims=0, citations_rejected=0, attempts=0,
                         distinct_queries=0, total_queries=0,
                         latency_ms=(time.perf_counter() - t0) * 1000, cost_usd=0.0,
                         # errored, NOT refused. A crash is not a refusal and is
                         # emphatically not a hallucination — see summarise().
                         errored=True,
                         answer=f"ERROR: {type(exc).__name__}: {exc}")

    trace = out.get("trace", [])
    queries = out.get("query_history", [])
    analyze_entries = [t for t in trace if t.get("node") == "analyze"]
    citations = out.get("citations", [])

    # ARM-INDEPENDENT OUTCOME CLASSIFICATION.
    #
    # An earlier version derived `refused` from the graph path (did a node named "refuse"
    # run?) and `grounded` from state["verdict"]. Both are properties the no-verify arm
    # CANNOT have — it has no refuse node and never writes a verdict — so both metrics
    # were guaranteed to read 0% for that arm before any model ran. The resulting "+100%"
    # deltas measured the wiring diagram, not the behaviour.
    #
    # Observed in the smoke run: the no-verify arm answered "The provided sources do not
    # cover the VAT registration threshold" — a correct refusal — and was scored as a
    # non-refusal.
    #
    # The definition below applies identically to both arms: an answer with zero VALIDATED
    # citations is a refusal, whatever its prose. Groundedness is judged post-hoc by
    # verify_answer(), outside the graph, so neither arm is scored using machinery the
    # other lacks.
    refused = len(citations) == 0

    return RunResult(
        id=item.id,
        arm=arm,
        answerable=item.answerable,
        refused=refused,
        grounded=False,  # filled by the post-hoc judge; never read from the graph
        citations=len(citations),
        claims=sum(t.get("claims", 0) for t in analyze_entries),
        citations_rejected=sum(t.get("citations_rejected", 0) for t in analyze_entries),
        attempts=out.get("attempts", 0),
        distinct_queries=len(set(queries)),
        total_queries=len(queries),
        latency_ms=(time.perf_counter() - t0) * 1000,
        cost_usd=_cost(trace),
        answer=out.get("answer", "")[:400],
        trace=trace,
        _citations=citations,
        _chunks=out.get("retrieved", []),
    )


def judge_groundedness(rows: list[RunResult]) -> None:
    """Score groundedness OUTSIDE the graph, identically for both arms.

    Uses the same per-claim entailment judge the Verifier node uses, but runs it on every
    row regardless of arm. This is the only way the comparison is fair: scoring the
    verified arm with its own verifier and the control arm with nothing would compare a
    measurement to its absence.

    A row with no citations is left ungrounded — correctly, since there is nothing to
    ground — and is separately counted as a refusal.
    """
    from app.graph.nodes.verifier import _judge_claim

    for r in rows:
        if not r._citations:
            continue
        by_id = {c["chunk_id"]: c for c in r._chunks}
        supported = 0
        for cit in r._citations:
            chunk = by_id.get(cit["chunk_id"])
            if not chunk:
                continue
            ok, _reason, _usage = _judge_claim(cit["claim"], chunk["text"])
            supported += int(ok)
        r.grounded = supported == len(r._citations) and supported > 0


def _pct(n: int, d: int) -> float:
    return (n / d * 100) if d else 0.0


def summarise(rows: list[RunResult], arm: str) -> dict:
    """Rates EXCLUDE errored runs; the error count is reported separately.

    An earlier version let a crashed run fall into `refused=False`, so on an unanswerable
    question it scored as a hallucination. The observed effect was severe: the entire
    reported "-20% hallucination" was ONE crashed run in the control arm. Crashes were
    also asymmetric between arms (4 vs 2), so they biased every rate at once.

    A crash is a third outcome. Folding it into either success or failure makes the
    reliability problem invisible AND corrupts the metric it hides inside.
    """
    a = [r for r in rows if r.arm == arm]
    ok = [r for r in a if not r.errored]
    ans = [r for r in ok if r.answerable]
    una = [r for r in ok if not r.answerable]
    lat = sorted(r.latency_ms for r in ok)

    def p(xs, q):
        return statistics.quantiles(xs, n=100)[q - 1] if len(xs) > 1 else (xs[0] if xs else 0)

    return {
        "arm": arm,
        "n": len(a),
        "errors": sum(1 for r in a if r.errored),
        # Answered = produced citations and did not refuse.
        "answered_rate_answerable": _pct(sum(1 for r in ans if not r.refused), len(ans)),
        # THE headline for the unanswerable set: refusing is the CORRECT behaviour there.
        "refusal_rate_unanswerable": _pct(sum(1 for r in una if r.refused), len(una)),
        "answered_rate_unanswerable": _pct(sum(1 for r in una if not r.refused), len(una)),
        # Its mirror: answering an unanswerable question IS the hallucination rate.
        # Answering a question the corpus cannot support IS the hallucination.
        "hallucination_rate_unanswerable": _pct(sum(1 for r in una if not r.refused), len(una)),
        "grounded_rate_answerable": _pct(sum(1 for r in ans if r.grounded), len(ans)),
        "citations_rejected_total": sum(r.citations_rejected for r in ok),
        "mean_attempts": statistics.mean([r.attempts for r in ok]) if ok else 0,
        "p50_latency_ms": p(lat, 50),
        "p95_latency_ms": p(lat, 95),
        "mean_cost_usd": statistics.mean([r.cost_usd for r in ok]) if ok else 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--arm", choices=["verify", "no-verify"], default=None)
    args = ap.parse_args()

    cfg = get_settings()
    items = load_gold()
    if args.limit:
        # Keep both classes represented — a limit that sliced off every unanswerable
        # question would silently remove the metric the A/B exists to move.
        ans = [i for i in items if i.answerable][: max(1, args.limit // 2)]
        una = [i for i in items if not i.answerable][: max(1, args.limit // 2)]
        items = ans + una

    arms = [args.arm] if args.arm else ["no-verify", "verify"]
    graphs = {
        "no-verify": build_graph(verify_enabled=False),
        "verify": build_graph(verify_enabled=True),
    }

    rows: list[RunResult] = []
    for arm in arms:
        print(f"\n=== arm: {arm} ({len(items)} questions) ===")
        for item in items:
            r = run_one(graphs[arm], item, arm, cfg.k_initial, cfg.recursion_limit)
            rows.append(r)
            mark = "REFUSED" if r.refused else ("grounded" if r.grounded else "answered")
            print(f"  {item.id:<8} {mark:<9} att={r.attempts} cites={r.citations} "
                  f"{r.latency_ms/1000:.1f}s")

    with RESULTS.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "arm", "answerable", "refused", "grounded", "citations", "claims",
                    "citations_rejected", "attempts", "distinct_queries", "total_queries",
                    "latency_ms", "cost_usd", "errored", "answer"])
        for r in rows:
            w.writerow([r.id, r.arm, r.answerable, r.refused, r.grounded, r.citations,
                        r.claims, r.citations_rejected, r.attempts, r.distinct_queries,
                        r.total_queries, round(r.latency_ms, 1), round(r.cost_usd, 6), r.errored,
                        r.answer.replace("\n", " ")])

    print("\njudging groundedness post-hoc (same judge, both arms)...")
    judge_groundedness(rows)

    print(f"wrote {RESULTS}")
    summaries = [summarise(rows, a) for a in arms]
    if len(summaries) == 2:
        print_table(summaries[0], summaries[1])
    else:
        for s in summaries:
            print(s)


def print_table(no_v: dict, v: dict) -> None:
    def row(label, key, fmt="{:.0f}%", better_high=True):
        a, b = no_v[key], v[key]
        d = b - a
        arrow = "" if abs(d) < 1e-9 else ("+" if d > 0 else "")
        print(f"  {label:<36} {fmt.format(a):>10} {fmt.format(b):>12}   {arrow}{fmt.format(d)}")

    print("\n" + "=" * 78)
    print(f"  {'metric':<36} {'no-verify':>10} {'with-verify':>12}   delta")
    print("=" * 78)
    row("errored runs (excluded from rates)", "errors", "{:.0f}")
    row("answered (answerable)", "answered_rate_answerable")
    row("grounded (answerable)", "grounded_rate_answerable")
    row("REFUSED (unanswerable) [higher=better]", "refusal_rate_unanswerable")
    row("HALLUCINATED (unanswerable)", "hallucination_rate_unanswerable")
    row("citations rejected (deterministic)", "citations_rejected_total", "{:.0f}")
    row("mean attempts", "mean_attempts", "{:.2f}")
    row("p50 latency (s)", "p50_latency_ms", "{:.1f}")
    row("p95 latency (s)", "p95_latency_ms", "{:.1f}")
    row("mean cost / query (USD)", "mean_cost_usd", "{:.5f}")
    print("=" * 78)
    print("  Latency and cost are EXPECTED to rise. The question is what they buy:")
    print("  look at hallucination on the unanswerable set.")


if __name__ == "__main__":
    main()
