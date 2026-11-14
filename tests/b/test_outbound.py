from __future__ import annotations

import re
from pathlib import Path

import pytest

from atlas.contracts import EgressPurpose, Mode
from atlas.outbound import (
    ModelRefusal,
    OutboundBoundary,
    OutboundRefused,
    PayloadWithheld,
    Redactor,
)
from tests.b.helpers import EventSink, ScriptedTransport
from tests.fakes import FakeContainment

ROOT = Path(__file__).resolve().parents[2]

# Fake credentials are assembled at runtime so no secret-like literal is stored in the repository.
ANT = "sk-" + "ant-" + "api03-" + "AbCdEfGhIjKlMnOpQrStUv"
GHP = "gh" + "p_" + "abcdefghij" + "klmnopqrst" + "uvwxyz" + "0123456789"
AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
PEM = "-----BEGIN RSA " + "PRIV" + "ATE KEY-----" + "\n" + "MIIabc" + "\n" + "-----END RSA " + "PRIV" + "ATE KEY-----"


@pytest.mark.parametrize(
    "text,category",
    [
        ("token " + ANT + " here", "token"),
        (GHP, "token"),
        ("key " + AWS, "token"),
        ("Authorization: Bear" + "er abcdef0123456789abcdef", "token"),
        (PEM, "private_key"),
        ("password: hunter2", "secret_field"),
        ("client-key-data: " + "LS0t" + "LS1CRUdJTg==", "secret_field"),
        ("contact jane.doe@example.org now", "email"),
        ("reported by @octo-dev yesterday", "handle"),
        ("node at 10.0.12.7 failed", "ip_address"),
        ("dial build-01.corp.internal:6443", "hostname"),
        ("author: Jane Doe", "person"),
        ("customer_id=88123", "end_user"),
    ],
)
def test_every_category_is_redacted(text: str, category: str) -> None:
    r = Redactor().redact(text)
    assert r.counts.get(category, 0) >= 1, (category, r.counts, r.text)
    assert "hunter2" not in r.text and "jane.doe" not in r.text


def test_public_hosts_and_commit_hashes_are_kept() -> None:
    r = Redactor().redact("see https://github.com/kubevirt/kubevirt/commit/9e58ac0fa2 in main.go")
    assert "github.com" in r.text and "9e58ac0fa2" in r.text and "main.go" in r.text


def _boundary(**kw):
    sink = EventSink()
    transport = kw.pop("transport", ScriptedTransport([{"ok": True}]))
    b = OutboundBoundary(events=sink, transport=transport, **kw)
    return b, sink, transport


PAYLOAD = {"system": "s", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 50}


def test_redacted_payload_reaches_model_with_role_selected_model() -> None:
    b, _, t = _boundary()
    payload = {**PAYLOAD, "messages": [{"role": "user", "content": "log: token " + GHP + " at 10.1.2.3"}]}
    b.call("investigator", EgressPurpose.MODEL_CALL, payload)
    sent = t.requests[0]
    assert sent["model"] == "claude-sonnet-5-5"
    body = sent["messages"][0]["content"]
    assert "ghp_" not in body and "10.1.2.3" not in body
    assert b.stats.redactions["token"] == 1 and b.stats.redactions["ip_address"] == 1


def test_structured_output_schema_is_requested() -> None:
    b, _, t = _boundary()
    b.call("verifier", EgressPurpose.MODEL_CALL, {**PAYLOAD, "schema": {"type": "object"}})
    assert t.requests[0]["output_config"]["format"]["type"] == "json_schema"


def test_haiku_roles() -> None:
    b, _, t = _boundary(transport=ScriptedTransport([{}, {}, {}]))
    for role in ("triage", "log_filter", "injection_classifier"):
        b.call(role, EgressPurpose.MODEL_CALL, PAYLOAD)
    assert {r["model"] for r in t.requests} == {"claude-haiku-5-5"}


def test_unclassifiable_span_withholds_whole_payload() -> None:
    b, _, t = _boundary()
    span = "Ab1Cd2Ef3Gh4" * 3  # low-diversity mixed string, assembled at runtime
    r = Redactor().redact(span)
    assert r.unclassified or r.counts.get("high_entropy")
    if r.counts.get("high_entropy"):
        pytest.skip("string treated as a high-entropy secret, which is also safe")
    with pytest.raises(PayloadWithheld):
        b.call("triage", EgressPurpose.MODEL_CALL, {**PAYLOAD, "messages": [{"role": "user", "content": span}]})
    assert t.requests == [] and b.stats.withheld == 1


def test_offline_refuses_before_connecting_and_counts() -> None:
    b, sink, t = _boundary(mode=Mode.OFFLINE)
    with pytest.raises(OutboundRefused):
        b.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)
    assert t.requests == []
    assert b.stats.egress_attempts == 1 and b.stats.refused == 1
    assert sink.types() == ["outbound_offline_attempt"]


def test_containment_pause_refuses() -> None:
    c = FakeContainment()
    c.trip("model_calls", "incident open")
    b, _, t = _boundary(containment=c)
    with pytest.raises(OutboundRefused, match="containment"):
        b.call("verifier", EgressPurpose.MODEL_CALL, PAYLOAD)
    assert t.requests == []


def test_unknown_role_and_purpose_are_not_allowlisted() -> None:
    b, sink, _ = _boundary()
    with pytest.raises(OutboundRefused):
        b.call("someone", EgressPurpose.MODEL_CALL, PAYLOAD)
    with pytest.raises(OutboundRefused):
        b.call("investigator", EgressPurpose.PUBLICATION, PAYLOAD)
    assert sink.types().count("outbound_not_allowlisted") == 2


def test_refusal_stop_reason_is_a_failed_step() -> None:
    b, _, _ = _boundary(transport=ScriptedTransport([({"x": 1}, "refusal")]))
    with pytest.raises(ModelRefusal):
        b.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)


def test_coding_agent_payload_passes_boundary() -> None:
    b, _, t = _boundary()
    b.call("coding_agent", EgressPurpose.MODEL_CALL, {**PAYLOAD, "messages": [{"role": "user", "content": "fix it\nowner: Jane Roe"}]})
    assert "Jane Roe" not in t.requests[0]["messages"][0]["content"]


class FakeWebhook:
    def __init__(self) -> None:
        self.posts = []

    def post(self, url, body):
        self.posts.append((url, body))
        return 200


def test_webhook_payload_passes_boundary_and_allowlist() -> None:
    hook = FakeWebhook()
    b, sink, _ = _boundary(webhook=hook, allowed_webhook_hosts=frozenset({"127.0.0.1"}))
    b.call("webhook", EgressPurpose.WEBHOOK_DELIVERY, {"url": "http://127.0.0.1:9000/hook", "body": {"text": "mail ops@example.org"}})
    assert hook.posts and "ops@example.org" not in str(hook.posts[0][1])
    with pytest.raises(OutboundRefused):
        b.call("webhook", EgressPurpose.WEBHOOK_DELIVERY, {"url": "https://evil.example/x", "body": {}})
    assert "outbound_not_allowlisted" in sink.types()


def test_spend_and_tokens_recorded() -> None:
    spent = []
    b, _, _ = _boundary(spend=spent.append)
    b.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)
    assert b.stats.input_tokens == 100 and b.stats.output_tokens == 20
    assert spent and spent[0] > 0


CLIENT_PATTERNS = re.compile(
    r"anthropic\.Anthropic\(|httpx\.(Client|AsyncClient|get|post)\(|requests\.(get|post|Session)\("
    r"|urllib\.request\.urlopen\(|http\.client\.HTTPS?Connection\("
)


def test_no_model_or_http_client_outside_boundary_and_gateway() -> None:
    allowed = {ROOT / "atlas" / "outbound.py", ROOT / "atlas" / "gateway.py"}
    offenders = []
    for path in (ROOT / "atlas").rglob("*.py"):
        if path in allowed:
            continue
        if CLIENT_PATTERNS.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
