"""In-memory fakes for every shared interface, used by unit tests before real components exist."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from atlas.contracts import (
    EgressPurpose,
    EligibilityResult,
    ModelResponse,
    Principal,
    Record,
    RunContext,
)

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now


class FakeConnector:
    """Returns pre-loaded records for a source, filtered to the run's snapshot."""

    def __init__(self, name: str, records: Sequence[Record] = ()) -> None:
        self.name = name
        self._records = list(records)
        self.calls: list[Mapping[str, Any]] = []

    def fetch(self, query: Mapping[str, Any], ctx: RunContext) -> list[Record]:
        self.calls.append(dict(query))
        return [r for r in self._records if r.valid_from <= ctx.cutoff]


class InMemoryStore:
    """Evidence store fake: one partition per snapshot, single writer per source, revision ordering."""

    def __init__(self, writers: Mapping[str, str] | None = None) -> None:
        self._writers = dict(writers or {})
        self._partitions: dict[str, list[Record]] = {}
        self.snapshot_for_write = "live"

    def put(self, record: Record, writer: str) -> None:
        expected = self._writers.get(record.source)
        if expected is not None and expected != writer:
            raise PermissionError(f"{writer} may not write source {record.source}")
        self._partitions.setdefault(self.snapshot_for_write, []).append(record)

    def _visible(self, ctx: RunContext) -> list[Record]:
        return self._partitions.get(ctx.snapshot_id, [])

    def history(self, ctx: RunContext, source: str, source_id: str) -> list[Record]:
        rows = [r for r in self._visible(ctx) if r.source == source and r.source_id == source_id]
        return sorted(rows, key=lambda r: (str(r.revision) if r.revision is not None else "", r.valid_from))

    def get(self, ctx: RunContext, source: str, source_id: str) -> Record | None:
        rows = self.history(ctx, source, source_id)
        if not rows:
            return None
        newest = rows[-1]
        conflicts = tuple(
            r.record_id for r in self._visible(ctx) if r.record_id in newest.conflicts
        )
        return replace(newest, conflicts=conflicts or newest.conflicts)

    def search(self, ctx: RunContext, query: Mapping[str, Any]) -> list[Record]:
        source = query.get("source")
        return [r for r in self._visible(ctx) if source is None or r.source == source]


@dataclass
class RecordedModel:
    """Returns recorded responses keyed by a hash of the request payload.

    Unknown requests raise, so tests never reach a real model by accident.
    """

    responses: dict[str, Any] = field(default_factory=dict)
    default: Any = None

    @staticmethod
    def key(payload: Mapping[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()

    def record(self, payload: Mapping[str, Any], response: Any) -> None:
        self.responses[self.key(payload)] = response

    @classmethod
    def from_file(cls, path: Path) -> "RecordedModel":
        return cls(responses=json.loads(path.read_text(encoding="utf-8")))

    def respond(self, payload: Mapping[str, Any]) -> Any:
        k = self.key(payload)
        if k in self.responses:
            return self.responses[k]
        if self.default is not None:
            return self.default
        raise KeyError(f"no recorded response for request {k[:12]}")


class FakeOutbound:
    """Outbound boundary fake: counts attempts per purpose and refuses everything in offline mode."""

    def __init__(self, model: RecordedModel | None = None, offline: bool = False) -> None:
        self.model = model or RecordedModel(default={"text": "ok"})
        self.offline = offline
        self.attempts: list[tuple[str, EgressPurpose]] = []

    def call(self, role: str, purpose: EgressPurpose, payload: Mapping[str, Any]) -> ModelResponse:
        self.attempts.append((role, purpose))
        if self.offline:
            raise ConnectionRefusedError("offline mode: outbound connections are refused")
        content = self.model.respond(payload)
        return ModelResponse(content=content, stop_reason="end_turn", input_tokens=10, output_tokens=5, model="recorded")


class FakeTool:
    def __init__(self, name: str, result: Any = None) -> None:
        self.name = name
        self.input_schema: Mapping[str, Any] = {"type": "object"}
        self.result = result
        self.calls: list[Mapping[str, Any]] = []

    def run(self, ctx: RunContext, arguments: Mapping[str, Any]) -> Any:
        self.calls.append(dict(arguments))
        return self.result


@dataclass
class _Approval:
    approval_id: str
    run_id: str
    generation: int
    kind: str
    voided: bool = False


class FakeApprovals:
    """Approvals fake implementing the void_superseded contract."""

    def __init__(self) -> None:
        self.rows: list[_Approval] = []

    def add(self, run_id: str, generation: int, kind: str = "A") -> str:
        approval_id = f"apr-{uuid.uuid4().hex[:8]}"
        self.rows.append(_Approval(approval_id, run_id, generation, kind))
        return approval_id

    def void_superseded(self, run_id: str, new_generation: int) -> list[str]:
        voided = []
        for row in self.rows:
            if row.run_id == run_id and row.generation < new_generation and not row.voided:
                row.voided = True
                voided.append(row.approval_id)
        return voided


class FakeProjection:
    """Removes every object whose required_scopes are not all held by the principal.

    Objects with unknown scopes (key missing) are withheld from everyone.
    """

    def project(self, artifact: Mapping[str, Any], principal: Principal) -> dict[str, Any]:
        withheld = 0

        def visible(obj: Mapping[str, Any]) -> bool:
            scopes = obj.get("required_scopes")
            return scopes is not None and set(scopes) <= set(principal.scopes)

        def walk(value: Any) -> Any:
            nonlocal withheld
            if isinstance(value, dict):
                if "required_scopes" in value and not visible(value):
                    withheld += 1
                    return None
                return {k: walk(v) for k, v in value.items()}
            if isinstance(value, list):
                out = []
                for item in value:
                    projected = walk(item)
                    if projected is not None:
                        out.append(projected)
                return out
            return value

        result = walk(dict(artifact)) or {}
        result["restricted_items"] = withheld
        return result


class FakeNotifier:
    """Notifier fake that takes severity and roles only from the event catalog."""

    def __init__(self, catalog_path: Path | None = None) -> None:
        path = catalog_path or SCHEMA_DIR / "notification_events.json"
        self.catalog = json.loads(path.read_text(encoding="utf-8"))["events"]
        self.records: list[dict[str, Any]] = []

    def emit(self, event_type: str, subject_id: str, payload_refs: Sequence[str], required_scopes: frozenset[str]) -> str:
        if event_type not in self.catalog:
            raise KeyError(f"event type not in catalog: {event_type}")
        entry = self.catalog[event_type]
        notification_id = f"ntf-{uuid.uuid4().hex[:8]}"
        self.records.append(
            {
                "id": notification_id,
                "event_type": event_type,
                "subject_id": subject_id,
                "severity": entry["severity"],
                "kind": entry["kind"],
                "roles": list(entry["roles"]),
                "backup_roles": list(entry["backup_roles"]),
                "payload_refs": list(payload_refs),
                "required_scopes": sorted(required_scopes),
            }
        )
        return notification_id


class FakeContainment:
    def __init__(self) -> None:
        self.tripped: dict[str, str] = {}

    def trip(self, consumer: str, reason: str) -> None:
        self.tripped[consumer] = reason

    def resume(self, consumer: str) -> None:
        self.tripped.pop(consumer, None)

    def permits(self, consumer: str) -> EligibilityResult:
        if consumer in self.tripped:
            return EligibilityResult(False, (self.tripped[consumer],))
        return EligibilityResult(True)
