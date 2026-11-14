"""Helpers for data-layer tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from atlas.contracts import Provenance, Record, RunContext, RunMode
from atlas.store import AUTHORITATIVE_WRITERS

T0 = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)


def rec(
    source: str = "issues",
    source_id: str = "issue/1",
    version: str = "v1",
    *,
    revision: int | str | None = None,
    valid_from: datetime | None = None,
    confirmed_through: datetime | None = None,
    scopes: frozenset[str] = frozenset({"public"}),
    lineage: tuple[str, ...] = (),
    provenance: Provenance = Provenance.OBSERVED,
    synthetic: bool = False,
    writer: str | None = None,
    record_id: str | None = None,
) -> Record:
    valid = valid_from or T0 - timedelta(days=1)
    return Record(
        record_id=record_id or f"{source}:{source_id}:{version}",
        source=source,
        source_id=source_id,
        revision=revision,
        content_version_id=version,
        valid_from=valid,
        observed_at=valid,
        confirmed_through=confirmed_through,
        writer=writer or AUTHORITATIVE_WRITERS[source],
        provenance=provenance,
        synthetic_ancestry=synthetic,
        access_scopes=scopes,
        lineage=lineage,
        content_ref=f"db:{record_id or source + ':' + source_id + ':' + version}",
    )


def ctx(snapshot_id: str, *, cutoff: datetime = T0, mode: RunMode = RunMode.REPLAY, alias: str | None = None, run_id: str = "run-1") -> RunContext:
    return RunContext(
        run_id=run_id,
        snapshot_id=snapshot_id,
        mode=mode,
        release_or_case="case",
        cutoff=cutoff,
        alias_variant=alias,
        evidence_clock=cutoff,
    )


def content(**kwargs: Any) -> dict[str, Any]:
    return dict(kwargs)
