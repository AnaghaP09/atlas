import json
from datetime import datetime, timedelta, timezone

import pytest

from atlas.connectors.ci import CUT_MARKER, cap_log, job_view
from atlas.connectors.security import SecurityConnector, parse_go_mod, run_scan
from atlas.connectors.synthetic import FORBIDDEN_CAPACITY_KEYS, all_synthetic, capacity_records
from atlas.connectors.target_ci import PollBusy, TargetCIAdapter
from atlas.contracts import EgressPurpose, Provenance, SecurityContext, TaskState, ValidationContext
from atlas.gateway import Gateway, GatewayRefused, PublicationRecord, RequestContext, Response
from atlas.store import ActionStore, WriterRejected

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)

GO_MOD = """module kubevirt.io/kubevirt

go 1.24

require (
\tgolang.org/x/net v0.30.0
\tgithub.com/example/lib v1.2.3 // indirect
)

require k8s.io/api v0.33.0
"""


def test_parse_go_mod():
    assert parse_go_mod(GO_MOD) == [("github.com/example/lib", "v1.2.3"), ("golang.org/x/net", "v0.30.0"), ("k8s.io/api", "v0.33.0")]


class OSVFake:
    def __init__(self, vulns=None, fail_batch=False, results_len=None):
        self.vulns = vulns or {}
        self.fail_batch = fail_batch
        self.results_len = results_len

    def __call__(self, method, url, headers, body):
        if url.endswith("/v1/querybatch"):
            if self.fail_batch:
                return Response(500, None)
            results = [{"vulns": [{"id": v} for v in self.vulns.get(q["package"]["name"], [])]} for q in body["queries"]]
            if self.results_len is not None:
                results = results[: self.results_len]
            return Response(200, {"results": results})
        vid = url.rsplit("/", 1)[1]
        return Response(200, {"id": vid, "database_specific": {"severity": "HIGH"},
                              "affected": [{"package": {"name": "golang.org/x/net"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "0.33.0"}]}]}]})


INV = [("golang.org/x/net", "v0.30.0"), ("k8s.io/api", "v0.33.0")]


def scan(fake):
    g = Gateway(fake)
    return run_scan(g, RequestContext(purpose=EgressPurpose.LIVE_INGEST), INV, destination="kubevirt/kubevirt", revision="abc",
                    action_id="investigation", clock=lambda: NOW)


def test_clean_scan_is_a_complete_record_with_no_findings():
    run, findings = scan(OSVFake())
    assert run["completion"] == "complete" and run["coverage_ok"] == 2 and findings == []
    assert run["fresh_until"] == NOW + timedelta(hours=24)


def test_scan_with_findings():
    run, findings = scan(OSVFake({"golang.org/x/net": ["GO-2025-0001"]}))
    assert run["completion"] == "complete"
    assert findings[0]["osv_id"] == "GO-2025-0001" and findings[0]["fixed_version"] == "0.33.0" and findings[0]["severity"] == "HIGH"


def test_failed_and_partial_scans():
    run, _ = scan(OSVFake(fail_batch=True))
    assert run["completion"] == "failed" and set(run["per_module"].values()) == {"failed"}
    run2, _ = scan(OSVFake(results_len=1))
    assert run2["completion"] == "failed"


def test_task_scan_writes_only_to_its_partition_and_only_gate_reads(tmp_path):
    store = ActionStore(tmp_path / "a.sqlite")
    conn = SecurityConnector(Gateway(OSVFake()), action_store=store, clock=lambda: NOW)
    ctx = SecurityContext("AnaghaP09/atlas-sandbox-target", "act-1", "base123")
    conn.scan_for_task(ctx, INV)
    runs = store.scan_runs(ctx, "security_gate")
    assert len(runs) == 1 and runs[0]["completion"] == "complete" and runs[0]["findings"] == []
    with pytest.raises(PermissionError):
        store.scan_runs(ctx, "investigator")
    other = SecurityContext("AnaghaP09/atlas-sandbox-target", "act-2", "base123")
    assert store.scan_runs(other, "security_gate") == []
    with pytest.raises(WriterRejected):
        store.write_scan_run(ctx, "execution", {"destination": "x"}, [])


def test_task_scan_refused_in_frozen_runs():
    from atlas.gateway import policy_check

    rctx = RequestContext(purpose=EgressPurpose.ACTION_SECURITY_INGEST, frozen_evaluation=True, action_security=("d", "a", "r"))
    assert policy_check("security", "POST", "https://api.osv.dev/v1/querybatch", {"queries": []}, rctx) is not None


# --------------------------------------------------------------------- target CI

DEST = "AnaghaP09/atlas-sandbox-target"
COMMIT = "abcdef1234567890abcdef1234567890abcdef12"


class CIServer:
    def __init__(self):
        self.job = {"id": 11, "run_attempt": 1, "name": "test", "status": "queued", "conclusion": None,
                    "started_at": None, "completed_at": None, "head_sha": COMMIT}

    def __call__(self, method, url, headers, body):
        if "/actions/runs?" in url:
            return Response(200, {"workflow_runs": [{"id": 5, "head_sha": COMMIT, "run_attempt": 1}]})
        if url.endswith("/actions/runs/5/jobs"):
            return Response(200, {"jobs": [dict(self.job)]})
        if url.endswith("/check-runs"):
            return Response(200, {"check_runs": []})
        return Response(404, None)


def pub(state=TaskState.AWAITING_VALIDATION):
    return PublicationRecord(DEST, "act-1", COMMIT, 7, state)


def test_pending_to_completed_duplicate_and_states(tmp_path):
    server = CIServer()
    store = ActionStore(tmp_path / "a.sqlite")
    adapter = TargetCIAdapter(Gateway(server), store, clock=lambda: NOW)
    assert adapter.poll(pub()) == 1
    assert adapter.poll(pub()) == 0  # identical observation is a no-op
    server.job.update(status="in_progress", started_at="2026-11-14T12:01:00Z")
    assert adapter.poll(pub()) == 1
    server.job.update(status="completed", conclusion="success", completed_at="2026-11-14T12:05:00Z")
    assert adapter.poll(pub()) == 1
    vctx = ValidationContext(DEST, "act-1", COMMIT)
    eff = store.effective_states(vctx, "execution")
    assert eff[("11", 1)]["status"] == "completed" and eff[("11", 1)]["conclusion"] == "success"
    assert len(store.ci_observations(vctx, "execution")) == 3


@pytest.mark.parametrize("state", [TaskState.ACCEPTED, TaskState.FAILED, TaskState.PUBLISHED])
def test_polling_after_acceptance_or_approval_expiry(tmp_path, state):
    adapter = TargetCIAdapter(Gateway(CIServer()), ActionStore(tmp_path / "a.sqlite"), clock=lambda: NOW)
    assert adapter.poll(pub(state)) == 1


def test_out_of_order_observations_keep_completed(tmp_path):
    store = ActionStore(tmp_path / "a.sqlite")
    vctx = ValidationContext(DEST, "act-1", COMMIT)
    completed = {"job_id": 11, "run_attempt": 1, "status": "completed", "conclusion": "failure", "started_at": "2026-11-14T12:01:00Z", "completed_at": "2026-11-14T12:05:00Z", "head_sha": COMMIT}
    pending = {"job_id": 11, "run_attempt": 1, "status": "in_progress", "conclusion": None, "started_at": None, "completed_at": None, "head_sha": COMMIT}
    store.write_ci_observation(vctx, "connector.target_ci", completed, NOW)
    store.write_ci_observation(vctx, "connector.target_ci", pending, NOW)  # late, lower rank, null started_at
    store.write_ci_observation(vctx, "connector.target_ci", completed, NOW)  # duplicate
    eff = store.effective_states(vctx, "execution")[("11", 1)]
    assert eff["status"] == "completed" and eff["conclusion"] == "failure"
    rerun = dict(completed, run_attempt=2, conclusion="success")
    store.write_ci_observation(vctx, "connector.target_ci", rerun, NOW)
    assert store.effective_states(vctx, "execution")[("11", 2)]["conclusion"] == "success"


def test_observation_for_other_commit_rejected_and_writer_enforced(tmp_path):
    store = ActionStore(tmp_path / "a.sqlite")
    vctx = ValidationContext(DEST, "act-1", COMMIT)
    with pytest.raises(WriterRejected):
        store.write_ci_observation(vctx, "connector.target_ci", {"job_id": 1, "head_sha": "ffff" * 10, "status": "queued"}, NOW)
    with pytest.raises(WriterRejected):
        store.write_ci_observation(vctx, "execution", {"job_id": 1, "status": "queued"}, NOW)


def test_concurrent_poll_blocked_by_lease(tmp_path):
    store = ActionStore(tmp_path / "a.sqlite")
    vctx = ValidationContext(DEST, "act-1", COMMIT)
    assert store.acquire_poll(vctx, "other")
    with pytest.raises(PollBusy):
        TargetCIAdapter(Gateway(CIServer()), store, clock=lambda: NOW).poll(pub())


def test_other_repository_or_missing_publication_refused(tmp_path):
    g = Gateway(CIServer())
    with pytest.raises(GatewayRefused):
        g.request("target_ci", "GET", "https://api.github.com/repos/other/repo/actions/runs/5/jobs",
                  RequestContext(purpose=EgressPurpose.TARGET_CI_READ, publication=pub()))
    with pytest.raises(GatewayRefused):
        g.request("target_ci", "GET", f"https://api.github.com/repos/{DEST}/actions/runs/5/jobs",
                  RequestContext(purpose=EgressPurpose.TARGET_CI_READ))


def test_validation_evidence_kinds_and_no_lifecycle_state(tmp_path):
    store = ActionStore(tmp_path / "a.sqlite")
    vctx = ValidationContext(DEST, "act-1", COMMIT)
    store.write_evidence(vctx, "execution", "behavioural_check", {"test": "TestJoinHostPortIPv6", "passed": True}, NOW)
    store.write_evidence(vctx, "execution", "human_adjudication", {"role": "em", "decision": "correct", "reason": "fix verified"}, NOW)
    with pytest.raises(ValueError):
        store.write_evidence(vctx, "execution", "human_adjudication", {"state": "accepted"}, NOW)
    with pytest.raises(WriterRejected):
        store.write_evidence(vctx, "connector.target_ci", "check_result", {"x": 1}, NOW)
    assert not store.has_lifecycle_state()
    assert [e["kind"] for e in store.evidence(vctx, "scorecard")] == ["behavioural_check", "human_adjudication"]
    with pytest.raises(PermissionError):
        store.evidence(vctx, "investigator")


# --------------------------------------------------------------------- CI logs and synthetic data


def test_log_cap_keeps_last_lines_with_marker():
    text = "\n".join(f"line {i}" for i in range(60_010))
    capped, total, cut = cap_log(text)
    assert cut and total == 60_010
    lines = capped.splitlines()
    assert lines[0] == CUT_MARKER.format(kept=50_000, total=60_010) and lines[-1] == "line 60009" and len(lines) == 50_001
    view = job_view("pr-logs/pull/kubevirt_kubevirt/1/pull-job/123", "ok", {"result": "FAILURE"})
    assert view["job"] == "pull-job" and view["build_id"] == "123" and view["result"] == "FAILURE"


def test_synthetic_records_are_flagged_fresh_and_nameless():
    rows = list(all_synthetic("G06", NOW))
    assert rows and all(r.provenance is Provenance.SYNTHETIC and r.synthetic_ancestry for r, _, _ in rows)
    from atlas.freshness import evaluate

    assert all(evaluate(r.source, r.confirmed_through, NOW).usable for r, _, _ in rows)
    for r, view, _ in capacity_records(NOW):
        assert not (FORBIDDEN_CAPACITY_KEYS & set(view))
        assert r.access_scopes == frozenset({"staffing"})


def test_capacity_with_names_rejected(tmp_path):
    (tmp_path / "capacity.json").write_text(json.dumps({"roles": [{"role_id": "r", "name": "someone"}]}))
    with pytest.raises(ValueError):
        capacity_records(NOW, tmp_path)
