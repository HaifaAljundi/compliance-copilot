# Compliance Copilot

**An AI assistant for regulatory documents that cites its sources, refuses to guess, and
never acts without a human.**

Built on self-hosted infrastructure. Document text never leaves the building.

---

# Part 1 — The problem

## What compliance teams actually do all day

A compliance officer at a DIFC-registered firm gets a question: *"We had a data breach —
what do we have to report, and how fast?"*

Answering it means:

1. Knowing **which** regulation applies (DIFC Data Protection Law? CBUAE guidance? Both?)
2. Finding the **specific article** — not the general topic, the exact clause
3. Quoting it **accurately**, because a paraphrase is not defensible to a regulator
4. **Acting** on it — opening a ticket, notifying a DPO, starting a filing clock
5. Being able to show **why** they did, months later, when someone asks

Steps 1–3 take 20–40 minutes of searching PDFs. Steps 4–5 get done inconsistently.

## Why a general-purpose chatbot makes this worse

Ask ChatGPT the same question and you get a confident, fluent, plausible answer — with no
way to tell whether it came from the actual regulation, a similar regulation from another
jurisdiction, or the model's imagination.

For most work, an 85%-accurate answer is useful. **For compliance, a confidently wrong
citation is worse than no answer at all**, because it gets acted upon.

The three failure modes that matter:

| Failure | What it looks like | Consequence |
|---|---|---|
| **Hallucinated citation** | "Under Article 47(3)…" — an article that doesn't exist | Filing built on nothing |
| **Right topic, wrong source** | Quotes GDPR when DIFC law applies | Wrong deadline, wrong regulator |
| **Confident answer, no coverage** | Answers a question the documents don't address | Undetectable until audit |

---

# Part 2 — What it does

## Demonstration: two questions, ninety seconds apart

### Question 1 — the documents have the answer

> *"We had a personal data breach affecting client records. What must we include in the
> notification to the Commissioner, and how quickly must we report it?"*

**Response in 7.6 seconds:**

> The Controller must notify the Commissioner **"as soon as practicable in the
> circumstances"**. The notification must at least:
> - describe the nature of the Personal Data Breach, including the categories and
>   approximate number of Data Subjects concerned;
> - provide the name and contact details of the Data Protection Officer;
> - describe the likely consequences of the breach; and
> - describe the measures taken or proposed to address it.
>
> *These requirements are set out in Article 41(4).*

**With 5 citations**, each showing:
- the exact section (`Article 41`)
- the **verbatim quote** it relied on
- a link to the source PDF

Then it **stopped and asked permission** before opening a compliance ticket.

### Question 2 — the documents do not

> *"How many days do we have to notify affected employees under UAE labour law?"*

Plausible. Adjacent. Genuinely not in the indexed documents.

**Response after 17 seconds — it tried three different searches first:**

> I cannot answer this from the available sources.
>
> After 3 retrieval attempts, no passage in the corpus supports a grounded answer.
> Closest material reviewed: Article 1, Article 21, Article 33, Article 53, Section 1.3.

**0 citations. No ticket created. No action taken.**

## The point

Same system, same request format, ninety seconds apart. One produced a cited answer and a
proposed action; the other produced an explicit refusal and did nothing.

**That difference is the product.** Anything can answer question 1. Refusing question 2 —
correctly, every time, without being told which questions are answerable — is the hard part.

---

# Part 3 — How it works

## In one picture

```
  Question
     │
     ▼
  ┌─────────────┐
  │  Supervisor │  Is this a real question, or small talk?
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │  Retriever  │  Searches the regulations by meaning, not keywords
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │   Analyst   │  Writes an answer. Every claim must quote a passage.
  └──────┬──────┘
         ▼
  ┌─────────────┐   not supported →  search differently, try again (up to 3×)
  │  Verifier   │ ──────────────────────────────────────────┐
  └──────┬──────┘                                            │
         │ supported                    still not supported  │
         ▼                                       ▼           │
  ┌─────────────┐                        ┌──────────────┐   │
  │   Action    │  ← human approves      │   Refuse     │ ◀─┘
  └─────────────┘                        └──────────────┘
```

## The four guarantees, and how each is enforced

### 1. Every claim is tied to a real quote

The assistant doesn't just write prose. It produces a **structured claim → source → quote**
record for every factual statement.

Before anyone sees the answer, the system checks — with no AI involved, just text matching:

- Does the cited passage actually exist in what was retrieved?
- Is the quote a **literal substring** of that passage?

Anything failing is discarded silently. This costs nothing, runs in milliseconds, and
catches the two most common hallucination types: invented sources and invented quotes.

> **In testing, this check alone rejected 12–30 fabricated citations per evaluation run.**

### 2. A second, different AI checks the first one's work

A separate model — from a different vendor family — is shown **one claim and one passage
at a time** and asked a single question: *"Does this passage state this claim? Yes or no."*

It is deliberately **not shown the original question**. Given the question, a model reasons
towards the answer seeming reasonable and fills gaps from its own knowledge. Withholding
the question removes the material it would use to do that.

The system refuses to start if the checker and the writer are the same model. A model
grading its own work approves it over 95% of the time — which would make the check
theatre.

### 3. If the evidence isn't there, it searches differently — then gives up

When verification fails, the system doesn't just retry. It asks the checker *what
specifically was missing*, turns that into a **new search query**, and widens the search.

A repeated search is refused outright — each attempt must differ from the last, or the
loop is just paying three times for the same conclusion.

After three attempts it stops and says so, listing what it searched and what it found
closest. **For compliance, "the documents don't cover this" is a correct and useful
answer.**

### 4. Nothing happens without a human

When the assistant concludes an action is warranted — open a ticket, notify a DPO, start a
filing — it does **not** do it.

It freezes mid-execution, writes its state to disk, and sends a human the proposed action
**together with the answer and all the evidence**. Nothing fires until someone clicks
Approve.

Two properties worth noting:

- **The reviewer sees the reasoning, not just a button.** Approving "create ticket" with no
  visible justification is rubber-stamping with extra steps.
- **The pause survives a restart.** In testing, the server was deliberately killed while
  paused; on restart, the approval still completed correctly. This is not an in-memory
  queue that loses work on a bad day.

---

# Part 4 — Where it runs, and what leaves the building

## Everything on your own infrastructure

```
   Your documents  ──▶  Local database (never leaves)
                              ▲
                              │
   Question  ──▶  Assistant ──┘
                     │
                     └──▶  Language model API  ── only the question
                                                  + the few paragraphs retrieved
```

| Component | Where it runs | What it sees |
|---|---|---|
| Document storage & search | **Your server** | Everything |
| Document indexing (embeddings) | **Your server** — local model | Everything |
| Answer writing & checking | External API (Groq) | The question + retrieved excerpts only |
| Workflow automation | **Your server** (n8n) | Everything |
| Approval records | **Your server** | Everything |

**The full corpus is never uploaded anywhere.** Indexing runs on a local model with no
internet access. Only the specific question and the handful of paragraphs relevant to it
reach an external API.

For clients who cannot accept even that, the answer-writing step can run on a local model
too — slower and less capable, but zero egress. That is a configuration change, not a
rebuild.

## Integration with existing automation

The assistant runs alongside n8n, an automation platform, and talks to it **in both
directions**:

**Outbound** — the assistant triggers workflows: create a ticket, send a notification,
start a filing. Every request is cryptographically signed and carries a unique key so a
network retry can never produce two tickets.

**Inbound** — existing workflows can call the assistant:
- A scheduled job detects an amended regulation and pushes it in for re-indexing
- A ticket-triage workflow asks a question mid-process and routes the cited answer onward
- The approval process itself: email, Slack, or a web form — whatever the client already uses

---

# Part 5 — Evidence

Everything below was measured on the running system, not estimated.

## Retrieval accuracy

| Metric | Result |
|---|---|
| Correct source in top 5 results | **98%** |
| Correct source in top 10 results | **100%** |
| Test questions | 52 answerable + 15 unanswerable |

The test questions were generated from the **body text** of the regulations, not from
section headings — a question written from a heading contains the words that find it, which
flatters the result. Questions naming their own article number were rejected outright.

## Response time and cost

| | Typical | Slowest 5% |
|---|---|---|
| Answer with citations | ~9 seconds | ~32 seconds |
| Cost per question | ~$0.003 | — |

The slow tail is questions the system had to search for repeatedly before refusing.
**Refusing correctly costs more than answering easily** — that is the trade being made.

## What the verification step actually buys

A controlled comparison — same questions, same models, same settings, only the verifier
switched off:

| | Without verifier | With verifier |
|---|---|---|
| Answers fully supported by sources | 95% | 98% |
| Correctly refused unanswerable questions | 100% | 100% |
| Fabricated citations caught | 12 | 30 |
| Response time (slowest 5%) | 15s | 32s |
| Cost per question | $0.0010 | $0.0028 |

**Honest reading:** on this corpus, the verification loop improves groundedness by about 2
points for roughly 2.7× the cost — a difference small enough to sit within run-to-run
variation. The measurement is reported as found rather than tuned to look better.

What is clearly doing the work is the **free, deterministic citation check**, which runs in
both configurations and rejects fabricated sources at no cost.

The useful conclusion: *structured citations are what make groundedness checkable at all.
The AI-based checking is the marginal addition.*

---

# Part 6 — Honest limitations

Stated plainly, because a compliance product that oversells itself is the wrong product.

**The corpus is small.** Three documents: the DIFC Data Protection Law and two CBUAE
AML/CFT guidance notes. Adding documents is routine — but retrieval accuracy on a
50-document corpus has not been measured and should not be assumed identical.

**It is an assistant, not an authority.** It finds and quotes what the documents say. It
does not interpret ambiguity, weigh conflicting provisions, or replace professional
judgment. Every answer is designed to be checked against the source — that is why the
quotes and links are there.

**Coverage is exactly the documents indexed.** It cannot answer about a regulation nobody
loaded. It will say so rather than guess, but "it refused" and "no such rule exists" are
different statements.

**The evaluation is a single run.** No variance bars. Directionally sound, not
publication-grade.

**Amended regulations must be re-indexed.** Automatable on a schedule via n8n, but if a
document changes and nobody re-indexes it, the assistant will confidently cite the old
version. This is the highest-risk operational failure mode and deserves an explicit
process.

**One demo shortcut:** the built-in web interface passes its access key in the URL. Fine on
a private machine, unsuitable for shared deployment — a real rollout needs proper login.

---

# Part 7 — What a deployment looks like

## Infrastructure

Runs on a single server. Currently: four containers on one Mac, alongside an
existing n8n instance, using roughly 4 GB of RAM.

No cloud account required. No per-seat licence. No data-processing agreement with a
document-storage vendor, because there isn't one.

## Adding a client's documents

1. Point the ingester at the PDFs or URLs
2. It splits them **by article and section** — never mid-clause, so every quote has an
   honest location
3. Indexing runs locally, roughly 10 seconds per document
4. Build a set of test questions with known answers, and **measure retrieval before going
   live** — if the search can't find the right passage, no amount of AI tuning fixes it

## Realistic effort

| Phase | Effort |
|---|---|
| Add a new document set | Hours |
| Build a test set and measure accuracy | 1–2 days (the part that can't be rushed) |
| Wire client-specific actions in n8n | Days, depending on the target systems |
| Proper authentication and multi-user access | Not yet built |

---

# Part 8 — Sensible next steps

**Prove it on the client's actual documents.** Retrieval accuracy is corpus-specific.
Everything else is engineering that already works.

**Expand the test set.** Especially questions that *should* be refused — that is where the
value is demonstrated and where problems hide.

**Route only uncertain answers through full verification.** Currently every answer pays the
verification cost for a small average gain. Spending it only where confidence is low would
cut cost substantially with little loss.

**Automate corpus freshness.** A scheduled job that watches regulator publication pages and
re-indexes on change closes the highest-risk operational gap.

---

# Appendix — For technical audiences

**Stack:** Python · LangGraph · LangChain · FastAPI · PostgreSQL with pgvector · Ollama
(local embeddings, 768-dim) · Groq (inference) · n8n · Docker

**Architecture:** A supervisor/worker state graph with a genuine cycle. The supervisor
routes by forced tool call rather than free-text parsing. The verifier's rejection feeds a
query rewrite, so each retry searches differently — enforced deterministically, not by
trusting the model to vary.

**Why a graph and not a chain:** three features require it — durable pauses for human
approval, streamed per-node progress, and the ability to compile the same node functions
with and without the verifier for controlled comparison.

**Durability:** graph state is checkpointed after every step. A paused approval survives
process death; verified by killing the container mid-pause and resuming afterwards.

**Idempotency:** every run carries a UUID sent with every webhook attempt, including
retries. The receiving workflow deduplicates on it. Topology prevents the cycle from
double-firing; the key protects against process restarts and lost responses — both are
needed.

**Testing:** 61 automated tests. 55 run with no network and no database, covering routing
logic, state semantics, and webhook retry behaviour — deliberately the parts whose failures
are silent rather than loud.

**Interoperability:** the vector store is shared between the Python agent and n8n's own
database nodes. Reconciling that required resolving four separate contract mismatches
between the two LangChain implementations — column naming, JSON column type, embedding
width, and primary-key generation. All four now have regression tests.
