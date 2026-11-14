"""Synthetic sources: Kubernetes events, telemetry aggregates and team capacity.

These sources have no public counterpart for the reference project, so the data is written by
the team and every record is marked ``provenance: synthetic`` with ``synthetic_ancestry: true``.
Timestamps are placed inside each source's maximum age relative to the evidence clock, so the
freshness rules are exercised rather than bypassed. Capacity data carries role ids and skill tags
only, never names.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from atlas.contracts import Provenance, Record
from atlas.store import AUTHORITATIVE_WRITERS

SYNTHETIC_ROOT = Path(__file__).resolve().parents[2] / "data" / "snapshots" / "synthetic"
FORBIDDEN_CAPACITY_KEYS = {"name", "names", "email", "login", "handle", "person", "rating", "performance"}


def _record(source: str, source_id: str, view: dict[str, Any], t: datetime, scopes: frozenset[str]) -> Record:
    version = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()[:16]
    return Record(
        record_id=f"{source}:{source_id}@{version}",
        source=source,
        source_id=source_id,
        revision=None,
        content_version_id=version,
        valid_from=t,
        observed_at=t,
        confirmed_through=t,
        writer=AUTHORITATIVE_WRITERS[source],
        provenance=Provenance.SYNTHETIC,
        synthetic_ancestry=True,
        access_scopes=scopes,
        lineage=(),
        content_ref=f"db:{source}:{source_id}@{version}",
        fetched_at=t,
    )


def kubernetes_records(case_id: str, clock: datetime, root: Path = SYNTHETIC_ROOT) -> list[tuple[Record, dict[str, Any], str]]:
    path = root / "k8s" / f"{case_id}.json"
    if not path.exists():
        return []
    out = []
    for i, ev in enumerate(json.loads(path.read_text(encoding="utf-8"))["events"]):
        t = clock - timedelta(minutes=int(ev.get("minutes_before", 10)))
        view = {k: v for k, v in ev.items() if k != "minutes_before"}
        view.update({"at": t.isoformat(), "synthetic": True})
        out.append((_record("kubernetes", f"k8s_event/{case_id}/{i}", view, t, frozenset({"public"})), view, "k8s_event"))
    return out


def telemetry_records(case_id: str, clock: datetime, root: Path = SYNTHETIC_ROOT) -> list[tuple[Record, dict[str, Any], str]]:
    path = root / "telemetry" / f"{case_id}.json"
    if not path.exists():
        return []
    out = []
    for series in json.loads(path.read_text(encoding="utf-8"))["series"]:
        end = clock - timedelta(minutes=int(series.get("minutes_before", 5)))
        view = {k: v for k, v in series.items() if k != "minutes_before"}
        view.update({"window_end": end.isoformat(), "aggregate_only": True, "synthetic": True})
        out.append((_record("telemetry", f"series/{case_id}/{series['metric']}", view, end, frozenset({"public"})), view, "telemetry_series"))
    return out


def capacity_records(clock: datetime, root: Path = SYNTHETIC_ROOT) -> list[tuple[Record, dict[str, Any], str]]:
    data = json.loads((root / "capacity.json").read_text(encoding="utf-8"))
    t = clock - timedelta(days=int(data.get("days_before", 2)))
    out = []
    for role in data["roles"]:
        bad = FORBIDDEN_CAPACITY_KEYS & set(role)
        if bad:
            raise ValueError(f"capacity data must not carry personal fields: {sorted(bad)}")
        view = dict(role, synthetic=True)
        out.append((_record("capacity", f"role/{role['role_id']}", view, t, frozenset({"staffing"})), view, "capacity_role"))
    return out


def all_synthetic(case_id: str, clock: datetime, root: Path = SYNTHETIC_ROOT) -> Iterable[tuple[Record, dict[str, Any], str]]:
    yield from kubernetes_records(case_id, clock, root)
    yield from telemetry_records(case_id, clock, root)
    yield from capacity_records(clock, root)
