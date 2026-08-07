# Compliance Copilot — agent service: LangGraph supervisor/worker over a UAE regulatory corpus

A supervisor/worker graph that answers compliance questions **with citations**, refuses
when the corpus cannot support an answer, and hands downstream actions to n8n workflows
behind a human approval gate.

Runs as a fourth service in `~/Sites/n8n/docker-compose.yml`, alongside n8n, its Postgres,
and pgvector. Nothing is published publicly; both directions of the n8n integration stay
on the compose network.

```
                    ┌── supervise ──┬─► respond ─────────────────────────► END
                    │  (forced tool │    (conversational only)
   question ────────┤   call)       └─► retrieve ─► analyze ─► verify ─┬─► act ─► END
                    │                      ▲                           │   (interrupt →
                    │                      └───── rewrite query ───────┤    n8n webhook)
                    │                        (not grounded,            │
                    │                         attempts remain)         └─► refuse ─► END
```

## Design decisions worth knowing before changing anything

| Decision | Why |
|---|---|
| Supervisor is a **forced tool call**, not the `langgraph-supervisor` library | Full control over context; the model cannot answer in prose or hedge. |
| Verifier uses a **different model** from the analyst | `config.py` refuses to start otherwise. A model grading its own output approves ~95%+ of it. |
| Verifier judges **per claim**, and never sees the question | Holistic "is this good?" grading is where approval rates go to 97%. Withholding the question stops it reasoning toward agreement. |
| `retrieved` uses the **overwrite** reducer | With `operator.add`, each loop-back accumulates chunk sets — 3× context cost, and chunks that already failed verification get resubmitted. |
| Loop-back **rewrites the query** from `missing_evidence` and widens `k` | A cycle is only worth having if each pass differs. Re-running the same query burns `max_attempts` to reach the same conclusion. |
| `act` is reachable **only** from a grounded verdict, **and** carries an idempotency key | Topology stops the cycle double-firing; the key stops a process restart doing it. Both are needed. |
| Citations validated **deterministically** before any LLM judges them | Fabricated chunk ids and non-verbatim quotes are caught for free. |
| Structured schemas use `Literal["yes","no"]`, never `bool` | `qwen/qwen3.6-27b` emits `"true"` as a string; Groq rejects the tool call server-side. |
| `reasoning_format="hidden"` on every model | Groq returns chain-of-thought **inline in `content`**. Naive parsing produced the literal search query `"<think>"`. |

## Setup

```bash
cd ~/Sites/n8n/agent
cp .env.example .env          # then fill in; PGVECTOR_PASSWORD must match ~/Sites/n8n/.env
uv venv && uv pip install -e ".[dev]"

# Ingest the corpus (needs pgvector up and `ollama pull nomic-embed-text` done)
PGVECTOR_HOST=127.0.0.1 PGVECTOR_PORT=5433 OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  python -m app.retrieval.ingest
```

Host-run commands need `PGVECTOR_HOST=127.0.0.1 PGVECTOR_PORT=5433` because pgvector
publishes only a **loopback** port; in-container the defaults (`pgvector:5432`) are correct.

## Running

```bash
# In the stack (production shape)
cd ~/Sites/n8n && docker-compose up -d agent

# Locally, for development
PGVECTOR_HOST=127.0.0.1 PGVECTOR_PORT=5433 OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  uvicorn app.api:app --port 8077
```

## API

| Endpoint | Caller | Purpose |
|---|---|---|
| `POST /ask` | n8n, UI | Ask a question; returns answer + resolved citations |
| `POST /ask/stream` | UI | SSE, one event per node — makes the retry cycle visible |
| `POST /resume` | n8n approval workflow | **Closes the HITL loop.** Releases a graph paused at the approval gate |
| `POST /ingest` | n8n schedule/watch | Re-ingest a document by `doc_id` |
| `GET /healthz` | container healthcheck | The only unauthenticated route |

All except `/healthz` require `Authorization: Bearer $AGENT_API_KEY`.

## Tests

```bash
pytest -m 'not integration'   # 49 tests, no network, no containers
pytest -m integration         # 4 tests, needs pgvector + Ollama
```

The offline set covers routing, reducers, query cleaning, and the webhook retry/idempotency
policy — everything whose failure is **silent** rather than loud.

## Evals

```bash
python -m evals.metrics              # recall@3/5/10 — run this FIRST
python -m evals.make_gold            # generate gold questions from chunk text
python -m evals.run_ab               # A/B with and without the Verifier → results.csv
python -m evals.run_ab --limit 6     # smoke test the harness
```

**Read the A/B numbers carefully.** Metrics are deliberately arm-independent: `refused` is
defined as "zero validated citations" and groundedness is judged post-hoc by the same judge
on both arms. An earlier version derived both from the graph path, which guaranteed a
+100% delta before any model ran — the no-verify arm has no `refuse` node and never writes
a verdict, so it could not score on either.

## Known limitations

- **Gold set bias.** 31% of generated questions contain an explicit locator ("Under DIFC
  Article 35…") because the generator prompt included the section label. Those are easier
  than real user questions, so recall is optimistic. Fix: strip locators before scoring.
- **`Part` context is not captured** for the DIFC law — Parts 1, 4–10 appear only in the
  table of contents, so a carry-forward would be confidently wrong. See the note in
  `corpus.py`. The table of contents is the honest source if this is ever wanted.
- **Corpus is not backed up.** `n8n_agent_corpus` and `n8n_agent_data` are managed volumes,
  not external ones — the corpus is re-downloadable and checkpoints are in-flight state.
  Deliberate, unlike the three `external: true` volumes which hold irreplaceable data.
- **`pdftotext` is a system dependency** (poppler-utils), installed in the Dockerfile. It
  is invisible to `pyproject.toml` and will fail at first ingestion if missing.
