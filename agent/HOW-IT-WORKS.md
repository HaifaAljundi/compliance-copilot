# How it works

Written so that coming back to this in three months, you can rebuild the mental model
without re-reading the code. `README.md` is the reference (commands, API, setup); this is
the explanation.

Built 2026-08-07 on top of the existing `~/Sites/n8n` stack.

---

## 1. What this is, in one paragraph

A Python service that answers questions about compliance documents, and refuses to answer
when the documents do not support one. Every factual claim it makes carries a verbatim
quote and a section reference. When an answer implies a downstream action ("file a
report"), it does not act — it pauses, waits for a human to approve through an n8n
workflow, and only then fires a webhook. It runs as a fourth container in your existing
n8n stack and is reachable only from inside that stack.

---

## 2. Where it sits

```
Browser / 3rd-party ──HTTPS──▶ Caddy (jump host) ──▶ n8n :5678
                                                       │  ▲
                                    http://agent:8000  │  │  http://n8n:5678/webhook/*
                                                       ▼  │
                                            ┌──────────────────────┐
                                            │  agent (this thing)  │
                                            └──────────┬───────────┘
                                                       │ pgvector:5432
                                            ┌──────────▼───────────┐
                                            │ pgvector (corpus)    │◀── also read/written
                                            └──────────────────────┘    by n8n's PGVector node
                                                       ▲
                                        host.docker.internal:11434 (Ollama, embeddings)
```

**Nothing new is exposed publicly.** The agent publishes no host port. n8n reaches it by
container name; it reaches n8n by container name. Your `pf` anchor and Caddy config were
not touched. pgvector publishes `127.0.0.1:5433` only — loopback, so it is not LAN-visible
and needs no firewall rule (unlike n8n's `:5678`, which binds `0.0.0.0` and is exactly why
the pf anchor exists).

**Four services now:** `n8n`, `n8n_postgres` (n8n's own DB, untouched), `n8n_pgvector`
(the corpus), `n8n_agent`.

---

## 3. What happens when you ask a question

```
            ┌──────────────┐
  question ─▶  supervise   │  forced tool call → "retrieve" | "act" | "answer"
            └──────┬───────┘
                   │
      ┌────────────┼────────────┐
      ▼            ▼            ▼
  respond       retrieve      act              respond = greetings only; it has NO
   (END)           │           │               documents and is told to refuse facts
                   ▼           │
               analyze         │
                   │           │
                   ▼           │
                verify ────────┤ grounded + action requested
                   │           │
      ┌────────────┼───────────┴──────┐
      │            │                  │
   not grounded    grounded        refuse           attempts exhausted
   attempts left   no action      (explicit "I cannot answer")
      │            │                  │
      ▼          (END)              (END)
   retrieve  ← rewritten query, k 5→12
```

**Step by step, for "If processing relies on consent, what must the controller demonstrate?"**

1. **supervise** — one cheap LLM call, forced to emit a `route` tool with an enum. It sees
   only the question, never the corpus. Returns `"retrieve"`.
2. **retrieve** — embeds the question with Ollama (`nomic-embed-text`, 768 dims), searches
   pgvector by cosine distance, returns the top 5 chunks with their metadata.
3. **analyze** — a hand-written ReAct loop. The model may call `search_corpus` or
   `fetch_section` if the excerpts look insufficient (bounded by a tool budget). Then a
   second, *fresh* call forced into a structured schema produces `answer` +
   `citations[{claim, chunk_id, quote}]`.
4. **Deterministic check** (inside `analyze`, no LLM) — does each `chunk_id` exist in what
   was retrieved? Is each `quote` a literal substring of that chunk? Anything failing is
   discarded. This is free and catches fabricated sources and fabricated quotes.
5. **verify** — for each surviving claim, a *different model* is asked "is this claim
   stated by this passage, yes/no", seeing **only the claim and the passage**. If all pass:
   grounded, done.
6. **If not grounded** — a second call (this one does see the question) writes
   `missing_evidence`: "no passage states the notification deadline". The graph loops back
   to `retrieve`, which turns that into a **new search query** and widens `k` to 12.
7. **After 3 attempts** — `refuse` produces an explicit "I cannot answer this from the
   available sources", listing what was searched and what was closest.

---

## 4. The files, and what each one owns

```
agent/
├── app/
│   ├── config.py           ← ALL environment config + the n8n storage contract constants
│   ├── llm.py              ← the ONLY file that names a model
│   ├── api.py              ← FastAPI: /ask /ask/stream /resume /ingest /healthz
│   ├── ui/index.html       ← single-file demo UI (SSE)
│   ├── retrieval/
│   │   ├── corpus.py       ← WHAT to ingest (source URLs + per-document heading patterns)
│   │   ├── ingest.py       ← HOW: fetch → pdftotext → section-split → chunk → embed → upsert
│   │   └── store.py        ← the ONLY file that issues DDL or names a column
│   ├── graph/
│   │   ├── state.py        ← AgentState + reducers. The contract every node reads/writes.
│   │   ├── edges.py        ← routing functions. PURE — no LLM, no I/O.
│   │   ├── build.py        ← the ONLY file that knows the graph's shape
│   │   └── nodes/          ← supervisor, retriever, analyst, verifier, action, responder
│   └── tools/n8n.py        ← webhook client: HMAC, idempotency, retry policy
├── evals/
│   ├── gold.jsonl          ← 48 questions (43 answerable, 5 deliberately not)
│   ├── make_gold.py        ← generates questions FROM chunk text, filters by self-retrieval
│   ├── metrics.py          ← recall@k. No LLM. Run this first.
│   └── run_ab.py           ← A/B with and without the Verifier → results.csv
└── tests/                  ← 55 offline (no network) + 4 integration
```

**The ownership rules are the point.** Each is a single place a whole class of bug can
live:

| File | Owns | So that… |
|---|---|---|
| `build.py` | graph shape | the A/B is a one-argument change, not a second implementation |
| `llm.py` | model names | swapping the verifier is one env var |
| `store.py` | table + column names | the n8n contract cannot drift across files |
| `edges.py` | routing | control flow is testable with no model and no database |
| `config.py` | env + constants | nothing is configurable that shouldn't be |

---

## 5. The eight decisions that matter

### 5.1 Supervisor uses a forced tool call, not the prebuilt library

`with_structured_output` on a Pydantic model compiles to a tool the provider is *required*
to emit. The model cannot answer in prose, hedge, or apologise — the API contract only
permits that tool with that enum. Invalid routes are rejected provider-side.

### 5.2 `retrieved` overwrites; `query_history` accumulates

The single most consequential line in `state.py`. With `Annotated[list, operator.add]`,
every loop-back would *append* another chunk set — pass 3 would carry 15 chunks instead of
5, cost 3× more, and re-submit the chunks that already failed verification in pass 1. Each
retrieval **replaces** the working set. `query_history` genuinely is an accumulator, and
it's what enforces "don't repeat a search".

### 5.3 The verifier cannot be the analyst

`config.py` **refuses to start** if `analyst_model == verifier_model`. A model grading its
own output approves it ~95%+ of the time, which would flatten the A/B into "no difference"
and silently destroy the headline result.

### 5.4 The judge does not see the question

It sees one claim and one passage. Given the question, a model reasons *toward* the answer
being reasonable — it fills gaps from its own knowledge and calls the result supported.
Withholding the question removes the material it would use to do that.

### 5.5 Citations are checked deterministically before any model judges them

Because a citation carries `chunk_id` + a verbatim `quote`, two hallucination classes are
catchable with plain string operations: fabricated sources and fabricated quotes. Free,
instant, no tokens. **This turned out to be the system's actual groundedness mechanism** —
it rejected 12–30 citations per eval run, in both A/B arms.

### 5.6 The retry must change the search, or the cycle is theatre

If `verify → retrieve` re-runs the same query, you get the same chunks, the same answer,
and the same rejection — at 3× the cost. So on retry, `retriever.py` walks a ladder until
it produces a query not already tried:

1. ask the model to rewrite from `missing_evidence`
2. ask again, naming the failure
3. **free**: strip "No retrieved passage states…" off the gap statement — the gap *is* the query
4. **free**: keyword-reduce the original question (drop stopwords)
5. give up, and **label it `exhausted`** in the trace so a spinning loop is visible

Rungs 3 and 4 cost nothing, so reliability improved without adding latency.

### 5.7 The action node cannot fire twice

Two independent protections, both needed:
- **Topology**: `act` is reachable *only* from a grounded verdict. The analyst can never
  reach it, so a loop-back cannot re-run it.
- **Idempotency key**: one `run_id` per run, sent on every attempt including retries.
  Topology cannot protect against a process restart or a lost HTTP response; this can.

### 5.8 Checkpointing is load-bearing, not decorative

`act` calls `interrupt()`. LangGraph persists state and returns. The process may exit. An
n8n workflow shows the proposed action to a human. Their decision POSTs to `/resume` and
execution continues **from the checkpoint, in a different process**. This was verified by
killing the container mid-pause.

---

## 6. Sharing one corpus with n8n

The corpus is written and read by two different LangChain implementations in two languages,
whose defaults **disagree on three of four column names**:

| Column | n8n node default | Python default | |
|---|---|---|---|
| id | `id` | `langchain_id` | ✗ |
| content | `text` | `content` | ✗ |
| embedding | `embedding` | `embedding` | ✓ |
| metadata | `metadata` | `langchain_metadata` | ✗ |

Left alone, Python creates a table n8n **silently cannot read** — empty results, not an
error. So `store.py` overrides Python's names to match n8n's, and n8n's node works with its
Column Names section left untouched (the side a human configures by hand is the side that
should have nothing to get wrong).

Additionally: `langchain-postgres` creates `metadata` as `json`, but the `@>` containment
operator every metadata filter uses **only exists for `jsonb`**. Reads and writes would
work; only *filtered* queries would fail. `store.py` coerces the column after creation.

`verify_contract()` asserts all of this against the live database. Run it after any
`langchain-postgres` upgrade.

**768 dimensions is fixed for the life of the corpus.** Changing the embedding model means
dropping and re-ingesting everything.

---

## 7. How the corpus is built

Source PDFs → `pdftotext -layout` → per-document heading regex → sections → size-split
*within* a section → embed → upsert.

**Sections are found first, and chunks never span a section boundary.** A generic character
splitter cuts wherever the budget runs out — routinely mid-article — producing a chunk that
belongs to Article 12 and Article 13 at once. There is no honest `section` value for such a
chunk, so it cannot be cited or verified. Retrieval would still look fine; citation quality
would be unfixable downstream.

Three real parsing traps, all handled in `ingest.py` / `corpus.py`:
- **Table-of-contents lines** (dot leaders) are stripped — they're dense with heading words,
  score well on almost any query, and push real content out of top-k.
- **The two issuers number differently** — DIFC uses `12.  Consent`, CBUAE uses `1.1.  Purpose`
  (trailing dot). One regex would mangle one of them, so the pattern is per-document.
- **Schedules restart numbering at 1**, so the law's "Article 1" collided with a schedule's
  paragraph 1. Now cited as `Schedule 1, Paragraph 1` vs `Article 1`.

**Not captured, deliberately**: the DIFC "Part" headings. Parts 1 and 4–10 appear only in
the table of contents, so a carry-forward had nothing to terminate it and labelled 44
sections spanning Articles 1–65 as "Part 3B". In a compliance citation a confidently wrong
locator is worse than a missing one, and the law self-cites as "Article 32" anyway.

---

## 8. What the evaluation actually found

| Metric | No Verifier | With Verifier |
|---|---:|---:|
| Grounded (answerable, n=43) | 95% | 98% |
| Refused (unanswerable, n=5) | 100% | 100% |
| Citations rejected (deterministic) | 12 | 30 |
| p95 latency | 15.3 s | 32.1 s |
| Cost / query | $0.00105 | $0.00280 |

**The verifier's loop costs 2.1× p95 latency and 2.7× spend for +2% groundedness — which
is smaller than run-to-run variance.** At 98% recall@5 the retriever usually finds the
right passage first time, so the loop has little to find.

**The deterministic citation check is doing the work**, in both arms, for zero tokens.

Read this as: *structured citations are what make groundedness checkable; the LLM judging
is the marginal part.* That is a more useful conclusion than "the verifier helps", and it
only surfaced because the metrics were made arm-independent.

### Why the metrics are shaped oddly

Three times the eval reported good numbers for the wrong reason:

1. **Path-based metrics.** `refused` was originally "did a node named `refuse` run?" — a
   property the control arm *cannot have*. It reported +100% before any model ran. Now:
   refusal is "zero validated citations", applied identically to both arms, and
   groundedness is judged post-hoc outside the graph.
2. **A crash counted as a hallucination.** The error path set `refused=False`, so on an
   unanswerable question a crash scored as a hallucination. The entire reported "−20%
   hallucination" was one crashed run. Errors are now a third outcome, excluded from rates.
3. **The rewriter manufactured its own repeats.** Its failure path returned the previous
   query — the exact thing the no-repeat guard existed to prevent. 6 of 10 loops repeated a
   search. Fixed by the ladder in §5.6; now 0 of 11.

---

## 9. Operating it

```bash
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
cd ~/Sites/n8n

docker-compose ps                       # 4 services, all healthy
docker logs n8n_agent --tail 30
docker-compose up -d agent              # after a code change: add --build
```

Host-run scripts need `PGVECTOR_HOST=127.0.0.1 PGVECTOR_PORT=5433` because pgvector
publishes only a loopback port. Inside containers the defaults are correct.

```bash
cd agent
pytest -m 'not integration'             # 56 tests, no network, no containers
pytest -m integration                   # 6 tests, needs pgvector + Ollama
python -m evals.metrics                 # recall@k — always run this first
python -m evals.run_ab                  # the A/B → results.csv
```

**If retrieval is bad, no amount of prompt work saves it.** `metrics.py` needs no LLM and
runs in seconds; check it before touching the graph.

---

## 10. What is not done

- **n8n writing to the corpus is unverified.** Python→n8n reads are proven; n8n→Python
  writes need a workflow built in the n8n UI (table `compliance_chunks`, all Column Names
  left at default).
- **The eval is under-powered.** 5 unanswerable questions means that column can only report
  multiples of 20%. Single run, no variance bars.
- **The gold set is optimistic.** 31% of generated questions leak a locator ("Under DIFC
  Article 35…") because the generator prompt included the section label.
- **Corpus and checkpoints are not backed up** — deliberately. Both are regenerable, so
  their volumes are managed rather than `external: true` like the three that hold
  irreplaceable data.

## 11. Where I'd go next

1. Grow the unanswerable set to ~15 and add multi-section questions — give the verifier
   something to actually catch. The current corpus is too easy to show its value.
2. Three seeded runs per arm for variance bars. ~25 minutes, and it settles whether +2% is
   real.
3. Strip locators from generated questions and re-measure recall honestly.
4. Consider routing only *low-confidence* answers through the verifier rather than all of
   them — the obvious response to a 2.7× cost for a marginal gain.
