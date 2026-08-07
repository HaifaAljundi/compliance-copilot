"""Action node — human approval, then the n8n webhook.

Two independent protections against firing an n8n workflow twice. Both are needed.

  TOPOLOGY   This node is reachable ONLY from a passing verifier verdict (see
             edges.route_after_verify). The analyst can never reach it directly, so a
             loop-back cannot re-run it.

  IDEMPOTENCY `run_id` is minted once per run and sent with every attempt, including
             retries. Topology cannot protect against a process restart mid-run, a
             replayed checkpoint, or an HTTP response lost after n8n already committed
             the work. This can.

The `interrupt()` call is what makes the checkpointer load-bearing rather than
decorative. The graph pauses HERE, before any side effect; the Python process may exit;
an n8n workflow puts the proposed action in front of a human; and a later POST to
/resume continues execution from the persisted checkpoint. Firing a real workflow on an
LLM's unreviewed decision is the first thing a reviewer attacks, and correctly so.
"""

from __future__ import annotations

from time import perf_counter

from langgraph.types import interrupt

from app.graph.state import AgentState
from app.tools.n8n import build_payload, send_action


def _evidence(state: AgentState) -> list[dict]:
    """The citations that justified this action, flattened for the webhook envelope.

    Only VALIDATED citations reach here — analyst.py already discarded fabricated chunk
    ids and non-verbatim quotes. So an action's evidence cannot cite a passage that does
    not exist, which is the property that makes this auditable.
    """
    by_id = {c["chunk_id"]: c for c in state.get("retrieved", [])}
    out = []
    for cit in state.get("citations", []):
        chunk = by_id.get(cit["chunk_id"])
        if not chunk:
            continue
        out.append(
            {
                "chunk_id": cit["chunk_id"],
                "doc_id": chunk["doc_id"],
                "section": chunk["section"],
                "issuer": chunk["issuer"],
                "quote": cit["quote"],
                "url": chunk["url"],
            }
        )
    return out


def act(state: AgentState) -> dict:
    """Pause for approval, then POST to n8n."""
    t0 = perf_counter()
    request = state.get("action_request")

    if not request:
        # Reachable only via a wiring mistake. Do nothing rather than invent an action —
        # this node's failure mode is a real-world side effect.
        return {
            "action_result": {"status": "skipped", "reason": "no action_request in state"},
            "trace": [{"node": "act", "ms": 0.0, "skipped": True}],
        }

    # Execution stops here. LangGraph persists state and raises out to the caller with
    # this payload. Nothing below runs until a human decision arrives via /resume, and
    # crucially, nothing above it has produced a side effect yet.
    decision = interrupt(
        {
            "kind": "approval_required",
            "run_id": state["run_id"],
            "action": request.get("action"),
            "payload": request.get("payload", {}),
            # The reviewer sees the answer and its evidence, not just the action name.
            # Approving "create_compliance_ticket" with no visible justification is
            # rubber-stamping with extra steps.
            "answer": state.get("answer", ""),
            "evidence": _evidence(state),
        }
    )

    approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
    approver = decision.get("approved_by") if isinstance(decision, dict) else None
    note = decision.get("note") if isinstance(decision, dict) else None

    if not approved:
        # A rejection is recorded, not silently dropped: "a human declined this" is an
        # audit fact, and the difference between declined and never-proposed matters.
        return {
            "action_result": {"status": "rejected_by_human", "run_id": state["run_id"],
                              "approved_by": approver, "note": note},
            "trace": [{"node": "act", "ms": round((perf_counter() - t0) * 1000, 1),
                       "approved": False}],
        }

    envelope = build_payload(
        run_id=state["run_id"],          # same id across retries — n8n dedupes on it
        thread_id=state.get("run_id", ""),
        action=request["action"],
        payload=request.get("payload", {}),
        evidence=_evidence(state),
        approved_by=approver,
    )
    result = send_action(envelope)

    return {
        "action_result": result,
        "trace": [
            {
                "node": "act",
                "ms": round((perf_counter() - t0) * 1000, 1),
                "approved": True,
                # "accepted" means n8n took the request, NOT that the workflow finished.
                "delivery": result.get("status"),
            }
        ],
    }
