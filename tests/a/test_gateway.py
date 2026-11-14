from datetime import datetime, timedelta, timezone

import pytest

from atlas.contracts import EgressPurpose, Mode, Role, RunMode, TaskState
from atlas.gateway import (
    CorrectionAuthorisation,
    CorrectionHeld,
    CorrectionJournal,
    Gateway,
    GatewayRefused,
    PublicationRecord,
    RequestContext,
    Response,
    SourceUnavailable,
    check_graphql,
    graphql_operations,
    sha256_text,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
LIVE = RequestContext(purpose=EgressPurpose.LIVE_INGEST)


class FakeTransport:
    def __init__(self, responder=None):
        self.calls = []
        self.responder = responder or (lambda m, u, h, b: Response(200, {"ok": True}))

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        return self.responder(method, url, headers, body)


def gw(transport=None, mode=Mode.CONNECTED, journal=None, clock=NOW, tokens=None):
    violations = []
    g = Gateway(
        transport or FakeTransport(),
        mode=lambda: mode,
        on_violation=lambda kind, detail: violations.append((kind, detail)),
        journal=journal,
        clock=lambda: clock,
        tokens=tokens,
    )
    return g, violations


# --------------------------------------------------------------------- GraphQL and OSV


def test_graphql_query_allowed_mutation_refused():
    ok = {"query": "query IssueSnapshot($n:Int!){ repository(owner:\"kubevirt\", name:\"kubevirt\"){ issue(number:$n){ title } } }", "variables": {"n": 1}}
    assert check_graphql(ok) is None
    assert "mutation" in check_graphql({"query": "mutation IssueSnapshot { addComment(input:{}) { clientMutationId } }"})
    assert "subscription" in check_graphql({"query": "subscription IssueSnapshot { x }"})
    assert check_graphql({"query": "query IssueSnapshot { a } mutation M { b }"}) is not None
    assert check_graphql({"query": "query Other { a }"}) == "GraphQL operation name is not on the allow-list"
    assert check_graphql({"query": "{ viewer { login } }"}) is not None  # anonymous query not allow-listed
    assert check_graphql({"query": "query IssueSnapshot { a }", "extra": 1}) is not None


def test_graphql_parser_ignores_strings_comments_and_fragments():
    doc = '# mutation in a comment\nquery IssueSnapshot { a(text: "mutation { x }") ...F }\nfragment F on Issue { title }'
    assert graphql_operations(doc) == [("query", "IssueSnapshot"), ("fragment", "F")]
    assert check_graphql({"query": doc}) is None


def test_osv_batch_allowed_extra_fields_refused():
    g, _ = gw()
    body = {"queries": [{"package": {"name": "golang.org/x/net", "ecosystem": "Go"}, "version": "0.30.0"}]}
    g.request("security", "POST", "https://api.osv.dev/v1/querybatch", LIVE, body)
    with pytest.raises(GatewayRefused):
        g.request("security", "POST", "https://api.osv.dev/v1/querybatch", LIVE, {"queries": [{"package": {"name": "x"}, "commit": "abc"}]})
    with pytest.raises(GatewayRefused):
        g.request("security", "POST", "https://api.osv.dev/v1/query", LIVE, {"package": {"name": "x", "purl": "p"}})


# --------------------------------------------------------------------- policy rows


@pytest.mark.parametrize(
    "connector,method,url",
    [
        ("issues", "DELETE", "https://api.github.com/repos/kubevirt/kubevirt/issues/1"),
        ("issues", "GET", "https://evil.example.com/repos/kubevirt/kubevirt"),
        ("issues", "GET", "https://api.github.com/repos/someone/else"),
        ("issues", "GET", "http://api.github.com/repos/kubevirt/kubevirt"),
        ("ci", "GET", "https://storage.googleapis.com/other-bucket/x"),
        ("ci", "POST", "https://storage.googleapis.com/kubevirt-prow/x"),
        ("security", "GET", "https://api.osv.dev/v1/query"),
        ("kubernetes", "GET", "https://api.github.com/repos/kubevirt/kubevirt"),
    ],
)
def test_requests_outside_policy_are_refused_and_reported(connector, method, url):
    g, violations = gw()
    with pytest.raises(GatewayRefused):
        g.request(connector, method, url, LIVE)
    assert violations and violations[0][0] == "allow_list_violation"
    assert g.refused()[0].allowed is False


def test_upstream_reads_allowed():
    g, violations = gw()
    g.request("issues", "GET", "https://api.github.com/repos/kubevirt/kubevirt/issues/18234/timeline", LIVE)
    g.request("ci", "GET", "https://storage.googleapis.com/kubevirt-prow/pr-logs/pull/x/build-log.txt", LIVE)
    assert not violations


def test_offline_refuses_everything_and_counts():
    g, violations = gw(mode=Mode.OFFLINE)
    with pytest.raises(GatewayRefused):
        g.request("issues", "GET", "https://api.github.com/repos/kubevirt/kubevirt", LIVE)
    assert g.attempted_external() == 1 and violations[0][0] == "offline_egress_attempt"


def test_live_ingest_refused_in_replay_and_frozen_runs():
    g, _ = gw()
    for ctx in (
        RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.REPLAY),
        RequestContext(purpose=EgressPurpose.LIVE_INGEST, frozen_evaluation=True),
    ):
        with pytest.raises(GatewayRefused):
            g.request("issues", "GET", "https://api.github.com/repos/kubevirt/kubevirt", ctx)


def test_model_and_publication_purposes_not_on_connector_routes():
    g, _ = gw()
    for purpose in (EgressPurpose.MODEL_CALL, EgressPurpose.PUBLICATION, EgressPurpose.WEBHOOK_DELIVERY):
        with pytest.raises(GatewayRefused):
            g.request("issues", "GET", "https://api.github.com/repos/kubevirt/kubevirt", RequestContext(purpose=purpose))


def test_transient_error_retried_once_then_unavailable():
    seq = iter([Response(503, None), Response(200, {"x": 1})])
    g, _ = gw(FakeTransport(lambda m, u, h, b: next(seq)))
    assert g.request("issues", "GET", "https://api.github.com/repos/kubevirt/kubevirt", LIVE).status == 200
    g2, _ = gw(FakeTransport(lambda m, u, h, b: Response(503, None)))
    with pytest.raises(SourceUnavailable):
        g2.request("issues", "GET", "https://api.github.com/repos/kubevirt/kubevirt", LIVE)


def test_tokens_attached_by_connector_and_never_logged():
    t = FakeTransport()
    g, _ = gw(t, tokens={"github": lambda: "tok-read", "publish": lambda: "tok-pub"})
    g.request("issues", "GET", "https://api.github.com/repos/kubevirt/kubevirt", LIVE)
    g.request("security", "POST", "https://api.osv.dev/v1/query", LIVE, {"package": {"name": "x"}})
    assert t.calls[0][2]["Authorization"] == "Bearer tok-read"
    assert "Authorization" not in t.calls[1][2]
    assert "tok" not in repr(g.attempts)


# --------------------------------------------------------------------- target CI

PUB = PublicationRecord("AnaghaP09/atlas-sandbox-target", "act-1", "abcdef1234567", 7, TaskState.AWAITING_VALIDATION)


def ci_ctx(pub=PUB, **kw):
    return RequestContext(purpose=EgressPurpose.TARGET_CI_READ, publication=pub, **kw)


def test_target_ci_bound_repository_and_commit_only():
    g, _ = gw()
    base = "https://api.github.com/repos/AnaghaP09/atlas-sandbox-target"
    g.request("target_ci", "GET", f"{base}/actions/runs?head_sha=abcdef1234567", ci_ctx())
    g.request("target_ci", "GET", f"{base}/actions/runs/99/jobs", ci_ctx())
    g.request("target_ci", "GET", f"{base}/commits/abcdef1234567/check-runs", ci_ctx())
    refused = [
        (f"{base}/actions/runs?head_sha=0000000", ci_ctx()),
        (f"{base}/actions/runs", ci_ctx()),
        (f"{base}/commits/0000000/check-runs", ci_ctx()),
        ("https://api.github.com/repos/kubevirt/kubevirt/actions/runs/1/jobs", ci_ctx()),
        ("https://api.github.com/repos/other/repo/actions/runs/1/jobs", ci_ctx()),
        (f"{base}/actions/runs/99/jobs", ci_ctx(pub=None)),
        (f"{base}/actions/runs/99/jobs", ci_ctx(frozen_evaluation=True)),
        (f"{base}/actions/runs/99/jobs", ci_ctx(pub=PublicationRecord(PUB.destination_repo, "a", PUB.published_commit, 7, TaskState.APPROVED_B))),
        (f"{base}/pulls/7", ci_ctx()),
    ]
    for url, ctx in refused:
        with pytest.raises(GatewayRefused):
            g.request("target_ci", "GET", url, ctx)


@pytest.mark.parametrize("state", [TaskState.PUBLISHED, TaskState.AWAITING_VALIDATION, TaskState.ACCEPTED, TaskState.FAILED])
def test_target_ci_reads_independent_of_approval_expiry(state):
    # Authorisation comes from the durable publication record; there is no approval input at all.
    g, _ = gw()
    pub = PublicationRecord(PUB.destination_repo, "act-1", PUB.published_commit, 7, state)
    g.request("target_ci", "GET", "https://api.github.com/repos/AnaghaP09/atlas-sandbox-target/actions/runs/1/jobs", ci_ctx(pub=pub))


# --------------------------------------------------------------------- corrections

PR_URL = "https://api.github.com/repos/AnaghaP09/atlas-sandbox-target/pulls/7"
ORIGINAL = "Fix IPv6 join. Cause: upstream bug in the resolver."
FIXED = "Fix IPv6 join."


def auth(text=FIXED, expected=ORIGINAL, aid="ca-1", expires=NOW + timedelta(hours=24), by=Role.AUDIT):
    return CorrectionAuthorisation(aid, "inc-1", "AnaghaP09/atlas-sandbox-target", 7, sha256_text(expected), text, expires, by)


def write_ctx(a):
    return RequestContext(purpose=EgressPurpose.CORRECTION_WRITE, authorisation=a)


class PRServer:
    """A fake pull request with a description and controllable failure modes."""

    def __init__(self, body=ORIGINAL):
        self.body = body
        self.patches = 0
        self.fail_mode = None  # "timeout_applied", "timeout_not_applied", "slow"
        self.pending = None

    def __call__(self, method, url, headers, payload):
        if method == "GET":
            return Response(200, {"body": self.body})
        if method == "PATCH":
            self.patches += 1
            if self.fail_mode == "timeout_applied":
                self.body = payload["body"]
                raise TimeoutError("lost response")
            if self.fail_mode == "slow":
                self.pending = payload["body"]
                raise TimeoutError("still processing")
            self.body = payload["body"]
            return Response(200, {"body": self.body})
        return Response(405, None)


def test_preauthorisation_read_needs_audit_and_incident(tmp_path):
    server = PRServer()
    g, _ = gw(FakeTransport(server))
    ctx = RequestContext(purpose=EgressPurpose.CORRECTION_PREAUTH_READ, role=Role.AUDIT, incident_id="inc-1", incident_publication=PUB)
    body, sha = g.preauthorisation_read(ctx)
    assert sha == sha256_text(ORIGINAL)
    for bad in (
        RequestContext(purpose=EgressPurpose.CORRECTION_PREAUTH_READ, role=Role.EM, incident_id="inc-1", incident_publication=PUB),
        RequestContext(purpose=EgressPurpose.CORRECTION_PREAUTH_READ, role=Role.AUDIT, incident_publication=PUB),
    ):
        with pytest.raises(GatewayRefused):
            g.request("correction", "GET", PR_URL, bad)
    with pytest.raises(GatewayRefused):
        g.request("correction", "GET", PR_URL.replace("/7", "/8"), ctx)
    with pytest.raises(GatewayRefused):
        g.request("correction", "PATCH", PR_URL, ctx, {"body": FIXED})


def test_correction_replaces_body_after_approval_expiry_and_repeat_is_noop(tmp_path):
    server = PRServer()
    g, _ = gw(FakeTransport(server), journal=CorrectionJournal(tmp_path / "j.sqlite"))
    a = auth()
    assert g.write_correction(write_ctx(a)) == "done"
    assert server.body == FIXED and server.patches == 1
    assert g.write_correction(write_ctx(a)) == "done"
    assert server.patches == 1


def test_two_authorisations_same_pr_second_is_stale_and_combined_succeeds(tmp_path):
    server = PRServer()
    g, _ = gw(FakeTransport(server), journal=CorrectionJournal(tmp_path / "j.sqlite"))
    assert g.write_correction(write_ctx(auth(text="A fixed", aid="ca-1"))) == "done"
    assert g.write_correction(write_ctx(auth(text="B fixed", aid="ca-2"))) == "refused_stale"
    assert server.body == "A fixed"
    combined = auth(text="A and B fixed", expected="A fixed", aid="ca-3")
    assert g.write_correction(write_ctx(combined)) == "done"
    assert server.body == "A and B fixed" and server.patches == 2


def test_reconciliation_before_slow_write_completes_holds_without_second_patch(tmp_path):
    server = PRServer()
    server.fail_mode = "slow"
    journal = CorrectionJournal(tmp_path / "j.sqlite")
    g, _ = gw(FakeTransport(server), journal=journal)
    a = auth()
    with pytest.raises(CorrectionHeld):
        g.write_correction(write_ctx(a))
    assert server.patches == 1
    with pytest.raises(CorrectionHeld):
        g.write_correction(write_ctx(a))  # never re-sent automatically
    assert server.patches == 1
    server.body = server.pending  # the slow write lands later
    assert g.reconcile_correction(write_ctx(a)) == "done"
    assert server.patches == 1


def test_lost_response_after_remote_effect_reconciles_to_done(tmp_path):
    server = PRServer()
    server.fail_mode = "timeout_applied"
    g, _ = gw(FakeTransport(server), journal=CorrectionJournal(tmp_path / "j.sqlite"))
    assert g.write_correction(write_ctx(auth())) == "done"
    assert server.patches == 1


def test_crash_before_local_commit_reconciles_without_second_write(tmp_path):
    server = PRServer()
    path = tmp_path / "j.sqlite"
    g, _ = gw(FakeTransport(server), journal=CorrectionJournal(path))
    a = auth()
    # Simulate: the remote write happened but the process died before recording "done".
    server.body = FIXED
    server.patches = 1
    g2, _ = gw(FakeTransport(server), journal=CorrectionJournal(path))
    assert g2.write_correction(write_ctx(a)) == "done"
    assert server.patches == 1


def test_body_changed_by_someone_else_is_refused_stale(tmp_path):
    server = PRServer(body="someone edited this")
    g, _ = gw(FakeTransport(server), journal=CorrectionJournal(tmp_path / "j.sqlite"))
    assert g.write_correction(write_ctx(auth())) == "refused_stale"
    assert server.patches == 0


def test_concurrent_correction_on_same_pr_is_held(tmp_path):
    server = PRServer()
    journal = CorrectionJournal(tmp_path / "j.sqlite")
    journal.acquire("AnaghaP09/atlas-sandbox-target#7", "other-holder")
    g, _ = gw(FakeTransport(server), journal=journal)
    with pytest.raises(CorrectionHeld):
        g.write_correction(write_ctx(auth()))
    assert server.patches == 0


@pytest.mark.parametrize(
    "method,url,body,a",
    [
        ("POST", PR_URL.replace("/pulls/7", "/issues/7/comments"), {"body": FIXED}, auth()),
        ("PATCH", PR_URL, {"body": FIXED, "title": "x"}, auth()),
        ("PATCH", PR_URL, {"state": "closed"}, auth()),
        ("PATCH", PR_URL, {"body": "different text"}, auth()),
        ("PATCH", PR_URL.replace("/7", "/8"), {"body": FIXED}, auth()),
        ("PATCH", "https://api.github.com/repos/other/repo/pulls/7", {"body": FIXED}, auth()),
        ("POST", "https://api.github.com/repos/AnaghaP09/atlas-sandbox-target/git/refs", {"ref": "x"}, auth()),
        ("POST", "https://api.github.com/repos/AnaghaP09/atlas-sandbox-target/pulls", {"title": "x"}, auth()),
        ("PATCH", PR_URL, {"body": FIXED}, auth(by=Role.EM)),
        ("PATCH", PR_URL, {"body": FIXED}, None),
    ],
)
def test_correction_policy_refusals(method, url, body, a):
    from atlas.gateway import policy_check

    assert policy_check("correction", method, url, body, write_ctx(a)) is not None


def test_expired_authorisation_refused(tmp_path):
    server = PRServer()
    g, _ = gw(FakeTransport(server), journal=CorrectionJournal(tmp_path / "j.sqlite"))
    with pytest.raises(GatewayRefused):
        g.write_correction(write_ctx(auth(expires=NOW - timedelta(seconds=1))))
    assert server.patches == 0


def test_correction_write_never_via_generic_request(tmp_path):
    g, _ = gw()
    with pytest.raises(GatewayRefused):
        g.request("correction", "PATCH", PR_URL, write_ctx(auth()), {"body": FIXED})
