"""The outbound boundary: the only code that sends anything to a model or an external chat endpoint.

Every call is redacted first, checked against the operating mode and the containment state, and
counted. A payload that still contains a sensitive category after redaction, or a span the
redactor cannot classify, is withheld and the call fails closed.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from atlas.contracts import EgressPurpose, Mode, ModelResponse

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OutboundRefused(RuntimeError):
    """A call was refused before any connection was opened."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PayloadWithheld(OutboundRefused):
    """The payload could not be made safe and was not sent."""


class ModelRefusal(RuntimeError):
    """The model declined the request; the step has failed."""


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

PUBLIC_HOSTS = frozenset(
    {
        "github.com",
        "api.github.com",
        "prow.ci.kubevirt.io",
        "storage.googleapis.com",
        "api.osv.dev",
        "osv.dev",
        "kubevirt.io",
        "go.dev",
        "pkg.go.dev",
        "quay.io",
        "registry.k8s.io",
    }
)

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)
_TOKEN_PATTERNS = [
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token|client[_-]?secret|"
    r"client-key-data|client-certificate-data|certificate-authority-data)\b(\s*[:=]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_HANDLE = re.compile(r"(?<![\w/@.])@[A-Za-z0-9](?:[A-Za-z0-9\-]{0,38})\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HOSTNAME = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:internal|local|lan|corp|intra|svc|cluster\.local|home|com|net|org|io|dev|cloud)\b"
)
_PERSON_FIELD = re.compile(
    r"(?im)^(\s*(?:author|committer|reporter|assignee|owner|reviewer|user|name)\s*[:=]\s*)(.+)$"
)
_END_USER = re.compile(
    r"(?i)\b(customer[_-]?id|user[_-]?id|account[_-]?number|phone|ssn|credit[_-]?card)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_CANDIDATE_SECRET = re.compile(r"[A-Za-z0-9+_\-=]{32,}")
_HEX_ID = re.compile(r"^[0-9a-f]{7,64}$")


def _entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)
    unclassified: list[str] = field(default_factory=list)

    def add(self, category: str, n: int = 1) -> None:
        if n:
            self.counts[category] = self.counts.get(category, 0) + n


class Redactor:
    """Removes every sensitive category before anything leaves the backend.

    Categories: secrets (tokens, keys, private keys, password and kubeconfig fields, high-entropy
    strings), host names and IP addresses not on the public allow-list, person names in
    attribution fields, emails, handles and end-user identifiers. A long random-looking string
    that is neither a known token nor clearly benign is reported as unclassified, and the caller
    withholds the whole payload.
    """

    def __init__(self, public_hosts: frozenset[str] = PUBLIC_HOSTS) -> None:
        self.public_hosts = public_hosts

    def redact(self, text: str) -> RedactionResult:
        result = RedactionResult(text=text)
        t = text

        t, n = _PRIVATE_KEY.subn("[REDACTED:private_key]", t)
        result.add("private_key", n)
        for pattern in _TOKEN_PATTERNS:
            t, n = pattern.subn("[REDACTED:token]", t)
            result.add("token", n)
        t, n = _KEY_VALUE_SECRET.subn(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:secret]", t)
        result.add("secret_field", n)
        t, n = _EMAIL.subn("[REDACTED:email]", t)
        result.add("email", n)
        t, n = _END_USER.subn(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:end_user]", t)
        result.add("end_user", n)
        t, n = _PERSON_FIELD.subn(lambda m: f"{m.group(1)}[REDACTED:person]", t)
        result.add("person", n)
        t, n = _HANDLE.subn("[REDACTED:handle]", t)
        result.add("handle", n)

        def ip_sub(m: re.Match[str]) -> str:
            parts = m.group(0).split(".")
            if all(p.isdigit() and int(p) <= 255 for p in parts):
                result.add("ip_address")
                return "[REDACTED:ip]"
            return m.group(0)

        t = _IPV4.sub(ip_sub, t)

        def host_sub(m: re.Match[str]) -> str:
            host = m.group(0).lower()
            if host in self.public_hosts or any(host.endswith("." + h) for h in self.public_hosts):
                return m.group(0)
            if re.fullmatch(r"[a-z0-9_\-]+\.(go|py|md|json|yaml|yml|txt|sh|io)", host):
                return m.group(0)  # file names such as main.go are not hosts
            result.add("hostname")
            return "[REDACTED:host]"

        t = _HOSTNAME.sub(host_sub, t)

        def candidate(m: re.Match[str]) -> str:
            s = m.group(0)
            if s.startswith("REDACTED") or _HEX_ID.match(s):
                return s  # commit hashes and digests are identifiers, not secrets
            if _entropy(s) >= 4.0:
                result.add("high_entropy")
                return "[REDACTED:high_entropy]"
            if _entropy(s) >= 3.5 and re.search(r"\d", s) and re.search(r"[A-Z]", s) and re.search(r"[a-z]", s):
                result.unclassified.append(s[:6] + "...")
            return s

        t = _CANDIDATE_SECRET.sub(candidate, t)
        result.text = t
        return result

    def scan(self, text: str) -> list[str]:
        """Categories still present in ``text`` (used after redaction as a second check)."""
        found = []
        if _PRIVATE_KEY.search(text):
            found.append("private_key")
        if any(p.search(text) for p in _TOKEN_PATTERNS):
            found.append("token")
        if _EMAIL.search(text):
            found.append("email")
        if _HANDLE.search(text):
            found.append("handle")
        return found


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


class ContainmentGate(Protocol):
    def permits(self, consumer: str) -> Any: ...


class ContainmentSink(Protocol):
    def record_event(self, event_type: str, detail: Mapping[str, Any]) -> None: ...


class ModelTransport(Protocol):
    def send(self, request: Mapping[str, Any]) -> ModelResponse: ...


class WebhookTransport(Protocol):
    def post(self, url: str, body: Mapping[str, Any]) -> int: ...


# Per million tokens, USD (input, output); used only for the spend counter.
PRICES = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.0, 20.0),
}

MODEL_ROLES = {
    "investigator": "claude-sonnet-5-5",
    "verifier": "claude-sonnet-5-5",
    "coding_agent": "claude-sonnet-5-5",
    "triage": "claude-haiku-5-5",
    "log_filter": "claude-haiku-5-5",
    "injection_classifier": "claude-haiku-5-5",
}


@dataclass
class OutboundStats:
    egress_attempts: int = 0
    refused: int = 0
    withheld: int = 0
    redactions: Counter = field(default_factory=Counter)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    by_purpose: Counter = field(default_factory=Counter)


class OutboundBoundary:
    """Implements the shared ``Outbound`` interface.

    ``payload`` for model purposes: ``{"system": str, "messages": [...], "max_tokens": int,
    "schema": {...} | None, "effort": str | None}``. For webhook delivery: ``{"url": str,
    "body": {...}}``. The role selects the model; callers never choose one.
    """

    MODEL_PURPOSES = frozenset({EgressPurpose.MODEL_CALL})

    def __init__(
        self,
        *,
        mode: Callable[[], Mode] | Mode = Mode.CONNECTED,
        containment: ContainmentGate | None = None,
        events: ContainmentSink | None = None,
        transport: ModelTransport | None = None,
        webhook: WebhookTransport | None = None,
        redactor: Redactor | None = None,
        allowed_webhook_hosts: frozenset[str] = frozenset(),
        spend: Callable[[float], None] | None = None,
    ) -> None:
        self._mode = mode
        self.containment = containment
        self.events = events
        self.transport = transport
        self.webhook = webhook
        self.redactor = redactor or Redactor()
        self.allowed_webhook_hosts = allowed_webhook_hosts
        self.spend = spend
        self.stats = OutboundStats()
        self._lock = threading.Lock()

    @property
    def mode(self) -> Mode:
        return self._mode() if callable(self._mode) else self._mode

    # -- helpers -----------------------------------------------------------

    def _event(self, event_type: str, detail: Mapping[str, Any]) -> None:
        if self.events is not None:
            self.events.record_event(event_type, detail)

    def _refuse(self, reason: str) -> None:
        with self._lock:
            self.stats.refused += 1
        raise OutboundRefused(reason)

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            r = self.redactor.redact(value)
            if r.unclassified:
                raise PayloadWithheld(f"unclassified span(s): {len(r.unclassified)}")
            residue = self.redactor.scan(r.text)
            if residue:
                self._event("outbound_unredacted_pii", {"categories": residue})
                raise PayloadWithheld("sensitive content remained after redaction")
            for k, n in r.counts.items():
                self.stats.redactions[k] += n
            return r.text
        if isinstance(value, Mapping):
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact_value(v) for v in value]
        return value

    def redact_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Redact every string in the payload; raises PayloadWithheld when it cannot be made safe."""
        try:
            return self._redact_value(dict(payload))
        except PayloadWithheld:
            with self._lock:
                self.stats.withheld += 1
            raise

    # -- the boundary --------------------------------------------------------

    def call(self, role: str, purpose: EgressPurpose, payload: Mapping[str, Any]) -> ModelResponse:
        with self._lock:
            self.stats.egress_attempts += 1
            self.stats.by_purpose[str(purpose)] += 1
        if self.mode == Mode.OFFLINE:
            self._event("outbound_offline_attempt", {"role": role, "purpose": str(purpose)})
            self._refuse("offline mode: outbound connections are refused")
        consumer = "model_calls" if purpose in self.MODEL_PURPOSES else "report_delivery"
        if self.containment is not None:
            verdict = self.containment.permits(consumer)
            if not getattr(verdict, "allowed", True):
                self._refuse("containment: " + "; ".join(getattr(verdict, "reasons", ())))

        if purpose == EgressPurpose.WEBHOOK_DELIVERY:
            return self._deliver_webhook(payload)
        if purpose not in self.MODEL_PURPOSES:
            self._event("outbound_not_allowlisted", {"role": role, "purpose": str(purpose)})
            self._refuse(f"purpose {purpose} is not served by the outbound boundary")
        if role not in MODEL_ROLES:
            self._event("outbound_not_allowlisted", {"role": role, "purpose": str(purpose)})
            self._refuse(f"unknown model role {role}")
        if self.transport is None:
            self._refuse("no model transport configured")

        safe = self.redact_payload(payload)
        model = MODEL_ROLES[role]
        request = {
            "model": model,
            "max_tokens": int(safe.get("max_tokens", 4096)),
            "system": safe.get("system", ""),
            "messages": safe["messages"],
        }
        if safe.get("schema"):
            request["output_config"] = {"format": {"type": "json_schema", "schema": safe["schema"]}}
        if safe.get("effort"):
            request.setdefault("output_config", {})["effort"] = safe["effort"]
        response = self.transport.send(request)
        price_in, price_out = PRICES.get(model, (0.0, 0.0))
        cost = (response.input_tokens * price_in + response.output_tokens * price_out) / 1_000_000
        with self._lock:
            self.stats.input_tokens += response.input_tokens
            self.stats.output_tokens += response.output_tokens
            self.stats.cost_usd += cost
        if self.spend is not None:
            self.spend(cost)
        if response.stop_reason == "refusal":
            raise ModelRefusal(f"model declined the request ({role})")
        return response

    def _deliver_webhook(self, payload: Mapping[str, Any]) -> ModelResponse:
        url = str(payload.get("url", ""))
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
        if host not in self.allowed_webhook_hosts:
            self._event("outbound_not_allowlisted", {"purpose": "webhook_delivery"})
            self._refuse("webhook destination is not on the allow-list")
        if self.webhook is None:
            self._refuse("no webhook transport configured")
        body = self.redact_payload(payload.get("body", {}))
        status = self.webhook.post(url, body)
        return ModelResponse(content={"status": status}, stop_reason="delivered", input_tokens=0, output_tokens=0, model="webhook")


class SpendMeter:
    """Hard spend ceiling for model calls. Notifies at 80% and 100%; refuses calls at the ceiling.

    Pass ``meter.add`` as the boundary's ``spend`` callback and ``meter.hold_reason`` to the
    scheduler so automatic work is held, not dropped, once the ceiling is reached.
    """

    def __init__(self, ceiling_usd: float, notify: Callable[[str, float], None] | None = None) -> None:
        self.ceiling = float(ceiling_usd)
        self.spent = 0.0
        self.notify = notify
        self._sent: set[str] = set()
        self._lock = threading.Lock()

    def add(self, usd: float) -> None:
        with self._lock:
            self.spent += usd
            for mark, event in ((1.0, "spend.100"), (0.8, "spend.80")):
                if self.spent >= mark * self.ceiling and event not in self._sent:
                    self._sent.add(event)
                    if self.notify:
                        self.notify(event, self.spent)

    def hold_reason(self) -> str | None:
        return "spend ceiling reached" if self.spent >= self.ceiling else None


class CeilingGate:
    """Containment-style gate that refuses model calls once the spend ceiling is reached."""

    def __init__(self, meter: SpendMeter, inner: ContainmentGate | None = None) -> None:
        self.meter = meter
        self.inner = inner

    def permits(self, consumer: str) -> Any:
        from atlas.contracts import EligibilityResult

        if consumer == "model_calls" and self.meter.hold_reason():
            return EligibilityResult(False, ("spend ceiling reached",))
        return self.inner.permits(consumer) if self.inner else EligibilityResult(True)


def parse_json_content(response: ModelResponse) -> Any:
    """Return the structured JSON of a response; content may already be parsed (fakes)."""
    content = response.content
    if isinstance(content, (dict, list)):
        return content
    if isinstance(content, str):
        return json.loads(content)
    raise ValueError("response has no JSON content")


class AnthropicTransport:
    """Sends requests with the official SDK. Constructed only here and only at startup."""

    def __init__(self, api_key: str, timeout: float = 120.0) -> None:
        import anthropic  # imported lazily so tests never need network or keys

        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)

    def send(self, request: Mapping[str, Any]) -> ModelResponse:
        msg = self._client.messages.create(**request)
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        content: Any = text
        if "output_config" in request and "format" in request["output_config"] and text:
            try:
                content = json.loads(text)
            except ValueError:
                content = text
        return ModelResponse(
            content=content,
            stop_reason=str(msg.stop_reason),
            input_tokens=msg.usage.input_tokens,
            output_tokens=msg.usage.output_tokens,
            model=request["model"],
        )
