"""Security connector: OSV.dev queries over the Go modules in ``go.mod`` at a pinned commit.

Every scan writes one scan-run record whether or not it finds anything, so a clean scan and a
missing scan are always distinguishable. A scan-run is complete only when every module in the
inventory was queried successfully.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from atlas.contracts import EgressPurpose, Provenance, Record, RunMode, SecurityContext
from atlas.freshness import MAX_AGE
from atlas.gateway import Gateway, GatewayRefused, RequestContext, SourceUnavailable
from atlas.store import AUTHORITATIVE_WRITERS, SECURITY_PARTITION_WRITER, ActionStore, EvidenceStore

OSV = "https://api.osv.dev"
BATCH = 500


def parse_go_mod(text: str) -> list[tuple[str, str]]:
    """Return (module, version) for every requirement, including indirect ones."""
    modules: list[tuple[str, str]] = []
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if in_block:
            parts = line.split()
            if len(parts) >= 2:
                modules.append((parts[0], parts[1]))
        elif line.startswith("require "):
            parts = line[len("require "):].split()
            if len(parts) >= 2:
                modules.append((parts[0], parts[1]))
    seen: dict[str, str] = {}
    for m, v in modules:
        seen[m] = v
    return sorted(seen.items())


def _severity(vuln: Mapping[str, Any]) -> str | None:
    sev = (vuln.get("database_specific") or {}).get("severity")
    if sev:
        return str(sev).upper()
    for s in vuln.get("severity") or []:
        if s.get("score"):
            return s.get("type")
    return None


def _fixed(vuln: Mapping[str, Any], module: str) -> str | None:
    for aff in vuln.get("affected") or []:
        if (aff.get("package") or {}).get("name") != module:
            continue
        for rng in aff.get("ranges") or []:
            for ev in rng.get("events") or []:
                if "fixed" in ev:
                    return ev["fixed"]
    return None


def run_scan(
    gateway: Gateway,
    rctx: RequestContext,
    inventory: Sequence[tuple[str, str]],
    *,
    destination: str,
    revision: str,
    action_id: str,
    clock: Callable[[], datetime],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Query OSV for every module. Returns (scan_run, findings)."""
    per_module: dict[str, str] = {m: "not_queried" for m, _ in inventory}
    vuln_ids: dict[str, set[str]] = {}
    for start in range(0, len(inventory), BATCH):
        chunk = inventory[start:start + BATCH]
        body = {"queries": [{"package": {"name": m, "ecosystem": "Go"}, "version": v.lstrip("v")} for m, v in chunk]}
        try:
            resp = gateway.request("security", "POST", f"{OSV}/v1/querybatch", rctx, body)
        except (GatewayRefused, SourceUnavailable):
            for m, _ in chunk:
                per_module[m] = "failed"
            continue
        results = (resp.body or {}).get("results") if resp.status == 200 and isinstance(resp.body, dict) else None
        if not results or len(results) != len(chunk):
            for m, _ in chunk:
                per_module[m] = "failed"
            continue
        for (m, _), res in zip(chunk, results):
            per_module[m] = "ok"
            for v in (res or {}).get("vulns") or []:
                vuln_ids.setdefault(v["id"], set()).add(m)
    findings: list[dict[str, Any]] = []
    versions = dict(inventory)
    scan_time = clock()
    scan_key = hashlib.sha256(f"{destination}|{revision}|{action_id}|{scan_time.isoformat()}".encode()).hexdigest()[:16]
    scan_run_id = f"scan-{scan_key}"
    for vid, modules in sorted(vuln_ids.items()):
        detail: Mapping[str, Any] = {}
        try:
            resp = gateway.request("security", "GET", f"{OSV}/v1/vulns/{vid}", rctx)
            if resp.status == 200 and isinstance(resp.body, dict):
                detail = resp.body
        except (GatewayRefused, SourceUnavailable):
            detail = {}
        for m in sorted(modules):
            findings.append({
                "finding_id": f"finding-{scan_key}-{vid}-{hashlib.sha256(m.encode()).hexdigest()[:8]}",
                "osv_id": vid,
                "module": m,
                "version": versions.get(m),
                "severity": _severity(detail),
                "fixed_version": _fixed(detail, m),
                "published": detail.get("published"),
                "modified": detail.get("modified"),
                "summary": detail.get("summary"),
                "scan_run_id": scan_run_id,
            })
    ok = sum(1 for s in per_module.values() if s == "ok")
    total = len(per_module)
    completion = "complete" if ok == total and total > 0 else ("failed" if ok == 0 else "partial")
    scan_run = {
        "scan_run_id": scan_run_id,
        "destination": destination,
        "action_id": action_id,
        "revision": revision,
        "inventory": [{"module": m, "version": v} for m, v in inventory],
        "per_module": per_module,
        "completion": completion,
        "coverage_ok": ok,
        "coverage_total": total,
        "observed_at": scan_time,
        "confirmed_through": scan_time,
        "fresh_until": scan_time + MAX_AGE["security"],
    }
    return scan_run, findings


class SecurityConnector:
    """Writes investigation scans to the live evidence store and task scans to the action store."""

    name = "security"
    source = "security"

    def __init__(self, gateway: Gateway, *, store: EvidenceStore | None = None, action_store: ActionStore | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.gateway = gateway
        self.store = store
        self.action_store = action_store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def scan_for_investigation(self, inventory: Sequence[tuple[str, str]], *, repository: str, revision: str) -> list[Record]:
        rctx = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
        scan_run, findings = run_scan(self.gateway, rctx, inventory, destination=repository, revision=revision,
                                      action_id="investigation", clock=self.clock)
        records = scan_records(scan_run, findings)
        if self.store is not None:
            for rec, content, etype in records:
                self.store.put(rec, AUTHORITATIVE_WRITERS["security"], content=content, entity_type=etype, freshness_basis="scan")
        return [r for r, _, _ in records]

    def scan_for_task(self, ctx: SecurityContext, inventory: Sequence[tuple[str, str]]) -> str:
        """Initial scan at task creation, or a later refresh. Writes only to the task's partition."""
        if self.action_store is None:
            raise RuntimeError("no action store")
        rctx = RequestContext(purpose=EgressPurpose.ACTION_SECURITY_INGEST,
                              action_security=(ctx.destination_repo, ctx.action_id, ctx.scanned_revision))
        scan_run, findings = run_scan(self.gateway, rctx, inventory, destination=ctx.destination_repo,
                                      revision=ctx.scanned_revision, action_id=ctx.action_id, clock=self.clock)
        return self.action_store.write_scan_run(ctx, SECURITY_PARTITION_WRITER, scan_run, findings)


def scan_records(scan_run: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]) -> list[tuple[Record, dict[str, Any], str]]:
    """Evidence records for a scan-run and its findings (investigation snapshots)."""
    out: list[tuple[Record, dict[str, Any], str]] = []
    t = scan_run["observed_at"]
    content = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in scan_run.items()}
    content["finding_ids"] = [f["finding_id"] for f in findings]
    sid = f"scan_run/{scan_run['scan_run_id']}"
    version = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]
    out.append((_rec(sid, version, t), content, "scan_run"))
    for f in findings:
        fid = f"finding/{f['finding_id']}"
        fv = hashlib.sha256(json.dumps(f, sort_keys=True, default=str).encode()).hexdigest()[:16]
        out.append((_rec(fid, fv, t, lineage=(f"security:{sid}@{version}",)), dict(f), "finding"))
    return out


def _rec(source_id: str, version: str, t: datetime, lineage: tuple[str, ...] = ()) -> Record:
    return Record(
        record_id=f"security:{source_id}@{version}",
        source="security",
        source_id=source_id,
        revision=None,
        content_version_id=version,
        valid_from=t,
        observed_at=t,
        confirmed_through=t,
        writer=AUTHORITATIVE_WRITERS["security"],
        provenance=Provenance.OBSERVED,
        synthetic_ancestry=False,
        access_scopes=frozenset({"security"}),
        lineage=lineage,
        content_ref=f"db:security:{source_id}@{version}",
        fetched_at=t,
    )


def module_inventory_from_go_mod(text: str) -> list[tuple[str, str]]:
    return [(m, v) for m, v in parse_go_mod(text) if not re.match(r"^v0\.0\.0-0{14}", v)]
