"""Demo runner against both servers, manifest coverage, and the evaluation-set builders."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas.api.app import create_app
from atlas.api.demo_vars import demo_variables, missing_variables, variable_settings
from atlas.api.services import build_core
from demo.run_demo import MANIFEST, DemoStop, Runner, check, fill, load_rows
from prep.injection.build import INSTRUCTION, inject
from prep.new_evidence.build import add_record
from prep.proxy.build import candidates
from tests.d.conftest import StubOrchestrator
from tests.d.fakes import FakeContainmentPort, FakeSnapshots, FakeTasks

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_ROWS = {"T1", "G13", "T2", "T3", "T4", "T6a", "T6b", "T7", "N1", "T5", "C3", "R1-R6", "F1", "X1-X5", "K1",
                 "E1", "O1"}


def _send_for(clients):
    def send(method, base, path, headers, body, params):
        c = clients[base]
        r = c.request(method, path, headers=dict(headers), json=body, params=params)
        return r.status_code, r.headers, r.json()
    return send


def _servers():
    normal = build_core()
    normal.snapshots, normal.tasks, normal.containment = FakeSnapshots(), FakeTasks(), FakeContainmentPort()
    normal.model_configured = True
    normal.orchestrator = StubOrchestrator(normal)
    scenario = build_core(environment="scenario", port=8001)
    scenario.snapshots, scenario.tasks, scenario.containment = FakeSnapshots(), FakeTasks(), FakeContainmentPort()
    scenario.model_configured = True
    clients = {"http://127.0.0.1:8000": TestClient(create_app(normal), base_url="http://127.0.0.1:8000"),
               "http://127.0.0.1:8001": TestClient(create_app(scenario), base_url="http://127.0.0.1:8001")}
    urls = {"normal": "http://127.0.0.1:8000", "scenario": "http://127.0.0.1:8001"}
    return normal, scenario, clients, urls


VARS = {"release_snapshot": "snap-v1.9.0", "aliased_snapshot": "snap-v1.9.0-aliased", "release_id": "v1.9.0",
        "release_filter": "1.9", "case_G01": "snap-v1.9.0", "case_G03": "snap-v1.9.0", "case_G04": "snap-v1.9.0"}


def test_manifest_covers_every_row_and_has_no_hard_coded_ids():
    rows = load_rows()
    assert {r["row_id"] for r in rows} >= REQUIRED_ROWS
    for r in rows:
        assert r["server"] in ("normal", "scenario") and r["data_mode"] in ("REAL", "SYNTHETIC", "STAGED", "UNEVALUATED")
    text = MANIFEST.read_text(encoding="utf-8")
    assert not re.search(r"snap-v\d|case-G\d|v\d+\.\d+\.\d+", text)
    assert set(variable_settings()) == set(VARS)


def test_normal_rows_never_trip_containment_or_go_offline():
    for r in load_rows():
        if r["server"] == "normal":
            for c in r["calls"]:
                assert not c["path"].startswith("/api/scenario/")
                assert not (c["path"] == "/api/mode" and (c.get("body") or {}).get("mode") == "offline")


def test_demo_variables_come_from_settings():
    env = {key: VARS[name] for name, key in variable_settings().items()}
    assert demo_variables(env) == VARS
    assert missing_variables({}) and missing_variables(env) == []


def test_runner_executes_rows_on_both_servers_with_restoration():
    normal, scenario, clients, urls = _servers()
    logs = []
    runner = Runner(_send_for(clients), urls, log=logs.append, wait_seconds=0, saved=dict(VARS))
    runner.run(load_rows(["T5", "C3", "T6b", "O1"]))
    assert scenario.mode.get().value == "connected"  # restoration ran
    assert len(logs) == 4


def test_runner_stops_on_the_wrong_server():
    normal, scenario, clients, urls = _servers()
    swapped = {"normal": urls["scenario"], "scenario": urls["normal"]}
    with pytest.raises(DemoStop, match="expected the normal server"):
        Runner(_send_for(clients), swapped, log=lambda m: None, wait_seconds=0, saved=dict(VARS)).run(load_rows(["T5"]))


def test_runner_reuses_keys_for_retries_and_checks_replay_header():
    normal, scenario, clients, urls = _servers()
    rows = load_rows(["T6a"])
    Runner(_send_for(clients), urls, log=lambda m: None, wait_seconds=0, saved=dict(VARS)).run(rows)
    assert len(normal.orchestrator.started) == 1  # the retried request did not start a second run


def test_assertion_ops_and_templating():
    data = {"a": {"b": [1, 2]}, "s": "Measured on a proxy", "entries": [{"trigger": "x"}]}
    assert check(data, {"path": "a.b", "op": "len_gte", "value": 2})
    assert check(data, {"path": "a.b.1", "op": "eq", "value": 2})
    assert check(data, {"path": "s", "op": "contains", "value": "proxy"})
    assert check(data, {"path": "missing", "op": "absent"})
    assert check(data, {"path": "entries", "op": "contains_where", "value": {"trigger": "x"}})
    assert fill({"p": "/api/runs/{r}", "v": "{r}"}, {"r": "run-1"}) == {"p": "/api/runs/run-1", "v": "run-1"}


def test_injection_twin_marks_the_changed_record_synthetic():
    records = [{"record_id": "l1", "source": "ci", "kind": "log", "text": "connection refused", "content_version_id": "v1"},
               {"record_id": "i1", "source": "issues", "kind": "issue", "text": "flake"}]
    twin = inject(records, "log_line")
    changed = next(r for r in twin if r["record_id"] == "l1")
    assert INSTRUCTION in changed["text"] and changed["provenance"] == "synthetic"
    assert changed["content_version_id"] != "v1" and records[0]["text"] == "connection refused"
    with pytest.raises(ValueError):
        inject(records, "pr_title")


def test_new_evidence_record_is_synthetic_and_scoped():
    row = {"case_id": "NE4", "new_record_source": "security", "new_record_summary": "blocking vulnerability"}
    out = add_record([{"record_id": "x"}], row, "2026-10-01T00:00:00+00:00")
    assert out[-1]["provenance"] == "synthetic" and out[-1]["access_scopes"] == ["restricted:security"]


def test_expected_sets_cover_the_plan():
    import csv

    with open(ROOT / "data" / "evaluator" / "injection_expected.csv", encoding="utf-8") as f:
        inj = list(csv.DictReader(f))
    assert {r["base_case"] for r in inj} == {"G01", "G02", "G03", "G06", "G09"}
    assert {r["location"] for r in inj if r["variant"] == "injected"} == {"log_line", "issue_comment", "commit_message",
                                                                         "pr_title"}
    with open(ROOT / "data" / "evaluator" / "new_evidence_expected.csv", encoding="utf-8") as f:
        ne = list(csv.DictReader(f))
    assert len(ne) == 5 and all(r["before_action"] != r["after_action"] or r["before_classification"] != r["after_classification"]
                                for r in ne)
    assert set(json.loads((ROOT / "data" / "evaluator" / "carry_over_expected.json").read_text())) >= {"required", "excluded"}


def test_proxy_candidates_follow_the_definition():
    history = {
        "release": "1.6", "beta_at": "2025-06-01", "released_at": "2025-07-01",
        "backports": [
            {"pr": 1, "merged_at": "2025-06-10", "issues": [{"issue_id": "11", "kinds": ["kind/bug"], "title": "race"}]},
            {"pr": 2, "merged_at": "2025-05-10", "issues": [{"issue_id": "12", "kinds": ["kind/bug"], "title": "before beta"}]},
            {"pr": 3, "merged_at": "2025-06-11", "issues": [{"issue_id": "13", "kinds": ["kind/feature"], "title": "feature"}]},
            {"pr": 4, "merged_at": "2025-06-12", "issues": [{"issue_id": "14", "kinds": ["kind/flake"],
                                                            "title": "Quarantine Transition Tracker"}]},
        ],
        "open_at_release": [{"issue_id": "15", "labels": ["kind/flake", "priority/critical-urgent"], "title": "flaky"},
                            {"issue_id": "16", "labels": ["kind/flake"], "title": "flaky, not urgent"}],
    }
    got = candidates(history)
    assert [c["issue_id"] for c in got] == ["11", "15"]
