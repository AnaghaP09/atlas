"""Evidence access for the agents.

Every record an agent reads or cites passes through one resolver bound to the run's snapshot. The
resolver attaches stored conflicts and supersession, and applies the per-source freshness rule:
age is the evidence clock minus ``confirmed_through`` (the time the source state was last
confirmed, or the cutoff through which reconstructed history was proven complete). A stale record,
or one whose freshness is unknown, is returned as unavailable with the reason, never as a value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Mapping

from atlas.contracts import Record, RunContext, Store

# Maximum evidence age per source (canvas: inputs and data). ``None`` means the source has no age
# limit for a frozen investigation (commits are immutable; CI runs are frozen per investigation).
MAX_AGE: dict[str, timedelta | None] = {
    "issues": timedelta(hours=1),
    "security": timedelta(hours=24),
    "telemetry": timedelta(minutes=15),
    "capacity": timedelta(days=7),
    "kubernetes": timedelta(hours=1),
    "git": None,
    "ci": None,
    "architecture": None,
}


@dataclass(frozen=True)
class Freshness:
    fresh: bool
    reason: str
    age: timedelta | None = None


def freshness(record: Record, ctx: RunContext) -> Freshness:
    limit = MAX_AGE.get(record.source, timedelta(hours=1))
    if limit is None:
        return Freshness(True, "no age limit for this source")
    if record.confirmed_through is None:
        return Freshness(False, "freshness unknown: state not confirmed through the evidence clock")
    age = ctx.evidence_clock - record.confirmed_through
    if age < timedelta(0):
        return Freshness(False, "freshness unknown: confirmation is later than the evidence clock", age)
    if age > limit:
        return Freshness(False, f"stale: {age} old, limit {limit}", age)
    return Freshness(True, "fresh", age)


@dataclass
class ResolvedRecord:
    """A record as an agent sees it: data plus status, never instructions."""

    record: Record
    status: str  # "available" | "unavailable"
    reason: str
    content: str | None
    conflicts: tuple[str, ...]
    superseded_by: str | None
    flags: list[str] = field(default_factory=list)

    @property
    def record_id(self) -> str:
        return self.record.record_id

    def as_data(self) -> dict[str, Any]:
        return {
            "record_id": self.record.record_id,
            "source": self.record.source,
            "source_id": self.record.source_id,
            "status": self.status,
            "reason": self.reason,
            "event_time": self.record.valid_from.isoformat(),
            "confirmed_through": self.record.confirmed_through.isoformat() if self.record.confirmed_through else None,
            "conflicts": list(self.conflicts),
            "superseded_by": self.superseded_by,
            "provenance": str(self.record.provenance),
            "flags": list(self.flags),
            "content": self.content if self.status == "available" else None,
        }


class EvidenceResolver:
    """Snapshot-bound evidence access for one run."""

    def __init__(
        self,
        store: Store,
        ctx: RunContext,
        content_of: Callable[[Record], str] | None = None,
        flagger: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.store = store
        self.ctx = ctx
        self.content_of = content_of or (lambda r: r.content_ref)
        self.flagger = flagger
        self.seen: dict[str, ResolvedRecord] = {}

    def resolve(self, record: Record) -> ResolvedRecord:
        f = freshness(record, self.ctx)
        if record.superseded_by:
            status, reason = "unavailable", f"superseded by {record.superseded_by}"
        elif not f.fresh:
            status, reason = "unavailable", f.reason
        else:
            status, reason = "available", f.reason
        content = self.content_of(record) if status == "available" else None
        flags = self.flagger(content) if (self.flagger and content) else []
        resolved = ResolvedRecord(
            record=record,
            status=status,
            reason=reason,
            content=content,
            conflicts=tuple(record.conflicts),
            superseded_by=record.superseded_by,
            flags=flags,
        )
        self.seen[record.record_id] = resolved
        return resolved

    def get(self, source: str, source_id: str) -> ResolvedRecord | None:
        rec = self.store.get(self.ctx, source, source_id)
        return self.resolve(rec) if rec else None

    def search(self, query: Mapping[str, Any]) -> list[ResolvedRecord]:
        return [self.resolve(r) for r in self.store.search(self.ctx, query)]

    def history(self, source: str, source_id: str) -> list[ResolvedRecord]:
        return [self.resolve(r) for r in self.store.history(self.ctx, source, source_id)]

    def by_id(self, record_id: str) -> ResolvedRecord | None:
        return self.seen.get(record_id)
