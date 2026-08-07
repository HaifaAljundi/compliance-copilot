# n8n workflows

Three paste-ready workflows. Node types and `typeVersion`s were read out of your running
n8n 2.31.6 container, not guessed.

**To import:** open n8n → new workflow → click the canvas → `Cmd+V` and paste the file's
contents. Then fix the credential references (they are placeholders).

---

## Prerequisites — two credentials

Create these once, in n8n → Settings → Credentials. Neither can be scripted: they hold
secrets and n8n encrypts them with `N8N_ENCRYPTION_KEY`.

### 1. Postgres → name it `pgvector (rag)`

| Field | Value |
|---|---|
| Host | `pgvector` |
| Port | `5432` |
| Database | `rag` |
| User | `rag` |
| Password | `PGVECTOR_PASSWORD` from `~/Sites/n8n/.env` |
| SSL | disable |

`pgvector`, not `localhost` — n8n reaches it by container name on the compose network.

### 2. Ollama → name it `Ollama (host)`

| Field | Value |
|---|---|
| Base URL | `http://host.docker.internal:11434` |

Ollama runs on the Mac, not in a container. Colima forwards this even though Ollama binds
`127.0.0.1` on the host. Re-verify after any `colima restart`.

### 3. Header Auth × 2 (for workflows 2 and 3)

- **`Agent shared secret`** — name `X-Agent-Secret`, value = `N8N_WEBHOOK_SECRET` from `agent/.env`
- **`Agent API key`** — name `Authorization`, value = `Bearer <AGENT_API_KEY>` from `agent/.env`

---

## 1 · `1-corpus-write-test.json` — proves the interop contract

**Why it exists.** The corpus is written and read by two different LangChain
implementations, in two languages, whose column-name defaults disagree on three of four
columns. Python→n8n reads are already proven. This proves the other direction.

**The whole point is what you DON'T configure.** Change only the table name to
`compliance_chunks`. Leave the *Column Names* section completely untouched — `store.py`
deliberately created the table to match n8n's defaults, so the side a human configures by
hand has nothing to get wrong.

Run it, then verify from Python that the row n8n wrote is readable and correctly embedded:

```bash
cd ~/Sites/n8n/agent && PGVECTOR_HOST=127.0.0.1 PGVECTOR_PORT=5433 \
  .venv/bin/python -c "
from sqlalchemy import text
from app.retrieval.store import get_sync_engine
with get_sync_engine().connect() as c:
    for r in c.execute(text(\"select metadata->>'doc_id', left(text,60), vector_dims(embedding) from compliance_chunks where metadata->>'doc_id'='n8n-interop-probe'\")):
        print(r)"
```

**Expect `768`.** A different number means the Ollama credential is pointing at the wrong
model — the column is `vector(768)` and every insert would fail.

Clean up afterwards:

```sql
delete from compliance_chunks where metadata->>'doc_id' = 'n8n-interop-probe';
```

---

## 2 · `2-agent-action-receiver.json` — receives the agent's webhook

Path: `/webhook/agent-action`, matching `N8N_ACTION_WEBHOOK_PATH` in `app/config.py`.

Three things it does before any work happens:

1. **Header Auth** — proves the caller knows the shared secret.
2. **HMAC-SHA256 over the raw body** — proves the body was not altered. The header alone
   cannot tell you that. `rawBody: true` on the Webhook node is **required**: the agent
   signs the exact bytes it sent, and n8n re-serialising parsed JSON would change key order
   and break every signature.
3. **Dedupe on `run_id`** — the agent reuses one id across all retries, so a response lost
   *after* this workflow committed work arrives again with the same key. Without the check
   that becomes a second ticket.

Responds **202**, not 200 — the agent records "accepted", never "completed". A long
compliance workflow must not hold the HTTP connection open.

Replace the `DO THE WORK (replace me)` node with your real downstream action. The
`evidence` array carries the citations that justified it, so a filing can show *why*
without calling back.

**Set the secret** in n8n → Settings → Variables as `AGENT_WEBHOOK_SECRET`, or paste it
into the Code node directly.

---

## 3 · `3-ask-and-approve.json` — closes the human-in-the-loop cycle

This is the one that makes the integration a system rather than a demo.

```
n8n ──POST /ask (with a proposed action)──▶ agent
                                             │ retrieves, answers, verifies
                                             │ reaches the action node
                                             │ interrupt() — PAUSES, persists state
n8n ◀────── 200 {status: "awaiting_approval"} ┘
 │
 ├─ shows a human the ANSWER + EVIDENCE, waits
 │
 └──POST /resume {thread_id, approved}──────▶ agent
                                              │ resumes FROM THE CHECKPOINT
                                              └─▶ fires /webhook/agent-action → workflow 2
```

Notes:

- **`thread_id` must be stable** between the two calls — it is what `/resume` addresses.
  The workflow uses `'n8n-' + $execution.id`.
- **The pause survives an agent restart.** Verified by killing the agent container
  mid-pause and resuming afterwards; state lives in the checkpointer, not in memory.
- **The reviewer sees the answer and its evidence**, not just an action name. Approving
  `create_compliance_ticket` with no visible justification is rubber-stamping with extra
  steps.
- **A rejection is recorded, not dropped** — "a human declined this" is an audit fact, and
  distinct from "never proposed".
- The approval node is Gmail `sendAndWait` as an example. Swap it for Slack, or an n8n
  Form, or whatever channel you actually use — nothing else changes.

---

## Order to do this in

1. Create the two credentials (Postgres, Ollama).
2. Import **workflow 1**, run it, verify `768` from Python. → interop proven, all four
   quadrants closed.
3. Create the two Header Auth credentials.
4. Import **workflow 2**, activate it. → the agent's outbound webhook now lands somewhere.
5. Import **workflow 3**, run it. → full loop: ask → pause → approve → resume → webhook
   fires → workflow 2 receives it.

After step 5 you will see one execution in workflow 3 and one in workflow 2, linked by the
same `run_id`.
