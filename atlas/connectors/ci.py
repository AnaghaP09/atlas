"""CI connector: job artefacts (build log and result) from the project's configured artefact store.

Logs are capped at 50,000 lines, keeping the last lines and a cut marker.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from atlas.connectors import ReplayConnector
from atlas.contracts import EgressPurpose, Provenance, Record, RunContext, RunMode
from atlas.gateway import Gateway, GatewayRefused, RequestContext
from atlas.store import AUTHORITATIVE_WRITERS, EvidenceStore

LOG_CAP = 50_000
CUT_MARKER = "[log truncated: only the last {kept} of {total} lines are kept]"


def cap_log(text: str, cap: int = LOG_CAP) -> tuple[str, int, bool]:
    lines = text.splitlines()
    if len(lines) <= cap:
        return "\n".join(lines), len(lines), False
    kept = lines[-cap:]
    return CUT_MARKER.format(kept=cap, total=len(lines)) + "\n" + "\n".join(kept), len(lines), True


def job_view(prow_path: str, log_text: str | None, finished: Mapping[str, Any] | None, *, total_lines: int | None = None, truncated: bool | None = None) -> dict[str, Any]:
    parts = prow_path.strip("/").split("/")
    job_name = parts[-2] if len(parts) >= 2 else prow_path
    build_id = parts[-1]
    view: dict[str, Any] = {
        "job": job_name,
        "build_id": build_id,
        "path": prow_path,
        "result": (finished or {}).get("result"),
        "finished_at": (finished or {}).get("timestamp"),
        "log_available": log_text is not None,
    }
    if log_text is not None:
        capped, total, cut = cap_log(log_text)
        view["log"] = capped
        view["line_count"] = total_lines if total_lines is not None else total
        view["truncated"] = truncated if truncated is not None else cut
    return view


def job_record(view: Mapping[str, Any], *, valid_from: datetime, cutoff: datetime, fetched_at: datetime | None) -> Record:
    version = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()[:16]
    source_id = f"job/{view['job']}/{view['build_id']}"
    return Record(
        record_id=f"ci:{source_id}@{version}",
        source="ci",
        source_id=source_id,
        revision=None,
        content_version_id=version,
        valid_from=valid_from,
        observed_at=valid_from,
        confirmed_through=cutoff,
        writer=AUTHORITATIVE_WRITERS["ci"],
        provenance=Provenance.OBSERVED,
        synthetic_ancestry=False,
        access_scopes=frozenset({"public"}),
        lineage=(),
        content_ref=f"db:ci:{source_id}@{version}",
        fetched_at=fetched_at,
    )


class CIConnector(ReplayConnector):
    name = "ci"
    source = "ci"

    def __init__(self, store: EvidenceStore, gateway: Gateway | None = None, *, artifact_base: str | None = None, **kw: Any) -> None:
        super().__init__(store, **kw)
        self.gateway = gateway
        self.artifact_base = artifact_base.rstrip("/") if artifact_base else None

    def fetch_live(self, query: Mapping[str, Any], ctx: RunContext) -> list[Record]:
        if self.gateway is None:
            raise RuntimeError("CI connector has no gateway")
        if not self.artifact_base:
            raise RuntimeError("CI artefact base is not configured for this project (sources.ci.artifact_base)")
        path = str(query["prow_path"]).strip("/")
        rctx = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
        base = f"{self.artifact_base}/{path}"
        try:
            log = self.gateway.request("ci", "GET", f"{base}/build-log.txt", rctx)
            text = log.body if log.status == 200 and isinstance(log.body, str) else None
        except GatewayRefused:
            text = None
        try:
            fin = self.gateway.request("ci", "GET", f"{base}/finished.json", rctx)
            finished = fin.body if fin.status == 200 and isinstance(fin.body, dict) else None
        except GatewayRefused:
            finished = None
        now = self.clock()
        view = job_view(path, text, finished)
        record = job_record(view, valid_from=now, cutoff=now, fetched_at=now)
        self.store.put(record, AUTHORITATIVE_WRITERS["ci"], content=view, entity_type="ci_job", freshness_basis="per_run")
        return [record]
