"""Dependency scans as gate-readable evidence, for prepared data sets and for live projects.

The security gate reads scan-run and finding records. This module asks OSV.dev about every Go
module in a project's ``go.mod`` and writes those records. A scan-run is written even when nothing
was found, so a clean scan and a missing scan are always different.

For a data set rebuilt after the fact, only advisories published on or before the cut-off count.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping, Sequence

from atlas.connectors.security import BATCH, OSV, _fixed
from atlas.contracts import Provenance, Record
from atlas.gateway import Gateway, GatewayRefused, RequestContext, SourceUnavailable
from atlas.store import AUTHORITATIVE_WRITERS

ROOT_COMPONENT = "project-dependencies"
AS_OF_RULE = "only advisories published on or before the cut-off are counted"
_SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "MEDIUM": 2, "LOW": 1}


def advisory_severity(detail: Mapping[str, Any], related: Sequence[Mapping[str, Any]] = ()) -> str | None:
    """The advisory's named severity, or the highest named severity among its aliases."""
    best: str | None = None
    for d in (detail, *related):
        sev = (d.get("database_specific") or {}).get("severity")
        if sev and _SEVERITY_ORDER.get(str(sev).upper(), 0) > _SEVERITY_ORDER.get(best or "", 0):
            best = str(sev).upper()
    return best


def _published(d: Mapping[str, Any]) -> datetime | None:
    value = d.get("published")
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def findings_as_of(vulns_by_module: Mapping[str, Sequence[str]], details: Mapping[str, Mapping[str, Any]],
                   versions: Mapping[str, str], cutoff: datetime | None) -> tuple[list[dict[str, Any]], int]:
    """One finding per (advisory group, module). Advisories published after ``cutoff`` are left out.

    Advisories that are aliases of each other (the same flaw listed by two databases) become one
    finding. Returns ``(findings, excluded_after_cutoff)``.
    """
    findings: list[dict[str, Any]] = []
    excluded = 0
    for module in sorted(vulns_by_module):
        seen: set[str] = set()
        ids = set(vulns_by_module[module])
        for vid in sorted(ids):
            if vid in seen:
                continue
            detail = details.get(vid) or {}
            group = {vid} | {a for a in detail.get("aliases") or [] if a in ids}
            seen |= group
            related = [details[a] for a in sorted(group) if a != vid and a in details]
            dates = [p for p in (_published(d) for d in (detail, *related)) if p is not None]
            published = min(dates) if dates else None
            if cutoff is not None and (published is None or published > cutoff):
                excluded += 1
                continue
            fixed = _fixed(detail, module) or next((f for f in (_fixed(d, module) for d in related) if f), None)
            summary = detail.get("summary") or next((d.get("summary") for d in related if d.get("summary")), None)
            findings.append({
                "advisory_ids": sorted(group), "module": module, "version": versions.get(module),
                "severity": advisory_severity(detail, related), "fix_version": fixed,
                "published": published.isoformat() if published else None, "summary": summary,
            })
    return findings, excluded


def gate_scan_records(*, scope_id: str, revision: str, inventory: Sequence[tuple[str, str]], complete: bool,
                      findings: Sequence[Mapping[str, Any]], at: datetime, fetched_at: datetime | None,
                      reconstructed: bool, excluded_after_cutoff: int = 0,
                      ) -> list[tuple[Record, dict[str, Any], str]]:
    """A scan-run record and its finding records in the form the security gate reads.

    ``at`` is the time the result holds for: the cut-off for a data set rebuilt after the fact
    (``reconstructed``), the scan time for a live project.
    """
    key = hashlib.sha256(f"{scope_id}|{revision}|{at.isoformat()}".encode()).hexdigest()[:16]
    scan_run_id = f"scan-{key}"
    out_findings: list[tuple[str, dict[str, Any]]] = []
    for f in findings:
        digest = hashlib.sha256(("|".join(f["advisory_ids"]) + "|" + f["module"]).encode()).hexdigest()[:10]
        fid = f"finding-{key}-{digest}"
        content = {"kind": "finding", "finding_id": fid, "component": ROOT_COMPONENT, "scan_run_id": scan_run_id}
        content.update({k: f.get(k) for k in ("advisory_ids", "module", "version", "severity", "fix_version", "published", "summary")})
        out_findings.append((fid, content))
    run: dict[str, Any] = {
        "kind": "scan_run", "scan_run_id": scan_run_id, "status": "success" if complete else "partial_failure",
        "coverage": [ROOT_COMPONENT] if complete else [], "finding_ids": [fid for fid, _ in out_findings],
        "target_revision": revision, "modules_scanned": len(inventory),
        "scanner": "OSV.dev query over the Go module list", "reconstructed": reconstructed, "as_of": at.isoformat(),
    }
    if reconstructed:
        run.update(as_of_rule=AS_OF_RULE, advisories_excluded_after_cutoff=excluded_after_cutoff)
    records: list[tuple[Record, dict[str, Any], str]] = []
    sid = f"scan_run/{scan_run_id}"
    version = hashlib.sha256(json.dumps(run, sort_keys=True).encode()).hexdigest()[:16]
    records.append((_record(sid, version, at, fetched_at), run, "scan_run"))
    for fid, content in out_findings:
        fv = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]
        records.append((_record(f"finding/{fid}", fv, at, fetched_at, lineage=(f"security:{sid}@{version}",)), content, "finding"))
    return records


def _record(source_id: str, version: str, at: datetime, fetched_at: datetime | None, lineage: tuple[str, ...] = ()) -> Record:
    return Record(
        record_id=f"security:{source_id}@{version}", source="security", source_id=source_id, revision=None,
        content_version_id=version, valid_from=at, observed_at=at, confirmed_through=at,
        writer=AUTHORITATIVE_WRITERS["security"], provenance=Provenance.OBSERVED, synthetic_ancestry=False,
        access_scopes=frozenset({"security"}), lineage=lineage,
        content_ref=f"db:security:{source_id}@{version}", fetched_at=fetched_at,
    )


def query_advisories(gateway: Gateway, rctx: RequestContext, inventory: Sequence[tuple[str, str]],
                     known: Mapping[str, Mapping[str, Any]] | None = None,
                     ) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]], bool]:
    """Ask OSV about every module. Returns (advisory ids per module, advisory details, complete)."""
    by_module: dict[str, list[str]] = {}
    complete = True
    for start in range(0, len(inventory), BATCH):
        chunk = inventory[start:start + BATCH]
        body = {"queries": [{"package": {"name": m, "ecosystem": "Go"}, "version": v.lstrip("v")} for m, v in chunk]}
        try:
            resp = gateway.request("security", "POST", f"{OSV}/v1/querybatch", rctx, body)
        except (GatewayRefused, SourceUnavailable):
            complete = False
            continue
        results = (resp.body or {}).get("results") if resp.status == 200 and isinstance(resp.body, dict) else None
        if not results or len(results) != len(chunk):
            complete = False
            continue
        for (m, _), res in zip(chunk, results):
            ids = [v["id"] for v in (res or {}).get("vulns") or []]
            if ids:
                by_module[m] = ids
    details: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (known or {}).items()}
    for vid in sorted({v for ids in by_module.values() for v in ids}):
        if vid in details:
            continue
        try:
            resp = gateway.request("security", "GET", f"{OSV}/v1/vulns/{vid}", rctx)
        except (GatewayRefused, SourceUnavailable):
            complete = False
            continue
        if resp.status == 200 and isinstance(resp.body, dict):
            details[vid] = resp.body
        else:
            complete = False
    return by_module, details, complete


def panel_items(findings: Sequence[Mapping[str, Any]], *, modules: int, at: datetime, blocking: Any) -> list[dict[str, Any]]:
    """Rows for the dashboard security panel: a summary line, then one row per finding."""
    rows: list[dict[str, Any]] = []
    for f in findings:
        rows.append({"finding_id": f.get("finding_id"), "advisory": (f.get("advisory_ids") or [None])[0], "module": f.get("module"),
                     "version": f.get("version"), "severity": f.get("severity"), "fix_version": f.get("fix_version"),
                     "blocking": bool(blocking(f))})
    rows.sort(key=lambda r: (not r["blocking"], -_SEVERITY_ORDER.get(str(r["severity"] or "").upper(), 0), str(r["module"])))
    note = (f"{modules} modules scanned at {at.isoformat(timespec='minutes')}: {len(rows)} finding(s), "
            f"{sum(1 for r in rows if r['blocking'])} blocking")
    return [{"note": note}] + rows
