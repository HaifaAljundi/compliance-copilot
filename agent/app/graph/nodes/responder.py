"""Responder node — the conversational path.

Exists because routing "answer" straight to END produced a run with an EMPTY answer:
no node on that path wrote anything. The graph reported success, the eval recorded a
refusal (zero citations), and nothing raised.

This node is deliberately incapable of answering regulatory questions. It has no
retrieved chunks and is instructed to redirect anything factual back through retrieval,
so a supervisor misroute degrades into a visible non-answer rather than an uncited
assertion from model memory.
"""

from __future__ import annotations

from time import perf_counter

from app.graph.state import AgentState
from app.llm import get_chat, model_for, usage_of

_SYSTEM = """You are the conversational front of a compliance document assistant.

You have NO access to source documents in this turn. Therefore:
- Greetings, thanks, and questions about your own capabilities: answer briefly.
- ANY question of fact about regulations, law, or compliance: do NOT answer it. Say that \
you need to search the source documents, and ask them to re-ask it directly.

Never state a regulatory fact from memory. You have no citation to support it."""


def respond(state: AgentState) -> dict:
    t0 = perf_counter()
    try:
        msg = get_chat("supervisor").invoke(
            [("system", _SYSTEM), ("human", state["question"])]
        )
        text, usage = (msg.content or "").strip(), usage_of(msg)
    except Exception:
        text, usage = (
            "I can answer questions about the compliance documents I have indexed. "
            "Could you rephrase that as a question about them?",
            {},
        )

    return {
        "answer": text,
        "citations": [],
        "trace": [{"node": "respond", "ms": round((perf_counter() - t0) * 1000, 1),
                   "model": model_for("supervisor"), **usage}],
    }
