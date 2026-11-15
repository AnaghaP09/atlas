"""Dependency scans as evidence the security gate can read.

A scan-run record is written even when nothing is found, so a clean scan passes and a missing scan
stays "not run". For a data set rebuilt after the fact, advisories published after the cut-off are
left out.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from atlas.connectors.dependency_scan import (ROOT_COMPONENT, advisory_severity, findings_as_of, gate_scan_records,
                                              panel_items, query_advisories)
from atlas.contracts import EgressPurpose, RunMode, SecurityState
from atlas.gateway import Gateway, RequestContext, Response
from atlas.security_gate import Finding, blocks, evaluate, parse_security_records

CUTOFF = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
INVENTORY = [("example.org/lib", "v1.2.0"), ("example.org/other", "v0.4.1")]
DETAILS = {
    "GHSA-aaaa": {"id": "GHSA-aaaa", "aliases": ["GO-2026-0001"], "published": "2026-05-01T00:00:00Z",
                  "summary": "Parser reads past the buffer", "database_specific": {"severity": "HIGH"},
                  "affected": [{"package": {"name": "example.org/lib"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.2.5"}]}]}]},
    "GO-2026-0001": {"id": "GO-2026-0001", "aliases": ["GHSA-aaaa"], "published": "2026-05-03T00:00:00Z"},
    "GHSA-late": {"id": "GHSA-late", "published": "2026-09-01T00:00:00Z", "database_specific": {"severity": "CRITICAL"}},
    "GHSA-low": {"id": "GHSA-low", "published": "2026-01-10T00:00:00Z", "database_specific": {"severity": "LOW"}},
}
BY_MODULE = {"example.org/lib": ["GHSA-aaaa", "GO-2026-0001", "GHSA-late"], "example.org/other": ["GHSA-low"]}


def gate(records, now=CUTOFF):
    scans, findings = parse_security_records([r for r, _, _ in records], lambda rec: json.dumps(next(c for r, c, _ in records if r is rec)))
    return evaluate([ROOT_COMPONENT], scans, findings, [], "policy-1", now)


def test_aliases_become_one_finding_and_later_advisories_are_left_out():
    findings, excluded = findings_as_of(BY_MODULE, DETAILS, dict(INVENTORY), CUTOFF)
    assert excluded == 1
    by_module = {f["module"]: f for f in findings}
    lib = by_module["example.org/lib"]
    assert lib["advisory_ids"] == ["GHSA-aaaa", "GO-2026-0001"]
    assert lib["severity"] == "HIGH" and lib["fix_version"] == "1.2.5" and lib["version"] == "v1.2.0"
    assert by_module["example.org/other"]["severity"] == "LOW"
    assert len(findings) == 2


def test_without_a_cutoff_every_advisory_counts():
    findings, excluded = findings_as_of(BY_MODULE, DETAILS, dict(INVENTORY), None)
    assert excluded == 0 and len(findings) == 3


def test_severity_uses_the_highest_named_level_among_aliases():
    assert advisory_severity({}, [DETAILS["GHSA-aaaa"], DETAILS["GHSA-low"]]) == "HIGH"
    assert advisory_severity({}) is None


def test_clean_scan_passes_and_no_scan_is_not_run():
    clean = gate_scan_records(scope_id="set-1", revision="abc", inventory=INVENTORY, complete=True, findings=[],
                              at=CUTOFF, fetched_at=CUTOFF + timedelta(days=100), reconstructed=True)
    assert len(clean) == 1 and clean[0][2] == "scan_run" and clean[0][1]["finding_ids"] == []
    assert gate(clean).state is SecurityState.PASSED
    assert gate([]).state is SecurityState.NOT_RUN


def test_high_finding_with_a_fix_blocks_and_low_does_not():
    findings, excluded = findings_as_of(BY_MODULE, DETAILS, dict(INVENTORY), CUTOFF)
    records = gate_scan_records(scope_id="set-1", revision="abc", inventory=INVENTORY, complete=True, findings=findings,
                                at=CUTOFF, fetched_at=None, reconstructed=True, excluded_after_cutoff=excluded)
    result = gate(records)
    assert result.state is SecurityState.BLOCKED
    blocking = [f for f in result.findings if f["blocking"]]
    assert len(blocking) == 1 and len(result.findings) == 2
    run = records[0][1]
    assert run["reconstructed"] is True and run["advisories_excluded_after_cutoff"] == 1 and run["modules_scanned"] == 2
    low_only = gate_scan_records(scope_id="set-2", revision="abc", inventory=INVENTORY, complete=True,
                                 findings=[f for f in findings if f["severity"] == "LOW"], at=CUTOFF, fetched_at=None,
                                 reconstructed=True)
    assert gate(low_only).state is SecurityState.PASSED


def test_incomplete_scan_never_passes():
    records = gate_scan_records(scope_id="set-1", revision="abc", inventory=INVENTORY, complete=False, findings=[],
                                at=CUTOFF, fetched_at=None, reconstructed=False)
    assert records[0][1]["coverage"] == [] and gate(records).state is SecurityState.NOT_RUN


def test_scan_records_are_restricted_to_the_security_scope_and_stand_at_the_cutoff():
    records = gate_scan_records(scope_id="set-1", revision="abc", inventory=INVENTORY, complete=True, findings=[],
                                at=CUTOFF, fetched_at=CUTOFF + timedelta(days=30), reconstructed=True)
    rec = records[0][0]
    assert rec.access_scopes == frozenset({"security"})
    assert rec.valid_from == rec.confirmed_through == CUTOFF and rec.fetched_at > CUTOFF


def test_live_scan_older_than_a_day_stops_counting():
    records = gate_scan_records(scope_id="live:x", revision="abc", inventory=INVENTORY, complete=True, findings=[],
                                at=CUTOFF, fetched_at=CUTOFF, reconstructed=False)
    assert gate(records, CUTOFF + timedelta(hours=23)).state is SecurityState.PASSED
    assert gate(records, CUTOFF + timedelta(hours=25)).state is SecurityState.NOT_RUN


def test_query_advisories_reports_incomplete_when_a_batch_fails():
    calls = []

    def transport(method, url, headers, body):
        calls.append((method, url))
        if url.endswith("/v1/querybatch"):
            return Response(200, {"results": [{"vulns": [{"id": "GHSA-aaaa"}]}, {}]}, {})
        return Response(200, DETAILS["GHSA-aaaa"], {})

    gateway = Gateway(transport, clock=lambda: CUTOFF)
    ctx = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
    by_module, details, complete = query_advisories(gateway, ctx, INVENTORY)
    assert complete and by_module == {"example.org/lib": ["GHSA-aaaa"]} and "GHSA-aaaa" in details

    def failing(method, url, headers, body):
        return Response(200, {"results": []}, {})

    _, _, complete = query_advisories(Gateway(failing, clock=lambda: CUTOFF), ctx, INVENTORY)
    assert complete is False


def test_panel_rows_put_blocking_findings_first():
    findings, _ = findings_as_of(BY_MODULE, DETAILS, dict(INVENTORY), None)
    for n, f in enumerate(findings):
        f["finding_id"] = f"finding-{n}"
    rows = panel_items(findings, modules=2, at=CUTOFF,
                       blocking=lambda f: blocks(Finding(f["finding_id"], "", f.get("severity"), f.get("fix_version"))))
    assert "2 modules scanned" in rows[0]["note"] and "3 finding(s)" in rows[0]["note"]
    assert rows[1]["blocking"] is True and rows[-1]["severity"] == "LOW"
