"""n8n webhook client. Offline — httpx is mocked at the transport layer via respx.

The properties tested here are the ones whose absence causes real-world damage rather
than a failed test: a retry that duplicates a filing, a 4xx retried three times against a
wrong secret, or a delivery failure silently reported as success.
"""

import hashlib
import hmac
import json

import httpx
import pytest
import respx

from app.tools.n8n import (
    _post,
    build_payload,
    send_action,
    sign,
)

URL = "http://n8n:5678/webhook/agent-action"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Strip the backoff wait so the retry tests do not sleep for seconds."""
    _post.retry.wait = lambda *a, **k: 0


def _envelope(run_id="run-abc"):
    return build_payload(
        run_id=run_id,
        thread_id="t-1",
        action="create_compliance_ticket",
        payload={"severity": "medium"},
        evidence=[{"chunk_id": "c1", "section": "Article 12", "quote": "..."}],
        approved_by="someone@example.test",
    )


class TestEnvelope:
    def test_carries_run_id_and_schema_version(self):
        e = _envelope()
        assert e["run_id"] == "run-abc"
        assert e["schema_version"] == "1"

    def test_evidence_travels_with_the_request(self):
        """A workflow that files something must be able to show why without calling back."""
        assert _envelope()["evidence"][0]["section"] == "Article 12"


class TestSignature:
    def test_signature_matches_hmac_of_the_exact_bytes(self):
        body = b'{"a":1}'
        assert sign(body, "s3cret") == hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

    def test_reserialisation_changes_the_signature(self):
        """Why n8n must verify against the RAW body, not a re-encoded dict.

        Same object, different key order — different bytes, different digest. A verifier
        that JSON-parses then re-serialises before hashing will reject valid requests.
        """
        secret = "s3cret"
        a = json.dumps({"a": 1, "b": 2}, separators=(",", ":")).encode()
        b = json.dumps({"b": 2, "a": 1}, separators=(",", ":")).encode()
        assert sign(a, secret) != sign(b, secret)


class TestRetryPolicy:
    @respx.mock
    def test_retries_5xx_then_succeeds(self):
        route = respx.post(URL).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(500),
                httpx.Response(202, json={"executionId": "e-9"}),
            ]
        )
        out = send_action(_envelope())
        assert out["status"] == "accepted"
        assert route.call_count == 3

    @respx.mock
    def test_never_retries_4xx(self):
        """A 401 is a wrong secret and a 422 is a bad payload. Repeating either three
        times just repeats the mistake and triples the log noise."""
        route = respx.post(URL).mock(return_value=httpx.Response(401, text="unauthorized"))
        out = send_action(_envelope())
        assert out["status"] == "rejected"
        assert route.call_count == 1

    @respx.mock
    def test_retries_connect_errors(self):
        route = respx.post(URL).mock(
            side_effect=[httpx.ConnectError("refused"), httpx.Response(202, json={})]
        )
        assert send_action(_envelope())["status"] == "accepted"
        assert route.call_count == 2

    @respx.mock
    def test_gives_up_after_three_attempts_and_reports_failure(self):
        """Reported, never raised: a webhook problem must not discard a good cited answer,
        and must never be mistaken for success."""
        route = respx.post(URL).mock(return_value=httpx.Response(502))
        out = send_action(_envelope())
        assert out["status"] == "failed"
        assert route.call_count == 3


class TestIdempotency:
    @respx.mock
    def test_run_id_is_identical_across_every_retry(self):
        """THE property that makes retrying safe.

        Without it, a response lost after n8n already committed the work becomes a second
        ticket, a second filing, a second notification.
        """
        seen = []

        def capture(request):
            seen.append(json.loads(request.content)["run_id"])
            return httpx.Response(500 if len(seen) < 3 else 202, json={})

        respx.post(URL).mock(side_effect=capture)
        send_action(_envelope("run-fixed"))
        assert seen == ["run-fixed"] * 3

    @respx.mock
    def test_idempotency_key_is_also_a_header(self):
        """So an n8n Code node can dedupe without parsing the body."""
        captured = {}

        def capture(request):
            captured.update(request.headers)
            return httpx.Response(202, json={})

        respx.post(URL).mock(side_effect=capture)
        send_action(_envelope("run-xyz"))
        assert captured["x-idempotency-key"] == "run-xyz"
        assert "x-agent-signature" in captured
        assert "x-agent-secret" in captured


class TestAcceptedIsNotCompleted:
    @respx.mock
    def test_status_says_accepted_not_completed(self):
        """n8n responds immediately with 202 and runs the workflow asynchronously. The
        agent must never conflate 'n8n took the request' with 'the work is done'."""
        respx.post(URL).mock(return_value=httpx.Response(202, json={"executionId": "e-1"}))
        assert send_action(_envelope())["status"] == "accepted"

    @respx.mock
    def test_empty_body_is_not_an_error(self):
        """'Respond immediately' mode can return no body at all."""
        respx.post(URL).mock(return_value=httpx.Response(200, text=""))
        assert send_action(_envelope())["status"] == "accepted"
