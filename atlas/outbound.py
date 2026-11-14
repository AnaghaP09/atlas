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
from typing import Any, Callable, Mapping, Protocol, Sequence

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


class ModelStepFailed(RuntimeError):
    """The model call did not complete (truncated or failed); the step has failed."""


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
    """Low-level sender used by the Anthropic provider (the SDK in production, a fake in tests)."""

    def send(self, request: Mapping[str, Any]) -> ModelResponse: ...


class WebhookTransport(Protocol):
    def post(self, url: str, body: Mapping[str, Any]) -> int: ...


# ---------------------------------------------------------------------------
# Model providers
# ---------------------------------------------------------------------------

STOP_COMPLETE, STOP_REFUSED, STOP_TRUNCATED, STOP_FAILED = "complete", "refused", "truncated", "failed"
ROLES = ("investigator", "verifier", "coding_agent", "triage", "log_filter", "injection_classifier")
REASONING_ROLES = frozenset({"investigator", "verifier", "coding_agent"})


@dataclass(frozen=True)
class ModelLimits:
    max_tokens: int = 4096
    effort: str | None = None


@dataclass(frozen=True)
class ModelResult:
    """Provider-neutral result of one model call."""

    content: Any  # parsed structured output (dict or list) or text
    stop: str  # complete | refused | truncated | failed
    input_tokens: int
    output_tokens: int
    model: str
    provider: str


class ModelProvider(Protocol):
    """One model backend. Used only by the outbound boundary, after redaction and every check."""

    name: str
    internal: bool  # True when the endpoint is inside the organisation's network

    def model_for(self, role: str) -> str: ...

    def endpoint_host(self) -> str | None: ...

    def complete(self, role: str, messages: Sequence[Mapping[str, Any]], schema: Mapping[str, Any] | None,
                 limits: ModelLimits) -> ModelResult: ...


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

_ANTHROPIC_STOPS = {"end_turn": STOP_COMPLETE, "stop_sequence": STOP_COMPLETE, "tool_use": STOP_COMPLETE,
                    "pause_turn": STOP_FAILED, "refusal": STOP_REFUSED, "max_tokens": STOP_TRUNCATED}
_OPENAI_STOPS = {"stop": STOP_COMPLETE, "length": STOP_TRUNCATED, "content_filter": STOP_REFUSED}


def _split_system(messages: Sequence[Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system = "\n\n".join(str(m["content"]) for m in messages if m.get("role") == "system")
    return system, [dict(m) for m in messages if m.get("role") != "system"]


class AnthropicProvider:
    """Claude through the official SDK (Connected mode)."""

    name = "anthropic"
    internal = False

    def __init__(self, transport: ModelTransport | None = None, *, api_key: str | None = None,
                 models: Mapping[str, str] | None = None) -> None:
        self.transport = transport if transport is not None else (AnthropicTransport(api_key) if api_key else None)
        self.models = dict(MODEL_ROLES, **(models or {}))

    def model_for(self, role: str) -> str:
        return self.models[role]

    def endpoint_host(self) -> str | None:
        return "api.anthropic.com"

    def complete(self, role, messages, schema, limits):
        if self.transport is None:
            raise RuntimeError("no Anthropic transport configured")
        system, rest = _split_system(messages)
        model = self.model_for(role)
        request: dict[str, Any] = {"model": model, "max_tokens": int(limits.max_tokens), "system": system, "messages": rest}
        if schema:
            request["output_config"] = {"format": {"type": "json_schema", "schema": dict(schema)}}
        if limits.effort:
            request.setdefault("output_config", {})["effort"] = limits.effort
        r = self.transport.send(request)
        return ModelResult(content=r.content, stop=_ANTHROPIC_STOPS.get(str(r.stop_reason), STOP_FAILED),
                           input_tokens=r.input_tokens, output_tokens=r.output_tokens, model=model, provider=self.name)


class OpenAICompatibleProvider:
    """A self-hosted, OpenAI-compatible chat endpoint (for example vLLM serving an open-weight model).

    Intended for Offline mode inside the organisation's network. The HTTP client is created here,
    inside the outbound boundary, and only for the configured endpoint.
    """

    name = "openai_compatible"

    def __init__(self, base_url: str, models: Mapping[str, str], *, api_key: str = "",
                 internal_hosts: frozenset[str] = frozenset(), timeout: float = 120.0, client: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.models = dict(models)
        self.api_key = api_key
        self.internal_hosts = frozenset(h.lower() for h in internal_hosts)
        self.timeout = timeout
        self._client = client

    @property
    def internal(self) -> bool:
        return (self.endpoint_host() or "") in self.internal_hosts

    def endpoint_host(self) -> str | None:
        return re.sub(r"^https?://", "", self.base_url).split("/")[0].split(":")[0].lower() or None

    def model_for(self, role: str) -> str:
        return self.models[role]

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def complete(self, role, messages, schema, limits):
        model = self.model_for(role)
        body: dict[str, Any] = {"model": model, "messages": [dict(m) for m in messages], "max_tokens": int(limits.max_tokens)}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": dict(schema), "strict": True}}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = self._http().post(f"{self.base_url}/v1/chat/completions", json=body, headers=headers)
        except Exception:
            return ModelResult(None, STOP_FAILED, 0, 0, model, self.name)
        if resp.status_code != 200:
            return ModelResult(None, STOP_FAILED, 0, 0, model, self.name)
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        content: Any = text
        if schema and text:
            try:
                content = json.loads(text)
            except ValueError:
                return ModelResult(text, STOP_FAILED, 0, 0, model, self.name)
        usage = data.get("usage") or {}
        return ModelResult(content, _OPENAI_STOPS.get(str(choice.get("finish_reason")), STOP_FAILED),
                           int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)), model, self.name)


def provider_from_env(env: Mapping[str, str]) -> ModelProvider:
    """Build the provider named by ``ATLAS_MODEL_PROVIDER`` (default ``anthropic``)."""
    kind = (env.get("ATLAS_MODEL_PROVIDER") or "anthropic").strip().lower()
    reasoning = env.get("ATLAS_MODEL_REASONING") or ""
    fast = env.get("ATLAS_MODEL_FAST") or ""
    if kind == "anthropic":
        models = {r: (reasoning or MODEL_ROLES[r]) if r in REASONING_ROLES else (fast or MODEL_ROLES[r]) for r in ROLES}
        key = env.get("ANTHROPIC_API_KEY") or ""
        return AnthropicProvider(api_key=key or None, models=models)
    if kind == "openai_compatible":
        base = env.get("ATLAS_MODEL_BASE_URL") or ""
        if not base or not reasoning or not fast:
            raise ValueError("openai_compatible needs ATLAS_MODEL_BASE_URL, ATLAS_MODEL_REASONING and ATLAS_MODEL_FAST")
        hosts = frozenset(h.strip() for h in (env.get("ATLAS_MODEL_INTERNAL_HOSTS") or "").split(",") if h.strip())
        models = {r: reasoning if r in REASONING_ROLES else fast for r in ROLES}
        return OpenAICompatibleProvider(base, models, api_key=env.get("ATLAS_MODEL_API_KEY") or "", internal_hosts=hosts)
    raise ValueError(f"unknown model provider {kind!r}")


@dataclass
class OutboundStats:
    egress_attempts: int = 0
    internal_calls: int = 0
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
        provider: ModelProvider | None = None,
        transport: ModelTransport | None = None,
        internal_model_hosts: frozenset[str] = frozenset(),
        webhook: WebhookTransport | None = None,
        redactor: Redactor | None = None,
        allowed_webhook_hosts: frozenset[str] = frozenset(),
        spend: Callable[[float], None] | None = None,
    ) -> None:
        self._mode = mode
        self.containment = containment
        self.events = events
        # ``transport`` is accepted for compatibility and wraps the Anthropic provider.
        self.provider: ModelProvider | None = provider or (AnthropicProvider(transport) if transport else None)
        self.internal_model_hosts = frozenset(h.lower() for h in internal_model_hosts)
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

    def _internal_model(self) -> bool:
        p = self.provider
        return bool(p is not None and getattr(p, "internal", False)
                    and (p.endpoint_host() or "") in self.internal_model_hosts)

    def call(self, role: str, purpose: EgressPurpose, payload: Mapping[str, Any]) -> ModelResponse:
        internal = purpose in self.MODEL_PURPOSES and self._internal_model()
        with self._lock:
            if internal:
                self.stats.internal_calls += 1
            else:
                self.stats.egress_attempts += 1
            self.stats.by_purpose[str(purpose)] += 1
        if self.mode == Mode.OFFLINE and not internal:
            self._event("outbound_offline_attempt", {"role": role, "purpose": str(purpose)})
            self._refuse("offline mode: only a model endpoint on the internal allow-list may be used")
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
        if self.provider is None:
            self._refuse("no model provider configured")

        safe = self.redact_payload(payload)
        messages = ([{"role": "system", "content": safe["system"]}] if safe.get("system") else []) + list(safe["messages"])
        limits = ModelLimits(max_tokens=int(safe.get("max_tokens", 4096)), effort=safe.get("effort"))
        result = self.provider.complete(role, messages, safe.get("schema"), limits)
        price_in, price_out = PRICES.get(result.model, (0.0, 0.0))
        cost = (result.input_tokens * price_in + result.output_tokens * price_out) / 1_000_000
        with self._lock:
            self.stats.input_tokens += result.input_tokens
            self.stats.output_tokens += result.output_tokens
            self.stats.cost_usd += cost
        if self.spend is not None:
            self.spend(cost)
        if result.stop == STOP_REFUSED:
            raise ModelRefusal(f"model declined the request ({role})")
        if result.stop == STOP_TRUNCATED:
            raise ModelStepFailed(f"model output was truncated ({role})")
        if result.stop != STOP_COMPLETE:
            raise ModelStepFailed(f"model call failed ({role}, {result.provider})")
        return ModelResponse(content=result.content, stop_reason=result.stop, input_tokens=result.input_tokens,
                             output_tokens=result.output_tokens, model=result.model)

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
