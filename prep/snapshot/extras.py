"""Dependency scans and component churn for prepared data sets (trusted preparation only).

Both are rebuilt as of the data set's cut-off:

* the dependency scan uses the Go module list at the commit the data set stands for and counts
  only advisories published on or before the cut-off;
* component churn counts the commits that touched each component in the 120 days before the
  cut-off, on the history of that commit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from atlas.connectors.dependency_scan import findings_as_of, gate_scan_records
from atlas.connectors.security import module_inventory_from_go_mod
from atlas.contracts import Provenance, Record
from atlas.store import AUTHORITATIVE_WRITERS, EvidenceDB, from_iso
from prep.fetch.raw import RawCache

CHURN_WINDOW_DAYS = 120


def add_security_scan(db: EvidenceDB, cache: RawCache, *, key: str, ref: str, cutoff: datetime) -> dict[str, Any]:
    """Write the scan-run and finding records for the module list at ``ref`` as of ``cutoff``."""
    go_mod = cache.file_at("go.mod", ref)
    text = (go_mod.get("data") or {}).get("text")
    if not text:
        return {"security": "module list not available at this commit; no scan stored"}
    inventory = module_inventory_from_go_mod(text)
    scan = cache.osv_scan(key, inventory)
    data = scan["data"]
    findings, excluded = findings_as_of(data["by_module"], data["details"], dict(inventory), cutoff)
    records = gate_scan_records(scope_id=key, revision=ref, inventory=inventory, complete=bool(data["complete"]),
                                findings=findings, at=cutoff, fetched_at=from_iso(scan["fetched_at"]),
                                reconstructed=True, excluded_after_cutoff=excluded)
    for record, content, etype in records:
        db.put(record, AUTHORITATIVE_WRITERS["security"], content=content, entity_type=etype, freshness_basis="scan")
    return {"security_modules": len(inventory), "security_findings": len(findings),
            "security_excluded_after_cutoff": excluded, "security_complete": bool(data["complete"])}


def add_component_churn(db: EvidenceDB, cache: RawCache, *, key: str, ref: str, cutoff: datetime,
                        components: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One record per component: commits touching it in the window before the cut-off."""
    since = cutoff - timedelta(days=CHURN_WINDOW_DAYS)
    churn = cache.churn(key, ref, since.isoformat(), cutoff.isoformat(), sorted(components))["data"]
    stored = 0
    for component in sorted(components):
        view = {"kind": "component_churn", "component": component, "commits": int(churn.get(component, 0)),
                "window_days": CHURN_WINDOW_DAYS, "window_from": since.isoformat(), "window_to": cutoff.isoformat(),
                "revision": ref, "owner_team": components[component].get("owner_team")}
        version = hashlib.sha256(json.dumps(view, sort_keys=True).encode()).hexdigest()[:16]
        source_id = f"churn/{component}"
        at = cutoff - timedelta(seconds=1)
        record = Record(
            record_id=f"git:{source_id}@{version}", source="git", source_id=source_id, revision=None,
            content_version_id=version, valid_from=at, observed_at=at, confirmed_through=cutoff,
            writer=AUTHORITATIVE_WRITERS["git"], provenance=Provenance.DERIVED, synthetic_ancestry=False,
            access_scopes=frozenset({"public"}), lineage=(), content_ref=f"db:git:{source_id}@{version}",
        )
        db.put(record, AUTHORITATIVE_WRITERS["git"], content=view, entity_type="component_churn", freshness_basis="identity")
        stored += 1
    return {"churn_components": stored, "churn_commits_total": sum(int(v) for v in churn.values())}
