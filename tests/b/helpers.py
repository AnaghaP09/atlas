"""Test helpers shared by the agents-layer tests."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from atlas.contracts import EgressPurpose, ModelResponse


class ScriptedTransport:
    """Returns pre-scripted model responses in order and keeps every request it was sent."""

    def __init__(self, responses: Sequence[Any] = (), stop_reason: str = "end_turn") -> None:
        self.responses = list(responses)
        self.stop_reason = stop_reason
        self.requests: list[Mapping[str, Any]] = []

    def send(self, request: Mapping[str, Any]) -> ModelResponse:
        self.requests.append(request)
        if not self.responses:
            raise KeyError("no scripted response left")
        item = self.responses.pop(0)
        stop = self.stop_reason
        if isinstance(item, tuple):
            item, stop = item
        return ModelResponse(content=item, stop_reason=stop, input_tokens=100, output_tokens=20, model=request["model"])


class ScriptedOutbound:
    """Outbound fake for agent tests: returns scripted content per role, records every call."""

    def __init__(self, by_role: Mapping[str, Sequence[Any]] | None = None, offline: bool = False) -> None:
        self.by_role = {k: list(v) for k, v in (by_role or {}).items()}
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.offline = offline

    def call(self, role: str, purpose: EgressPurpose, payload: Mapping[str, Any]) -> ModelResponse:
        self.calls.append((role, payload))
        if self.offline:
            raise ConnectionRefusedError("offline")
        queue = self.by_role.get(role)
        if not queue:
            raise KeyError(f"no scripted response for role {role}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        stop = "end_turn"
        if isinstance(item, tuple):
            item, stop = item
        return ModelResponse(content=item, stop_reason=stop, input_tokens=100, output_tokens=20, model="scripted")

    def roles(self) -> list[str]:
        return [r for r, _ in self.calls]


class EventSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, Any]]] = []

    def record_event(self, event_type: str, detail: Mapping[str, Any]) -> None:
        self.events.append((event_type, dict(detail)))

    def types(self) -> list[str]:
        return [e for e, _ in self.events]
