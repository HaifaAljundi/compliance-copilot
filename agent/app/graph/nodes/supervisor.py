"""Supervisor node — routes by FORCING a single tool call.

This is the manual tool-calling supervisor, not `langgraph-supervisor`. Adapted from the
langgraphjs `agent_supervisor` example, which binds one routing tool with `tool_choice`
forced and reads `tool_calls[0].args` into a state field that a conditional edge then
switches on.

Why forcing a tool call beats asking for a word:
  * The model cannot answer with prose, an apology, or a hedge. The API contract only
    permits emitting this tool with these arguments.
  * The enum is validated provider-side, so an invalid route is rejected before it
    reaches the graph rather than falling through a Python `else`.

Deliberately cheap: it never sees corpus text, and `max_tokens` is capped. It is one extra
round-trip on every query, which is a real cost — so it is priced explicitly in the trace
rather than hidden inside the graph's total.

One difference from the reference: its `next` channel uses a reducer that falls back to
the previous value (`(x, y) => y ?? x ?? END`). Here `route` takes the last write with no
fallback, so a supervisor that returns nothing fails loudly instead of silently repeating
its previous decision — which would look like an infinite loop with no obvious cause.
"""

from __future__ import annotations

from time import perf_counter

from pydantic import BaseModel, Field

from app.graph.state import AgentState, Route
from app.llm import invoke_structured, model_for

_SYSTEM = """You route a compliance question to the next step. Choose exactly one:

- "retrieve": the question needs source material from the compliance corpus. This is the \
default for any question of fact about regulations.
- "act": the user is asking to TRIGGER an action (file a report, open a ticket, notify \
someone), not asking a question.
- "answer": the message is ONLY a greeting, a thank-you, or a question about your own \
capabilities. It must contain no question of fact.

Choose "retrieve" for ANY question of fact — including questions that may fall outside \
the corpus. Deciding a question is out of scope is the retrieval step's job, not yours: \
it can check and then say so with evidence, whereas you would only be guessing.

When uncertain, choose "retrieve"."""


class _Route(BaseModel):
    """The routing tool. Its docstring is sent to the model as the tool description."""

    next: Route = Field(description="The next step: retrieve, act, or answer.")


def supervise(state: AgentState) -> dict:
    """Emit a routing decision. Returns only `route`."""
    t0 = perf_counter()

    # with_structured_output on a Pydantic model compiles to a forced tool call — the
    # provider is told this tool MUST be emitted, so there is no free-text path.
    usage: dict = {}
    try:
        decision, usage = invoke_structured(
            "supervisor", _Route, [("system", _SYSTEM), ("human", state["question"])]
        )
        route: Route = decision.next
        failed = False
    except Exception:
        # Fail toward retrieval. Every other fallback is worse: "answer" would respond
        # from model memory with no sources, and "act" would fire a side effect on the
        # strength of an API error.
        route, failed = "retrieve", True

    return {
        "route": route,
        "trace": [
            {
                "node": "supervise",
                "ms": round((perf_counter() - t0) * 1000, 1),
                "model": model_for("supervisor"),
                "route": route,
                "fallback": failed,
                **usage,
            }
        ],
    }
