"""Connectors: one per evidence source.

Every connector has two modes:

* **replay** (any run bound to a frozen or prepared snapshot): reads only the run's snapshot;
* **live** (an admitted live-ingestion run): fetches through the permission gateway and writes to
  the live store as the source's single authorised writer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from atlas.contracts import Record, RunContext, RunMode
from atlas.store import LIVE_SNAPSHOT_ID, EvidenceStore


class ReplayConnector:
    """Base class: replay reads come from the run's snapshot only."""

    name = "base"
    source = "derived"

    def __init__(self, store: EvidenceStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(self, query: Mapping[str, Any], ctx: RunContext) -> list[Record]:
        if ctx.mode is RunMode.LIVE and ctx.snapshot_id == LIVE_SNAPSHOT_ID and query.get("refresh"):
            return self.fetch_live(query, ctx)
        q = {k: v for k, v in query.items() if k in ("entity_type", "source_id", "text", "limit")}
        q["source"] = self.source
        return self.store.search(ctx, q)

    def fetch_live(self, query: Mapping[str, Any], ctx: RunContext) -> list[Record]:  # pragma: no cover - overridden
        raise NotImplementedError(f"{self.name} has no live mode")


__all__ = ["ReplayConnector"]
