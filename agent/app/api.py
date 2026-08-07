"""FastAPI service. Runs on the compose network; nothing is published publicly.

The inbound direction is the more valuable half of the n8n integration. One-way (agent
fires webhooks at n8n) is a demo; two-way makes the agent a component your existing
production workflows can call.

  POST /ask       any n8n workflow asks a question and routes the cited answer onward
  POST /ingest    an n8n watch/schedule workflow pushes a new or amended regulation
  POST /resume    THE ONE THAT CLOSES THE LOOP — an n8n approval workflow releases a
                  graph paused at the human-approval interrupt

/resume is what makes the checkpointer load-bearing. The Action node calls interrupt();
LangGraph persists state and returns; this process may restart; an n8n workflow puts the
proposed action in front of a human; their decision POSTs back here and execution
continues from the checkpoint. Without persistence that pause could not survive the
round trip, and the approval gate would have to be faked with an in-memory queue.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.config import get_settings
from app.graph.build import build_graph
from app.graph.state import initial_state

_state: dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the graph once, at startup.

    The checkpointer's context manager is entered here and held for the process lifetime.
    Building per-request would give every request its own SQLite connection and, worse,
    a fresh checkpointer — so a thread paused at an interrupt could never be resumed by a
    later request, which is the entire point of /resume.
    """
    cfg = get_settings()
    Path(cfg.checkpoint_db_path).parent.mkdir(parents=True, exist_ok=True)
    cm = SqliteSaver.from_conn_string(cfg.checkpoint_db_path)
    checkpointer = cm.__enter__()
    _state["cm"] = cm
    _state["graph"] = build_graph(verify_enabled=True, checkpointer=checkpointer)
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


app = FastAPI(title="Compliance Agent", version="0.1.0", lifespan=lifespan)


def require_key(authorization: str = Header(default="")) -> None:
    """Bearer auth on every endpoint except /healthz.

    Defence in depth, not the primary control: this service publishes no host port and is
    reachable only as `agent:8000` on the compose network. The token means a compromised
    container elsewhere on that network still cannot drive the agent.
    """
    expected = f"Bearer {get_settings().agent_api_key}"
    # compare_digest to avoid leaking the token's prefix through timing.
    import hmac

    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


class AskRequest(BaseModel):
    question: str
    thread_id: str | None = Field(
        default=None,
        description="Reuse to continue a conversation. Omit for a one-shot question.",
    )
    action: dict | None = Field(
        default=None,
        description="Optional {action, payload} to propose after a GROUNDED answer.",
    )


class ResumeRequest(BaseModel):
    thread_id: str
    approved: bool
    approved_by: str | None = None
    note: str | None = None


class IngestRequest(BaseModel):
    doc_id: str


def _config(thread_id: str) -> dict:
    cfg = get_settings()
    return {
        "configurable": {"thread_id": thread_id[:255]},  # PostgresSaver caps at 255
        "recursion_limit": cfg.recursion_limit,
    }


def _public(out: dict) -> dict:
    """Response shape. Citations are resolved to human-readable locators.

    The API never returns a bare chunk_id: a consumer needs "DIFC Data Protection Law,
    Article 12" and a URL, not an opaque hash. Resolving here rather than in the graph
    keeps chunk_id as the internal join key it is.
    """
    by_id = {c["chunk_id"]: c for c in out.get("retrieved", [])}
    verdict = out.get("verdict")
    return {
        "answer": out.get("answer", ""),
        "citations": [
            {
                "claim": c["claim"],
                "quote": c["quote"],
                "section": by_id.get(c["chunk_id"], {}).get("section", ""),
                "document": by_id.get(c["chunk_id"], {}).get("title", ""),
                "issuer": by_id.get(c["chunk_id"], {}).get("issuer", ""),
                "url": by_id.get(c["chunk_id"], {}).get("url", ""),
            }
            for c in out.get("citations", [])
        ],
        "grounded": bool(verdict and verdict.get("grounded")),
        "attempts": out.get("attempts", 0),
        "queries_tried": out.get("query_history", []),
        "action_result": out.get("action_result"),
        "run_id": out.get("run_id"),
    }


@app.get("/healthz")
def healthz() -> dict:
    """Unauthenticated, for the container healthcheck."""
    return {"status": "ok"}


@app.post("/ask", dependencies=[Depends(require_key)])
def ask(req: AskRequest) -> dict:
    cfg = get_settings()
    thread = req.thread_id or f"ask-{abs(hash(req.question)) % 10**12}"
    state = initial_state(req.question, k=cfg.k_initial)
    if req.action:
        state["action_request"] = req.action

    out = _state["graph"].invoke(state, _config(thread))

    # __interrupt__ means the graph paused at the approval gate. Report it as such —
    # 200 with a pending status, not an error: nothing failed, a human is required.
    if "__interrupt__" in out:
        payload = out["__interrupt__"][0].value
        return {"status": "awaiting_approval", "thread_id": thread, "approval": payload}
    return {"status": "complete", "thread_id": thread, **_public(out)}


@app.post("/ask/stream", dependencies=[Depends(require_key)])
async def ask_stream(req: AskRequest) -> StreamingResponse:
    """SSE, one event per node. Makes the cycle visible in the demo UI.

    Streaming node UPDATES rather than tokens: the interesting thing to watch here is the
    graph's path — "retrieving, analysing, verifying, retrying (attempt 2 of 3)" — which
    is what shows a viewer that the cycle is doing something. Token streaming would show
    prose appearing and hide the structure entirely.
    """
    cfg = get_settings()
    thread = req.thread_id or f"stream-{abs(hash(req.question)) % 10**12}"
    state = initial_state(req.question, k=cfg.k_initial)
    if req.action:
        state["action_request"] = req.action

    async def events():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def run():
            try:
                for chunk in _state["graph"].stream(state, _config(thread), stream_mode="updates"):
                    for node, update in chunk.items():
                        loop.call_soon_threadsafe(queue.put_nowait, {"node": node, "update": update})
                loop.call_soon_threadsafe(queue.put_nowait, None)
            except Exception as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"node": "error", "update": {"error": str(exc)}}
                )
                loop.call_soon_threadsafe(queue.put_nowait, None)

        # The graph is synchronous; run it off the event loop so SSE can flush while it
        # works. Without this the whole response arrives at once and "streaming" is a lie.
        loop.run_in_executor(None, run)

        while True:
            item = await queue.get()
            if item is None:
                yield "event: done\ndata: {}\n\n"
                break
            node, upd = item["node"], item["update"] or {}
            summary = {
                "node": node,
                "attempts": upd.get("attempts"),
                "hits": len(upd.get("retrieved", []) or []),
                "citations": len(upd.get("citations", []) or []),
                "query": upd.get("search_query"),
                "answer": upd.get("answer"),
                "grounded": (upd.get("verdict") or {}).get("grounded"),
            }
            yield f"data: {json.dumps({k: v for k, v in summary.items() if v is not None})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        # Caddy buffers by default; without this an SSE stream arrives in one lump.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/resume", dependencies=[Depends(require_key)])
def resume(req: ResumeRequest) -> dict:
    """Release a graph paused at the approval interrupt.

    Called by an n8n approval workflow. Command(resume=...) delivers the decision as the
    return value of the interrupt() call inside the action node, and execution continues
    from the persisted checkpoint — in a process that need not be the one that paused.
    """
    out = _state["graph"].invoke(
        Command(
            resume={
                "approved": req.approved,
                "approved_by": req.approved_by,
                "note": req.note,
            }
        ),
        _config(req.thread_id),
    )
    return {"status": "resumed", "thread_id": req.thread_id, **_public(out)}


@app.post("/ingest", dependencies=[Depends(require_key)])
def ingest_doc(req: IngestRequest) -> dict:
    """Re-ingest one document. Called by an n8n watch/schedule workflow.

    Makes corpus freshness an automation rather than a remembered manual script — which
    matters for regulations, since an amended source that is never re-indexed produces
    confidently cited, outdated compliance answers.
    """
    from app.retrieval.corpus import by_id
    from app.retrieval.ingest import ingest

    try:
        spec = by_id(req.doc_id)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    n = ingest(spec, Path("./corpus"))
    return {"status": "ingested", "doc_id": spec.doc_id, "chunks": n}


@app.get("/", response_class=HTMLResponse)
def ui() -> str:
    return (Path(__file__).parent / "ui" / "index.html").read_text(encoding="utf-8")
