# n8n AI features — design

Date: 2026-07-29
Instance: n8n.example.com (n8n 2.31.6, Colima Docker, `~/Sites/n8n`)
Status: awaiting review

> **Naming note.** "copilot" in this document means n8n's own in-editor *build with AI*
> assistant (goal 2 below, dropped under Constraint C1). It is unrelated to **Compliance
> Copilot**, the project in this repository.

## Context

The instance appeared to have "no AI". It does not. `@n8n/n8n-nodes-langchain` is
bundled in the image and provides agents, chains, tools, memory, MCP, rerankers,
text splitters, embeddings and vector stores, plus 25 chat-model nodes.

Nothing is installed or enabled to get these. They are inert only because no LLM
credential exists, and n8n does not surface the AI node category prominently until
one is configured.

Two things are genuinely absent and are what this design adds:

1. LLM credentials, so the AI nodes do something.
2. A vector store + embedding model, so retrieval-augmented workflows are possible.

## Goals

| # | Goal | Status |
|---|------|--------|
| 1 | AI nodes usable inside workflows | in scope |
| 2 | In-editor AI copilot ("build with AI") | **dropped** — see Constraint C1 |
| 3 | RAG over own documents | in scope |

Provider constraint from the operator: own infrastructure only — Groq (already in
use for another internal project) and local Ollama. No new paid LLM accounts.

## Verified findings

All checked against the running container on 2026-07-29, not assumed.

| Finding | Evidence |
|---|---|
| AI nodes present | `ls @n8n/n8n-nodes-langchain/dist/nodes` → agents, chains, embeddings, vector_store, memory, tools, mcp, Guardrails, ModelSelector |
| Groq reachable from container | `wget https://api.groq.com/openai/v1/models` → HTTP 401 (auth failure = DNS + egress OK) |
| Ollama reachable from container | `wget http://host.docker.internal:11434/api/tags` → OK. Also resolves via `host.lima.internal` and `192.168.5.2`. Works despite `lsof` showing `127.0.0.1:11434`, because Colima forwards through. |
| LM Studio needs auth | `curl localhost:1234/v1/models` → `invalid_api_key`, requires `Authorization: Bearer` |
| **pgvector unavailable** | `select count(*) from pg_available_extensions where name='vector'` → `0`. `postgres:16.14-alpine` does not ship it. |
| **No embedding model in Ollama** | `/api/tags` → only `qwen3.6:27b-coding-nvfp4` and `gemma4:latest`, both chat-only |
| No AI env vars set | `docker exec n8n env \| grep -i 'n8n_ai\|instance_ai'` → empty; `/rest/settings` reports no AI feature flags |

### Constraint C1 — the copilot cannot run on Groq or Ollama

`@n8n/instance-ai` (the module behind the build-with-AI copilot) declares exactly
two model dependencies:

```
"@langchain/anthropic": "catalog:"
"@ai-sdk/anthropic":    "catalog:"
```

There is no OpenAI adapter, no Groq, no Ollama. `N8N_INSTANCE_AI_MODEL` defaults to
`anthropic/claude-opus-4-8` and Anthropic is the only provider package present.
`N8N_INSTANCE_AI_MODEL_URL` can redirect the endpoint, but it speaks the **Anthropic**
wire format; Groq and LM Studio are OpenAI-shaped and will not work behind it.

No combination of environment variables makes the copilot work on the chosen
providers. Goal 2 is therefore dropped rather than worked around. Revisit if n8n
adds provider adapters, or if an Anthropic key or an Anthropic-shaped proxy
(e.g. LiteLLM's `/v1/messages`) becomes acceptable later.

## Design

### Part A — chat model credentials (goal 1)

No configuration change, no restart. Credentials only, added through the n8n UI.

- **Groq** (`LmChatGroq`) — for anything needing speed or a large model. Restricted
  to 3 approved models; `gpt-oss-120b` for text and
  `qwen3.6-27b` for vision are already in use by another internal project.
- **Ollama** (`LMChatOllama`) — base URL `http://host.docker.internal:11434`,
  models `gemma4:latest` or `qwen3.6:27b-coding-nvfp4`. No egress, no per-token
  cost, no key.

Credentials are encrypted at rest with the existing `N8N_ENCRYPTION_KEY`, so
`backup.sh` already covers them with no change.

### Part B — pgvector container (goal 3)

A **new, separate** Postgres instance. The existing `postgres` service holds live
n8n execution and credential data; it is deliberately not modified. Swapping its
image to `pgvector/pgvector:pg16` would work (same major version) but puts the
production datastore in the path of a feature experiment for no benefit.

New service in `docker-compose.yml`, following the conventions already established
in that file:

- image `pgvector/pgvector:pg16`, **pinned** (never `:latest`)
- `container_name: n8n_pgvector`
- external, pre-created volume `n8n_pgvector_data`, so `docker-compose down -v`
  can never delete it and `backup.sh` can find it by a stable name
- `mem_limit: 1g` — same blast-radius cap as the existing `postgres` service
- **no published host port** — the existing `postgres` service publishes none
  either; n8n reaches it over the compose network by service name. This avoids
  a host port collision with the other Postgres instances on this machine.
- healthcheck mirroring the existing `pg_isready` one
- `POSTGRES_PASSWORD` from a new `.env` key `PGVECTOR_PASSWORD`
- `CREATE EXTENSION vector;` run once after first start

### Part C — embeddings and RAG workflow (goal 3)

- `ollama pull nomic-embed-text` (~274 MB, 768 dimensions) for `EmbeddingsOllama`.
  Runs locally: no egress, no cost.
- Ingest workflow: Default Data Loader → Recursive Character Text Splitter →
  Embeddings Ollama → PGVector Store (insert mode)
- Retrieval workflow: AI Agent or Question-and-Answer Chain, with PGVector Store
  (retrieve mode) as the retriever and Groq or Ollama as the chat model

## Non-goals

- Enabling the AI copilot (Constraint C1)
- Modifying the existing n8n Postgres service or its data
- Publishing any new host port
- Exposing anything additional through the jump host
- Changing the forward-auth gating on the editor

## Risks

| Risk | Mitigation |
|---|---|
| `docker-compose up -d` restarts the n8n container unnecessarily | Adding a new service should not recreate existing ones; verify n8n uptime after applying and confirm `/healthz` |
| `docker compose` plugin absent on this host | Use `docker-compose` (v1 syntax), as the rest of this stack does |
| Ollama reachability depends on Colima port forwarding | Re-verify `host.docker.internal:11434` from inside the container after any Colima restart |
| Embedding dimensions are fixed at index time | Changing away from `nomic-embed-text` (768-dim) later requires re-indexing the whole collection |
| New Postgres volume not covered by backups | `backup.sh` takes the n8n volumes by name; decide explicitly whether vector data is worth backing up or is cheap to re-index |

## Open question

The Groq API key currently lives in another internal project's `.env`. Reusing it here means one
credential shared across two applications, so a rotation or a revoke affects both.
Minting a separate key for n8n keeps the blast radius smaller. Operator decision —
reuse is acceptable, separate is tidier.
