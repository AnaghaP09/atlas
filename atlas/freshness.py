"""Freshness of evidence, measured only from the time through which a state was confirmed.

Three times are kept on every record and must not be confused:

* the event or content time (``valid_from`` / ``observed_at``): when the content last changed;
* the acquisition time (``fetched_at``): when the backend downloaded it;
* ``confirmed_through``: the time through which the source state is confirmed to be the state the
  record describes.

Only ``confirmed_through`` decides freshness. An issue that has not changed for a month but was
fetched successfully a minute ago is fresh; a reconstructed historical record is fresh at the
cutoff only when its history was proven complete through the cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from atlas.contracts import Record


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class FreshnessBasis(StrEnum):
    LIVE = "live"
    RECONSTRUCTED = "reconstructed"
    SCAN = "scan"
    SERIES_END = "series_end"
    PER_RUN = "per_run"
    IDENTITY = "identity"


#: Maximum age per source. ``None`` means the source is bound to an identity or to the run
#: (a commit, a frozen CI run, an architecture revision) and does not age while that identity holds.
MAX_AGE: dict[str, timedelta | None] = {
    "issues": timedelta(hours=1),
    "git": None,
    "ci": None,
    "kubernetes": timedelta(hours=1),
    "security": timedelta(hours=24),
    "telemetry": timedelta(minutes=15),
    "architecture": None,
    "capacity": timedelta(days=7),
    "target_ci": None,
    "derived": None,
}


@dataclass(frozen=True)
class Freshness:
    status: FreshnessStatus
    age: timedelta | None
    fresh_until: datetime | None
    reason: str

    @property
    def usable(self) -> bool:
        return self.status is FreshnessStatus.FRESH


def fresh_until(source: str, confirmed_through: datetime | None) -> datetime | None:
    """When a record stops being fresh. ``None`` if unknown or if the source does not age."""
    if confirmed_through is None:
        return None
    limit = MAX_AGE.get(source)
    if limit is None:
        return None
    return confirmed_through + limit


def evaluate(source: str, confirmed_through: datetime | None, evidence_clock: datetime) -> Freshness:
    """Freshness of one observation against the run's evidence clock."""
    if source not in MAX_AGE:
        return Freshness(FreshnessStatus.UNKNOWN, None, None, f"no freshness rule for source {source}")
    if confirmed_through is None:
        return Freshness(FreshnessStatus.UNKNOWN, None, None, "no confirmation time")
    if confirmed_through.tzinfo is None or evidence_clock.tzinfo is None:
        return Freshness(FreshnessStatus.UNKNOWN, None, None, "timestamp without a time zone")
    age = evidence_clock - confirmed_through
    if age < timedelta(0):
        return Freshness(FreshnessStatus.UNKNOWN, age, None, "confirmation is later than the evidence clock")
    limit = MAX_AGE[source]
    until = fresh_until(source, confirmed_through)
    if limit is None:
        return Freshness(FreshnessStatus.FRESH, age, None, "identity-bound source does not age")
    if age <= limit:
        return Freshness(FreshnessStatus.FRESH, age, until, "within the maximum age")
    return Freshness(FreshnessStatus.STALE, age, until, f"older than the maximum age of {limit}")


def evaluate_record(record: Record, evidence_clock: datetime) -> Freshness:
    return evaluate(record.source, record.confirmed_through, evidence_clock)


def reconstructed_confirmation(
    cutoff: datetime, history_complete_through: datetime | None
) -> datetime | None:
    """``confirmed_through`` for a record rebuilt from event history.

    The cutoff is confirmed only when the event and edit history was fetched after the cutoff and
    paginated to the end, i.e. it is complete through the cutoff. Otherwise freshness is unknown.
    """
    if history_complete_through is None or history_complete_through < cutoff:
        return None
    return cutoff
