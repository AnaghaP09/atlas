"""The data layer connected to the server: projects kept apart, live evidence, task security, polling."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from atlas.connectors.project import ProjectSources, SourceSet
from atlas.contracts import EgressPurpose, Mode, RunMode, SecurityContext, SecurityState
from atlas.data_wiring import (
    ActionSecurityReader,
    DataSnapshots,
    PollLoop,
    ProjectConfig,
    ProjectPoller,
    RunEvidenceStore,
    TargetScanner,
    load_project_configs,
)
from atlas.connectors.security import SecurityConnector
from atlas.gateway import Gateway, GatewayRefused, RequestContext, Response
from atlas.security_gate import SecurityGate
from atlas.snapshots import SnapshotError, SnapshotRegistry, normalized_sha256
from atlas.store import LIVE_SNAPSHOT_ID, ActionStore, EvidenceDB
from tests.a.helpers import T0, ctx, rec

SOURCES = SourceSet((ProjectSources("alpha", repository="example/alpha"),
                     ProjectSources("beta", repository="example/beta")))


def pctx(snapshot_id, project, mode=RunMode.REPLAY):
    return replace(ctx(snapshot_id, mode=mode), project_id=project)


@pytest.fixture
def registry(tmp_path):
    reg = SnapshotRegistry(tmp_path / "data", sources=SOURCES, reference_repository="example/alpha")
    yield reg
    reg.close()


# ---------------------------------------------------------------------------- projects kept apart


def test_each_project_has_its_own_live_store(registry, tmp_path):
    registry.store("alpha").put(rec(version="a1"), "connector.issues", content={"title": "alpha"})
    registry.store("beta").put(rec(version="b1"), "connector.issues", content={"title": "beta"})
    a = registry.store().get(pctx(LIVE_SNAPSHOT_ID, "alpha", RunMode.LIVE), "issues", "issue/1")
    b = registry.store().get(pctx(LIVE_SNAPSHOT_ID, "beta", RunMode.LIVE), "issues", "issue/1")
    assert (a.content_version_id, b.content_version_id) == ("a1", "b1")
    assert (tmp_path / "data" / "local" / "stores" / "alpha" / "live.sqlite").exists()
    assert (tmp_path / "data" / "local" / "stores" / "beta" / "live.sqlite").exists()


def test_frozen_snapshots_are_namespaced_listed_and_opened_per_project(registry):
    registry.store("alpha").put(rec(version="a1"), "connector.issues")
    registry.store("beta").put(rec(version="b1"), "connector.issues")
    info_a = registry.freeze(frozen_at=T0, project_id="alpha")
    info_b = registry.freeze(frozen_at=T0, project_id="beta")
    assert info_a.snapshot_id.startswith("alpha.") and info_b.snapshot_id.startswith("beta.")
    assert {s["snapshot_id"] for s in registry.list_snapshots("alpha")} == {info_a.snapshot_id}
    assert registry.store().get(pctx(info_a.snapshot_id, "alpha"), "issues", "issue/1").content_version_id == "a1"
    with pytest.raises(SnapshotError, match="another project"):
        registry.open(pctx(info_b.snapshot_id, "alpha"))


def _export(root, snapshot_id, rows):
    d = root / "snapshots" / "releases" / snapshot_id
    d.mkdir(parents=True)
    (d / "records.jsonl").write_bytes("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode())
    (d / "snapshot.json").write_text(json.dumps({"snapshot_id": snapshot_id, "cutoff": T0.isoformat(),
                                                 "records_sha256": normalized_sha256(d / "records.jsonl")}))


def test_prepared_snapshots_belong_to_the_project_of_the_reference_repository(registry, tmp_path):
    src = EvidenceDB(tmp_path / "src.sqlite")
    src.put(rec(), "connector.issues", content={"title": "t"})
    _export(tmp_path / "data", "release-close-x", src.export_rows())
    src.close()
    assert [s["project_id"] for s in registry.list_snapshots("alpha")] == ["alpha"]
    assert registry.list_snapshots("beta") == []
    assert registry.store().get(pctx("release-close-x", "alpha"), "issues", "issue/1") is not None
    with pytest.raises(SnapshotError, match="another project"):
        registry.open(pctx("release-close-x", "beta"))


def test_live_evidence_returns_the_named_version_and_stays_in_its_project(registry):
    store = registry.store("alpha")
    first = store.put(rec(version="v1", revision=1), "connector.issues", content={"title": "first"})
    store.put(rec(version="v2", revision=2), "connector.issues", content={"title": "second"})
    old = registry.live_evidence("alpha", first, "v1")
    assert old["content"] == {"title": "first"} and old["project_id"] == "alpha"
    assert registry.live_evidence("beta", first, "v1") is None


# ---------------------------------------------------------------------------- action store per project


def test_action_store_keeps_projects_apart(tmp_path):
    actions = ActionStore(tmp_path / "actions.sqlite")
    run = {"scan_run_id": "scan-1", "destination": "o/r", "action_id": "act-1", "revision": "abc",
           "inventory": [], "per_module": {}, "completion": "complete", "coverage_ok": 0, "coverage_total": 0,
           "observed_at": T0, "confirmed_through": T0, "fresh_until": T0 + timedelta(hours=24)}
    a = SecurityContext("o/r", "act-1", "abc", "alpha")
    b = SecurityContext("o/r", "act-1", "abc", "beta")
    actions.write_scan_run(a, "connector.security", run, [])
    assert len(actions.scan_runs(a, "security_gate")) == 1
    assert actions.scan_runs(b, "security_gate") == []


# ---------------------------------------------------------------------------- task security


class NoNetwork:
    calls = 0

    def __call__(self, method, url, headers, body):
        NoNetwork.calls += 1
        raise AssertionError("no request expected")


def test_target_without_dependencies_gets_a_passing_security_decision(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "go.mod").write_text("module example.org/demo\n\ngo 1.27\n", encoding="utf-8")
    actions = ActionStore(tmp_path / "actions.sqlite")
    gateway = Gateway(NoNetwork(), clock=lambda: T0)
    scanner = TargetScanner(SecurityConnector(gateway, action_store=actions, clock=lambda: T0), target)
    reader = ActionSecurityReader(actions, lambda c: scanner.module_path())
    c = SecurityContext("o/r", "act-1", "abc", "alpha")
    scanner.scan(c)
    result = SecurityGate("policy-1", lambda: T0 + timedelta(minutes=5)).evaluate_for_task(reader, c, scanner.components(c))
    assert result.state is SecurityState.PASSED
    assert NoNetwork.calls == 0
    other = SecurityContext("o/r", "act-1", "abc", "beta")
    blocked = SecurityGate("policy-1", lambda: T0).evaluate_for_task(reader, other, scanner.components(other))
    assert blocked.state is SecurityState.NOT_RUN


# ---------------------------------------------------------------------------- gateway row for Actions


def test_actions_reads_only_for_configured_repositories():
    seen = []

    def transport(method, url, headers, body):
        seen.append(url)
        return Response(200, {"workflow_runs": []}, {})

    g = Gateway(transport, sources=SOURCES)
    live = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
    g.request("actions", "GET", "https://api.github.com/repos/example/beta/actions/runs", live)
    for bad in ("https://api.github.com/repos/other/repo/actions/runs",
                "https://api.github.com/repos/example/beta/issues"):
        with pytest.raises(GatewayRefused):
            g.request("actions", "GET", bad, live)
    with pytest.raises(GatewayRefused):
        g.request("actions", "POST", "https://api.github.com/repos/example/beta/actions/runs", live, {})
    assert len(seen) == 1


# ---------------------------------------------------------------------------- poller and dashboard


class FakeFeed:
    def __init__(self):
        self.polls, self.activity, self.panels, self.connected = [], [], {}, False

    def record_poll(self, project_id, at=None):
        self.polls.append(project_id)

    def record_activity(self, project_id, item):
        self.activity.append((project_id, item))

    def update_panels(self, project_id, panels, totals=None):
        self.panels[project_id] = dict(panels)
        self.totals = dict(totals or {})
        return 1

    def set_history(self, project_id, points):
        self.history = list(points)


def _github(routes):
    def transport(method, url, headers, body):
        for key, value in routes.items():
            if key in url:
                if callable(value):
                    return value(headers)
                return Response(200, value, {"ETag": '"e1"'})
        return Response(404, None, {})
    return transport


def test_poller_updates_dashboard_with_ci_runs_and_release(registry):
    routes = {
        "/issues?": [],
        "/actions/runs": {"workflow_runs": [
            {"id": 7, "name": "ci", "status": "completed", "conclusion": "failure", "head_branch": "main",
             "head_sha": "abc", "created_at": T0.isoformat(), "updated_at": T0.isoformat(), "html_url": "https://x"}]},
        "/releases/latest": {"tag_name": "v1.0.0", "published_at": T0.isoformat(), "name": "v1.0.0"},
    }
    gateway = Gateway(_github(routes), sources=SOURCES, clock=lambda: T0)
    feed = FakeFeed()
    cfg = ProjectConfig(sources=SOURCES.projects[1], ci="github_actions", labels={"bug": "bug"})
    poller = ProjectPoller("beta", cfg, gateway, registry, feed, clock=lambda: T0)
    result = poller.poll_once()
    assert result["polled"] is True and feed.connected and feed.polls == ["beta"]
    panels = feed.panels["beta"]
    assert panels["failing_ci"][0]["conclusion"] == "failure"
    assert len(registry.live("beta").search({"source": "ci"})) == 1
    assert panels["latest_release"][0]["tag"] == "v1.0.0"
    stored = registry.live("beta").search({"source": "ci"})
    assert len(stored) == 1 and registry.live("alpha").search({"source": "ci"}) == []


def test_poller_failure_confirms_nothing_and_reports_the_reason(registry):
    def down(method, url, headers, body):
        raise ConnectionError("down")

    gateway = Gateway(down, sources=SOURCES, clock=lambda: T0)
    feed = FakeFeed()
    poller = ProjectPoller("beta", ProjectConfig(sources=SOURCES.projects[1]), gateway, registry, feed, clock=lambda: T0)
    result = poller.poll_once()
    assert result["polled"] is False and "poll failed" in result["reason"]
    assert feed.polls == [] and feed.connected is False


def test_offline_mode_pauses_polling_without_any_request(registry):
    calls = []
    gateway = Gateway(lambda *a: calls.append(a), mode=lambda: Mode.OFFLINE, sources=SOURCES)
    poller = ProjectPoller("beta", ProjectConfig(sources=SOURCES.projects[1]), gateway, registry, FakeFeed())
    loop = PollLoop({"beta": poller}, lambda: Mode.OFFLINE)
    assert "offline" in loop.tick()["paused"]
    assert calls == []


# ---------------------------------------------------------------------------- snapshot port and run store


def test_snapshot_port_freezes_lists_and_reads_per_project(registry):
    registry.store("alpha").put(rec(version="a1"), "connector.issues", content={"title": "alpha"})
    port = DataSnapshots(registry, {}, "alpha", clock=lambda: T0)
    snap = port.freeze("alpha")
    assert snap["snapshot_id"].startswith("alpha.") and snap["project_id"] == "alpha"
    assert [s["snapshot_id"] for s in port.list(project_id="alpha")] == [snap["snapshot_id"]]
    assert port.list(project_id="beta") == []
    record_id = registry.live("alpha").get("issues", "issue/1").record_id
    assert port.evidence(snap["snapshot_id"], record_id, "a1")["content"] == {"title": "alpha"}


def test_run_store_hands_out_content_only_for_records_it_returned(registry):
    registry.store("alpha").put(rec(version="a1"), "connector.issues", content={"title": "seen"})
    info = registry.freeze(frozen_at=T0, project_id="alpha")
    store = RunEvidenceStore(registry)
    unseen = rec(version="zz", record_id="issues:issue/9:zz")
    assert store.content_of(unseen) == ""
    got = store.get(pctx(info.snapshot_id, "alpha"), "issues", "issue/1")
    assert json.loads(store.content_of(got)) == {"title": "seen"}


def test_project_configs_come_only_from_the_projects_file(tmp_path):
    assert load_project_configs(tmp_path / "missing.toml") == {}
    f = tmp_path / "projects.toml"
    f.write_text('[projects.one]\nrepository = "example/one"\nci = "github_actions"\n[projects.one.labels]\nbug = "bug"\n',
                 encoding="utf-8")
    cfg = load_project_configs(f)["one"]
    assert cfg.sources.repository == "example/one" and cfg.ci == "github_actions" and cfg.labels == {"bug": "bug"}


def test_failing_ci_panel_says_so_when_nothing_failed(registry):
    routes = {"/issues?": [], "/releases/latest": {},
              "/actions/runs": {"workflow_runs": [{"id": 1, "name": "ci", "status": "completed", "conclusion": "success",
                                                   "updated_at": T0.isoformat()}]}}
    gateway = Gateway(_github(routes), sources=SOURCES, clock=lambda: T0)
    feed = FakeFeed()
    poller = ProjectPoller("beta", ProjectConfig(sources=SOURCES.projects[1], ci="github_actions"), gateway, registry,
                           feed, clock=lambda: T0)
    poller.poll_once()
    assert "no failing runs" in feed.panels["beta"]["failing_ci"][0]["note"]
