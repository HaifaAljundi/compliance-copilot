"""n8n webhook client — the agent's outbound side effect.

Runs on the compose network: the agent POSTs to http://n8n:5678/webhook/... directly. It
does NOT go out through Caddy on the jump host and back in over the public internet to
reach a service one bridge away. Consequences: no TLS handshake, no public exposure, no
pf rule, and the public /webhook* path stays reserved for genuine third parties.

Three properties this file exists to guarantee:

  IDEMPOTENCY  `run_id` is minted once per graph run and reused across every retry. n8n
               dedupes on it. This is what makes retrying safe, and it is also the only
               protection against a double-fire caused by a process restart — which
               graph topology alone cannot prevent.

  AUTHENTICITY Header Auth proves the caller knows a shared secret. The HMAC-SHA256 over
               the RAW body binds that secret to the exact bytes sent, so n8n rejects a
               body that does not match the digest it was given.

               Be precise about what this does NOT buy: the signature is keyed on the
               SAME secret the header carries in cleartext (config.n8n_webhook_secret),
               so it is a second gate on one credential, not independent proof of
               integrity. Anyone who can read a request can re-sign an altered one. To
               make the integrity claim real, give the signature its own key — a separate
               setting, or HKDF from a shared root with distinct info strings — and n8n a
               credential that holds only that key.

  RESTRAINT    Retries happen ONLY on connect errors, timeouts and 5xx. Never on 4xx: a
               401 is a wrong secret and a 422 is a malformed payload, and repeating
               either just repeats the mistake three times.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import get_settings

SCHEMA_VERSION = "1"


class WebhookRetryable(Exception):
    """Connect error, timeout, or 5xx — the classes where a retry can plausibly help."""


class WebhookRejected(Exception):
    """4xx. Terminal by construction: retrying cannot fix a wrong secret or a bad body."""


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 over the exact bytes sent.

    Signing the serialised bytes rather than the dict matters: any re-serialisation on
    either side (key order, whitespace, unicode escaping) changes the digest. n8n must
    verify against the raw request body for the same reason.
    """
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_payload(
    *,
    run_id: str,
    thread_id: str,
    action: str,
    payload: dict[str, Any],
    evidence: list[dict[str, Any]],
    approved_by: str | None = None,
) -> dict[str, Any]:
    """The versioned envelope.

    `evidence` travels with the request deliberately. A downstream workflow that files
    something should be able to show WHY without calling back — and in a compliance
    context, an action without its justification attached is the thing an auditor asks
    about.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,          # idempotency key — n8n dedupes on this
        "thread_id": thread_id,
        "action": action,
        "issued_at": datetime.now(UTC).isoformat(),
        "approved_by": approved_by,
        "payload": payload,
        "evidence": evidence,
    }


@retry(
    retry=retry_if_exception_type(WebhookRetryable),
    stop=stop_after_attempt(3),
    # Jitter, not plain exponential: without it, concurrent runs that fail together
    # retry together and hammer a recovering n8n in lockstep.
    wait=wait_exponential_jitter(initial=0.5, max=8),
    reraise=True,
)
def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, content=body, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as exc:
        raise WebhookRetryable(str(exc)) from exc

    if resp.status_code >= 500:
        raise WebhookRetryable(f"{resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise WebhookRejected(f"{resp.status_code}: {resp.text[:200]}")

    try:
        return resp.json()
    except Exception:
        # n8n's "respond immediately" mode can return an empty body. Not an error.
        return {"raw": resp.text[:500], "status_code": resp.status_code}


def send_action(envelope: dict[str, Any], *, timeout: float = 15.0) -> dict[str, Any]:
    """POST an action to n8n. Returns a status dict; never raises to the caller.

    Failure is REPORTED, not raised, because the graph must be able to tell the user
    "the action was not delivered". A raised exception at this point would abandon a
    perfectly good, fully-cited answer over a webhook problem.

    Note the returned status is "accepted", not "completed". n8n's Webhook node should be
    set to respond immediately with 202 and let the workflow run asynchronously — a long
    compliance workflow must not hold an HTTP connection open, and the agent must never
    conflate accepted with done.
    """
    s = get_settings()
    body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        # Matches n8n's Webhook node "Authentication -> Header Auth".
        "X-Agent-Secret": s.n8n_webhook_secret,
        "X-Agent-Signature": sign(body, s.n8n_webhook_secret),
        # Surfaced as a header too, so an n8n Code node can dedupe without parsing JSON.
        "X-Idempotency-Key": envelope["run_id"],
    }

    try:
        result = _post(s.n8n_action_url, body, headers, timeout)
        return {"status": "accepted", "run_id": envelope["run_id"], "response": result}
    except WebhookRejected as exc:
        return {"status": "rejected", "run_id": envelope["run_id"], "error": str(exc),
                "retried": False}
    except WebhookRetryable as exc:
        return {"status": "failed", "run_id": envelope["run_id"], "error": str(exc),
                "retried": True}
