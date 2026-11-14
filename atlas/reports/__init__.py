"""Report renderers and the live run-event producer.

Renderers take a verified artefact, project it for the reader, and return JSON plus Markdown. They
never read the store. Events use only the names in ``schema/events.json``; ``stream.reset`` belongs
to the stream layer and is never produced here. Every event and report carries the authoritative
generation, report version and correction version.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from atlas.agents import DISPLAY_NAMES, SCOUT, SENTINEL
from atlas.contracts import EVENT_NAMES, Principal

REPORT_TYPES = ("forecast", "allocation", "architecture", "investigation", "security_gate", "scorecard", "question")
DATA_MODE_NOTE = {
    "REAL": "Real data.",
    "SYNTHETIC": "Synthetic data built for the demo; not a measured result.",
    "STAGED": "Not available in this demo.",
    "UNEVALUATED": "Not evaluated in this build.",
}


class EventProducer:
    """Produces run events and serves them, projected, after a client's last event id."""

    def __init__(self, projection: Any, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.projection = projection
        self.clock = clock
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.subscribers: list[Callable[[dict[str, Any]], None]] = []

    def emit(self, event: str, *, run_id: str | None, authoritative_generation: int, report_version: int = 1,
             correction_version: int = 0, payload: Mapping[str, Any] | None = None, agent: str | None = None,
             required_scopes: Sequence[str] = ("internal",)) -> dict[str, Any]:
        if event == "stream.reset":
            raise ValueError("stream.reset is sent by the stream layer, not by run producers")
        if event not in EVENT_NAMES:
            raise ValueError(f"unknown event name {event}")
        if agent not in (None, SCOUT, SENTINEL):
            raise ValueError("agent must be scout or sentinel")
        e = {
            "event": event,
            "event_id": f"evt-{uuid.uuid4().hex[:12]}",
            "emitted_at": self.clock().isoformat(),
            "run_id": run_id,
            "authoritative_generation": authoritative_generation,
            "report_version": report_version,
            "correction_version": correction_version,
            "payload": dict(payload or {}),
            "required_scopes": list(required_scopes),
        }
        if agent:
            e["agent"] = agent
        with self._lock:
            self._events.append(e)
        for fn in list(self.subscribers):
            fn(e)
        return e

    def since(self, last_event_id: str | None, principal: Principal, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            events = list(self._events)
        if last_event_id:
            ids = [e["event_id"] for e in events]
            events = events[ids.index(last_event_id) + 1:] if last_event_id in ids else events
        if run_id:
            events = [e for e in events if e["run_id"] == run_id]
        out = []
        for e in events:
            projected = self.projection.project({"required_scopes": [], "event": e}, principal)
            if projected.get("event"):
                out.append(projected["event"])
        return out


def _bullets(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    return [f"- {e.get('text')}" for e in entries if e.get("text")]


def render(report_type: str, artefact: Mapping[str, Any], principal: Principal, projection: Any,
           extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Project the verified artefact for the reader and render it."""
    if report_type not in REPORT_TYPES:
        raise ValueError(f"unknown report type {report_type}")
    data = projection.project(artefact, principal)
    extra = projection.project(dict(extra or {}, required_scopes=[]), principal) if extra else {}
    mode = data.get("data_mode", "REAL")
    title = {
        "forecast": "Next-release debt and risk forecast",
        "allocation": "Allocation plan",
        "architecture": "Architecture hotspots and debt acceptance",
        "investigation": "Investigation assessment",
        "security_gate": "Security gate report",
        "scorecard": "Agent reliability scorecard",
        "question": "Answer",
    }[report_type]
    lines = [f"# {title}", "",
             f"Run `{data.get('run_id')}` · generation {data.get('authoritative_generation')} · "
             f"report version {data.get('report_version', 1)} · correction version {data.get('correction_version', 0)}",
             f"Data: **{mode}**. {DATA_MODE_NOTE.get(mode, '')}",
             f"Investigated by {DISPLAY_NAMES[SCOUT]} (agent); every claim checked by {DISPLAY_NAMES[SENTINEL]} (agent).",
             ""]
    status = data.get("run_status")
    if status in ("unverified", "partial", "failed"):
        lines += [f"**Status: {status}.** Nothing in this report is verified for action.", ""]
    cls = (data.get("classification") or {}).get("value")
    act = (data.get("action") or {}).get("value")
    if report_type in ("investigation", "question"):
        lines += [f"Classification: {cls or 'withheld or not supported'}", f"Recommended action: {act or 'none'}"]
        if data.get("safety_disposition") == "HOLD_FOR_HUMAN":
            lines.append("Held for a person: instruction-like text was found in the evidence.")
        if data.get("root_cause"):
            lines.append(f"Root cause: {data['root_cause'].get('text')}")
        lines += ["", "## Findings"] + _bullets(data.get("findings", []))
    if report_type == "forecast":
        f = data.get("forecast") if isinstance(data.get("forecast"), Mapping) else {}
        lines += ["## Items likely to carry over", "Probabilities are illustrative, not calibrated."]
        for item in f.get("items", []):
            lines.append(f"- {item.get('issue_id')} ({item.get('component')}): score {item.get('score')}")
        if f.get("proxy_note"):
            lines += ["", f"Note: {f['proxy_note']}"]
    if report_type == "allocation":
        lines += ["## Recommended allocation"] + [f"- {i.get('id')}: {i.get('allocation')}" for i in data.get("items", [])]
        for opt in (extra.get("staffing") or {}).get("options", []):
            lines.append(f"- Scenario '{opt['name']}': unassigned {len(opt['unassigned'])}")
        lines.append("Staffing scenarios use synthetic role data and carry no dates.")
    if report_type == "architecture":
        lines += ["## Hotspots"] + [f"- {h.get('component')}: {h.get('reason')}" for h in extra.get("hotspots", [])]
    if report_type == "security_gate":
        sc = data.get("security_check") or {}
        lines += [f"State: {sc.get('state')}", f"Valid until: {sc.get('valid_until')}"]
        lines += [f"- {f.get('finding_id')} {f.get('severity')} ({'blocking' if f.get('blocking') else 'not blocking'})"
                  for f in sc.get("findings", [])]
    if report_type == "scorecard":
        for row in extra.get("rows", []):
            lines.append(f"- {row.get('task_class')}: net contribution {row.get('net_contribution', 'unavailable')}")
    lines += ["", f"{data.get('restricted_items', 0)} item(s) withheld for this reader."]
    return {"report_type": report_type, "json": data, "markdown": "\n".join(lines),
            "authoritative_generation": data.get("authoritative_generation"),
            "report_version": data.get("report_version", 1), "correction_version": data.get("correction_version", 0)}
