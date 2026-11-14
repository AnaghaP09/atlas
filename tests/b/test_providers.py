"""Model provider interface: Anthropic (existing behaviour) and a self-hosted OpenAI-compatible endpoint.

The self-hosted provider is exercised only against a local fake HTTP server on 127.0.0.1.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from atlas.contracts import EgressPurpose, Mode
from atlas.outbound import (
    AnthropicProvider,
    ModelRefusal,
    ModelStepFailed,
    OpenAICompatibleProvider,
    OutboundBoundary,
    OutboundRefused,
    provider_from_env,
)
from tests.b.helpers import EventSink, ScriptedTransport
from tests.fakes import FakeContainment

PAYLOAD = {"system": "rules", "messages": [{"role": "user", "content": "mail ops@example.org about 10.1.2.3"}],
           "max_tokens": 64, "schema": {"type": "object"}}


class FakeServer:
    """A minimal OpenAI-compatible chat endpoint that records requests."""

    def __init__(self, reply=None, finish="stop", status=200):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                data = json.dumps({"choices": [{"message": {"content": json.dumps(reply if reply is not None else {"ok": True})},
                                                "finish_reason": finish}],
                                   "usage": {"prompt_tokens": 11, "completion_tokens": 3}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


MODELS = {r: ("big-model" if r in ("investigator", "verifier", "coding_agent") else "small-model")
          for r in ("investigator", "verifier", "coding_agent", "triage", "log_filter", "injection_classifier")}


def selfhosted(port, internal=True):
    return OpenAICompatibleProvider(f"http://127.0.0.1:{port}", MODELS, api_key="local-key",
                                    internal_hosts=frozenset({"127.0.0.1"}) if internal else frozenset())


def test_anthropic_provider_keeps_existing_request_shape_and_maps_stops() -> None:
    t = ScriptedTransport([{"a": 1}, ({"a": 1}, "max_tokens"), ({"a": 1}, "pause_turn"), ({"a": 1}, "refusal")])
    b = OutboundBoundary(provider=AnthropicProvider(t))
    r = b.call("verifier", EgressPurpose.MODEL_CALL, PAYLOAD)
    req = t.requests[0]
    assert r.stop_reason == "complete" and req["model"] == "claude-sonnet-5-5" and req["system"] == "rules"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert all(m["role"] != "system" for m in req["messages"])
    with pytest.raises(ModelStepFailed, match="truncated"):
        b.call("verifier", EgressPurpose.MODEL_CALL, PAYLOAD)
    with pytest.raises(ModelStepFailed):
        b.call("verifier", EgressPurpose.MODEL_CALL, PAYLOAD)
    with pytest.raises(ModelRefusal):
        b.call("verifier", EgressPurpose.MODEL_CALL, PAYLOAD)


def test_selfhosted_provider_against_local_fake_server_with_redaction() -> None:
    with FakeServer(reply={"verdicts": []}) as srv:
        b = OutboundBoundary(provider=selfhosted(srv.port), internal_model_hosts=frozenset({"127.0.0.1"}))
        r = b.call("verifier", EgressPurpose.MODEL_CALL, PAYLOAD)
        assert r.content == {"verdicts": []} and r.model == "big-model" and r.stop_reason == "complete"
        sent = srv.requests[0]
        assert sent["path"] == "/v1/chat/completions" and sent["auth"] == "Bearer local-key"
        body = json.dumps(sent["body"])
        assert "ops@example.org" not in body and "10.1.2.3" not in body  # redaction applies above the interface
        assert sent["body"]["messages"][0] == {"role": "system", "content": "rules"}
        assert sent["body"]["response_format"]["type"] == "json_schema"
        b.call("triage", EgressPurpose.MODEL_CALL, PAYLOAD)
        assert srv.requests[1]["body"]["model"] == "small-model"
        assert b.stats.internal_calls == 2 and b.stats.egress_attempts == 0 and b.stats.input_tokens == 22


@pytest.mark.parametrize("finish,status,error", [("length", 200, ModelStepFailed), ("content_filter", 200, ModelRefusal),
                                                  ("stop", 500, ModelStepFailed)])
def test_selfhosted_stop_mapping(finish, status, error) -> None:
    with FakeServer(finish=finish, status=status) as srv:
        b = OutboundBoundary(provider=selfhosted(srv.port), internal_model_hosts=frozenset({"127.0.0.1"}))
        with pytest.raises(error):
            b.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)


def test_offline_accepts_only_an_internal_allow_listed_endpoint() -> None:
    with FakeServer() as srv:
        ok = OutboundBoundary(mode=Mode.OFFLINE, provider=selfhosted(srv.port), internal_model_hosts=frozenset({"127.0.0.1"}))
        ok.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)
        assert len(srv.requests) == 1 and ok.stats.egress_attempts == 0

        sink = EventSink()
        not_listed = OutboundBoundary(mode=Mode.OFFLINE, events=sink, provider=selfhosted(srv.port),
                                      internal_model_hosts=frozenset())
        with pytest.raises(OutboundRefused, match="offline"):
            not_listed.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)
        assert len(srv.requests) == 1 and not_listed.stats.egress_attempts == 1
        assert sink.types() == ["outbound_offline_attempt"]

        provider_not_internal = OutboundBoundary(mode=Mode.OFFLINE, provider=selfhosted(srv.port, internal=False),
                                                 internal_model_hosts=frozenset({"127.0.0.1"}))
        with pytest.raises(OutboundRefused):
            provider_not_internal.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)
    anthropic_offline = OutboundBoundary(mode=Mode.OFFLINE, provider=AnthropicProvider(ScriptedTransport([{}])),
                                         internal_model_hosts=frozenset({"api.anthropic.com"}))
    with pytest.raises(OutboundRefused):
        anthropic_offline.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)


def test_containment_and_unknown_roles_apply_to_selfhosted_provider() -> None:
    with FakeServer() as srv:
        c = FakeContainment()
        c.trip("model_calls", "incident")
        b = OutboundBoundary(provider=selfhosted(srv.port), containment=c, internal_model_hosts=frozenset({"127.0.0.1"}))
        with pytest.raises(OutboundRefused, match="containment"):
            b.call("investigator", EgressPurpose.MODEL_CALL, PAYLOAD)
        c.resume("model_calls")
        with pytest.raises(OutboundRefused):
            b.call("someone", EgressPurpose.MODEL_CALL, PAYLOAD)
        assert srv.requests == []


def test_webhook_still_refused_offline_even_with_internal_model() -> None:
    with FakeServer() as srv:
        b = OutboundBoundary(mode=Mode.OFFLINE, provider=selfhosted(srv.port), internal_model_hosts=frozenset({"127.0.0.1"}),
                             allowed_webhook_hosts=frozenset({"127.0.0.1"}))
        with pytest.raises(OutboundRefused):
            b.call("webhook", EgressPurpose.WEBHOOK_DELIVERY, {"url": f"http://127.0.0.1:{srv.port}/h", "body": {}})
        assert b.stats.egress_attempts == 1


def test_provider_from_env() -> None:
    p = provider_from_env({})
    assert p.name == "anthropic" and p.model_for("investigator") == "claude-sonnet-5-5" and p.model_for("triage") == "claude-haiku-5-5"
    p2 = provider_from_env({"ATLAS_MODEL_PROVIDER": "openai_compatible", "ATLAS_MODEL_BASE_URL": "http://llm.internal:8000",
                            "ATLAS_MODEL_REASONING": "open-reasoner", "ATLAS_MODEL_FAST": "open-small",
                            "ATLAS_MODEL_INTERNAL_HOSTS": "llm.internal"})
    assert p2.name == "openai_compatible" and p2.internal and p2.model_for("verifier") == "open-reasoner"
    assert p2.model_for("log_filter") == "open-small"
    with pytest.raises(ValueError):
        provider_from_env({"ATLAS_MODEL_PROVIDER": "openai_compatible"})
    with pytest.raises(ValueError):
        provider_from_env({"ATLAS_MODEL_PROVIDER": "something"})
