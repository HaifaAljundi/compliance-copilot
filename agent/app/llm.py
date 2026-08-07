"""Chat model factories — the only place in this codebase where a model is named.

Nodes ask for a ROLE ("verifier"), never a model id. Three consequences that matter:

  * Swapping the verifier to a different model is one env var, no code change.
  * The analyst/verifier separation is enforced structurally. config.py refuses to start
    if they are equal, and no node can quietly reach around that by constructing its own
    client.
  * Per-node cost in the eval is attributable, because the role is recorded in the trace
    next to the token counts.

Embeddings deliberately live in retrieval/store.py, not here: they are bound to the
corpus (768 dims, fixed at index time), whereas these are swappable per run.
"""

from functools import lru_cache
from typing import Literal

from langchain_groq import ChatGroq

from app.config import get_settings

Role = Literal["supervisor", "analyst", "verifier"]


@lru_cache
def get_chat(role: Role) -> ChatGroq:
    """Chat model for a role. Cached: one client per role per process."""
    s = get_settings()
    model = {
        # Emits a single routing token and never sees corpus text, so it has no business
        # running the largest model. Kept configurable so the eval can price it honestly.
        "supervisor": s.supervisor_model,
        "analyst": s.analyst_model,
        "verifier": s.verifier_model,
    }[role]

    return ChatGroq(
        model=model,
        api_key=s.groq_api_key,
        # temperature=0 throughout. Not a quality choice — a reproducibility one. The A/B
        # compares two graphs over the same gold set, and sampling noise between arms
        # would be indistinguishable from the effect being measured.
        temperature=s.temperature,
        max_retries=2,
        # Every model in use here is a reasoning model, and by default Groq returns the
        # chain of thought INLINE in `content`, wrapped in <think>...</think> — not in a
        # separate field. Verified: without this, a request for "output only the query"
        # returns content beginning "\n<think>\nHere's a thinking process:...".
        #
        # That is a silent corruption, not a crash. Naive parsing of the first line
        # yields the literal string "<think>", which then gets embedded and searched,
        # returning noise while every downstream signal reports success.
        #
        # "hidden" makes Groq strip it server-side. Confirmed accepted by all three
        # models currently configured. Nothing in this graph wants visible reasoning —
        # every call site needs either a clean string or a structured tool call.
        reasoning_format="hidden",
    )


def model_for(role: Role) -> str:
    """The model id behind a role, for trace entries. Avoids reaching into the client."""
    s = get_settings()
    return {
        "supervisor": s.supervisor_model,
        "analyst": s.analyst_model,
        "verifier": s.verifier_model,
    }[role]


def invoke_structured(role: Role, schema, messages):
    """Structured output that ALSO returns token usage. Returns (parsed, usage).

    `with_structured_output(schema)` returns only the parsed Pydantic object — the
    underlying AIMessage, and with it `usage_metadata`, is discarded. That silently
    zeroes every cost figure in the eval, which is worse than having no cost figure at
    all: a zero looks like a measurement.

    `include_raw=True` returns {"raw", "parsed", "parsing_error"} so both survive.

    Usage is returned rather than logged so the CALLING NODE records it in its own trace
    entry, keeping cost attributable per node instead of aggregated into one number.
    """
    # Retried once, because forced tool calling is FLAKY, not deterministic.
    #
    # Measured over a 96-run A/B: 7 runs (7%) died on either
    #   BadRequestError: Tool choice is required, but model did not call a tool
    # or a None `parsed` — the model answered in prose despite tool_choice being forced.
    # It is a sampling outcome, so the same call usually succeeds on a second attempt.
    #
    # This matters beyond reliability: the crashes were ASYMMETRIC between the two A/B
    # arms (4 vs 2), which silently biased every rate the eval reported. An unreliable
    # call in the measurement path corrupts the measurement, not just the run.
    #
    # The retry appends an explicit instruction rather than resending verbatim — a bare
    # resend re-rolls the same dice, whereas naming the required behaviour shifts it.
    bound = get_chat(role).with_structured_output(schema, include_raw=True)
    last_error: Exception | None = None

    for attempt in range(2):
        payload = messages
        if attempt == 1:
            nudge = "Respond ONLY by calling the provided tool with its required fields."
            payload = (
                messages + [("human", nudge)]
                if isinstance(messages, list)
                else f"{messages}\n\n{nudge}"
            )
        try:
            result = bound.invoke(payload)
        except Exception as exc:  # BadRequestError: tool_use_failed, and transport errors
            last_error = exc
            continue
        parsed = result.get("parsed")
        if parsed is not None:
            break
        last_error = ValueError(str(result.get("parsing_error")))
    else:
        raise ValueError(f"structured output failed for {role} after 2 attempts: {last_error}")

    if parsed is None:
        raise ValueError(f"structured output failed for {role} after 2 attempts: {last_error}")

    usage = getattr(result.get("raw"), "usage_metadata", None) or {}
    return parsed, {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
    }


def usage_of(message) -> dict:
    """Extract token usage from a plain (non-structured) AIMessage."""
    u = getattr(message, "usage_metadata", None) or {}
    return {"prompt_tokens": u.get("input_tokens", 0),
            "completion_tokens": u.get("output_tokens", 0)}
