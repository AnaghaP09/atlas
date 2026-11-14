"""Server-sent event streams: one hub, two streams, projected per reader.

Producers (run events from the agents layer, notifications) publish into the hub. The hub keeps a
bounded replay buffer. A client reconnects with ``Last-Event-ID``; if that id is older than the
buffer, the hub sends ``stream.reset`` with ``{reason, resume_from}`` and continues from
``resume_from``. Every event is projected for the reader before it is sent; an event the reader may
not see is skipped entirely. Event names and fields follow ``schema/events.json`` exactly.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from atlas.contracts import EVENT_NAMES, Principal

WIRE_FIELDS = ("event", "event_id", "emitted_at", "run_id", "authoritative_generation", "report_version",
               "correction_version", "data")
RUN_STREAM = "runs"
NOTIFICATION_STREAM = "notifications"


def to_wire(producer_event: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a producer event as the schema's wire event. ``payload`` and ``agent`` become ``data``."""
    data = dict(producer_event.get("data") or producer_event.get("payload") or {})
    if producer_event.get("agent"):
        data.setdefault("agent", producer_event["agent"])
    return {
        "event": producer_event["event"],
        "event_id": producer_event["event_id"],
        "emitted_at": producer_event["emitted_at"],
        "run_id": producer_event.get("run_id"),
        "authoritative_generation": producer_event.get("authoritative_generation"),
        "report_version": producer_event.get("report_version"),
        "correction_version": producer_event.get("correction_version"),
        "data": data,
    }


def encode(event: Mapping[str, Any]) -> str:
    """One SSE frame. ``stream.reset`` carries no ``id:`` line so the client keeps its position."""
    lines = []
    if event["event"] != "stream.reset":
        lines.append(f"id: {event['event_id']}")
    lines.append(f"event: {event['event']}")
    lines.append("data: " + json.dumps(event, separators=(",", ":"), default=str))
    return "\n".join(lines) + "\n\n"


KEEPALIVE = ": keepalive\n\n"


class StreamHub:
    def __init__(self, projection: Any, buffer_size: int = 1000,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.projection = projection
        self.clock = clock
        self._buf: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        self._lock = threading.Lock()
        self.version = 0

    # --------------------------------------------------------------- publishing

    def publish(self, stream: str, wire: Mapping[str, Any], required_scopes: Sequence[str],
                roles: Sequence[str] | None = None) -> None:
        if wire["event"] not in EVENT_NAMES or wire["event"] == "stream.reset":
            raise ValueError(f"event {wire['event']!r} cannot be published by a producer")
        entry = {"stream": stream, "wire": {k: wire.get(k) for k in WIRE_FIELDS},
                 "required_scopes": list(required_scopes), "roles": list(roles) if roles is not None else None}
        with self._lock:
            self._buf.append(entry)
            self.version += 1

    def on_run_event(self, producer_event: Mapping[str, Any]) -> None:
        """Subscriber for the agents layer's event producer."""
        self.publish(RUN_STREAM, to_wire(producer_event), producer_event.get("required_scopes", ["internal"]))

    def on_notification(self, record: Mapping[str, Any]) -> None:
        roles = list(record.get("roles", [])) + list(record.get("backup_roles", []))
        content = record.get("content") or {}
        wire = {
            "event": "notification.created",
            "event_id": f"evt-{uuid.uuid4().hex[:12]}",
            "emitted_at": self.clock().isoformat(),
            "run_id": record.get("run_id"),
            "authoritative_generation": None,
            "report_version": None,
            "correction_version": None,
            "data": {"notification_id": record["id"], "event_type": record["event_type"],
                     "severity": record["severity"], "kind": record["kind"], "subject_id": record["subject_id"],
                     "content": content},
        }
        self.publish(NOTIFICATION_STREAM, wire, record.get("required_scopes", ["internal"]), roles)

    # ---------------------------------------------------------------- reading

    def _visible(self, entry: Mapping[str, Any], principal: Principal) -> dict[str, Any] | None:
        if entry["roles"] is not None and (principal.role is None or principal.role.value not in entry["roles"]):
            return None
        projected = self.projection.project(
            {"required_scopes": entry["required_scopes"], "wire": entry["wire"]}, principal)
        wire = projected.get("wire")
        if not wire:
            return None
        if wire.get("data") is None:
            wire["data"] = {}
        return wire

    def read(self, stream: str, principal: Principal, last_event_id: str | None = None,
             run_id: str | None = None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Events after ``last_event_id`` for this reader, preceded by a reset when the id fell out."""
        with self._lock:
            entries = [e for e in self._buf if e["stream"] == stream]
        reset = None
        if last_event_id:
            ids = [e["wire"]["event_id"] for e in entries]
            if last_event_id in ids:
                entries = entries[ids.index(last_event_id) + 1:]
            else:
                # The position is older than the buffer (or was never seen): the client must refetch.
                resume_from = entries[0]["wire"]["event_id"] if entries else None
                reset = {
                    "event": "stream.reset", "event_id": f"reset-{uuid.uuid4().hex[:10]}",
                    "emitted_at": self.clock().isoformat(), "run_id": run_id, "authoritative_generation": None,
                    "report_version": None, "correction_version": None,
                    "data": {"reason": "buffer_gap", "resume_from": resume_from},
                }
        out = []
        for e in entries:
            if run_id is not None and e["wire"].get("run_id") != run_id:
                continue
            w = self._visible(e, principal)
            if w is not None:
                out.append(w)
        return reset, out
