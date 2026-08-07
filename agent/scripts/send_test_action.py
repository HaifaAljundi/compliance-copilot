#!/usr/bin/env python
"""Fire a correctly-signed action webhook at n8n, for testing workflow 2.

A plain curl cannot do this: the HMAC must be computed over the EXACT bytes sent, and any
re-serialisation (key order, whitespace) changes the digest. This reuses the agent's own
signing code, so what n8n receives is byte-identical to a real delivery.

    # 1. In n8n, click "Listen for test event" on the Webhook node, then:
    python scripts/send_test_action.py --test

    # 2. Once the workflow is ACTIVE, use the production path:
    python scripts/send_test_action.py

    # Negative cases — a receiver that accepts a forgery has no security property:
    python scripts/send_test_action.py --tamper     # wrong signature -> must be rejected
    python scripts/send_test_action.py --replay     # same run_id twice -> must dedupe

Run from the agent/ directory with the venv active, or:
    PGVECTOR_HOST=127.0.0.1 N8N_BASE_URL=http://localhost:5678 .venv/bin/python scripts/...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.tools.n8n import build_payload, sign  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true",
                    help="use /webhook-test/ (needs 'Listen for test event' clicked first)")
    ap.add_argument("--tamper", action="store_true", help="send a WRONG signature")
    ap.add_argument("--replay", action="store_true", help="send the same run_id twice")
    ap.add_argument("--run-id", default="test-run-0001")
    args = ap.parse_args()

    s = get_settings()
    # The test path is a DIFFERENT URL: /webhook-test/ instead of /webhook/. It accepts
    # exactly one request, only while the editor is listening, and runs the workflow in
    # the foreground so you can inspect each node's data.
    base = s.n8n_base_url.rstrip("/")
    path = "/webhook-test/agent-action" if args.test else s.n8n_action_webhook_path
    url = f"{base}{path}"

    envelope = build_payload(
        run_id=args.run_id,
        thread_id="manual-test",
        action="create_compliance_ticket",
        payload={"title": "STR filing deadline review", "severity": "high"},
        evidence=[
            {
                "chunk_id": "abc123",
                "doc_id": "difc-dp-law-5-2020",
                "section": "Article 41",
                "quote": "notify the Commissioner without undue delay",
                "url": "https://example.test/a",
            }
        ],
        approved_by="approver@example.com",
    )

    # sort_keys + no whitespace, matching tools/n8n.py exactly. n8n must verify against
    # these raw bytes, which is why the Webhook node needs rawBody: true.
    body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()

    signature = sign(b"tampered payload" if args.tamper else body, s.n8n_webhook_secret)
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Secret": s.n8n_webhook_secret,
        "X-Agent-Signature": signature,
        "X-Idempotency-Key": envelope["run_id"],
    }

    def post(label: str) -> None:
        print(f"\n{label}\n  POST {url}")
        try:
            r = httpx.post(url, content=body, headers=headers, timeout=120)
        except Exception as exc:
            print(f"  transport error: {type(exc).__name__}: {exc}")
            return
        print(f"  HTTP {r.status_code}")
        print(f"  body: {r.text[:240] or '(empty)'}")
        if r.status_code == 404:
            print("  -> workflow not active, or you did not click 'Listen for test event'")
        elif r.status_code == 403:
            print("  -> Header Auth rejected it: check the 'Agent shared secret' credential")
        elif r.status_code == 200 and not r.text:
            print("  -> ran but the Respond node was not reached; a node threw. "
                  "Check the execution in n8n.")

    if args.tamper:
        post("TAMPERED SIGNATURE — n8n must reject this")
    elif args.replay:
        post("FIRST delivery")
        post("REPLAY of the same run_id — must come back duplicate=true")
    else:
        post("VALID signed request")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
