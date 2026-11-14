"""Shared data shapes: every fixture validates and the cross-cutting rules hold."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from atlas import contracts

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schema"
FIXTURES = Path(__file__).parent / "fixtures"
RUN_FIXTURES = sorted(FIXTURES.glob("run_*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    resources = []
    for name in ("evidence.schema.json", "claim.schema.json", "output.schema.json", "events.json"):
        schema = _load(SCHEMA / name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def _validator(name: str) -> Draft202012Validator:
    schema = _load(SCHEMA / name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=_registry())


@pytest.mark.parametrize("name", ["evidence.schema.json", "claim.schema.json", "output.schema.json", "events.json"])
def test_schemas_are_valid(name: str) -> None:
    Draft202012Validator.check_schema(_load(SCHEMA / name))


def test_all_five_run_shapes_present() -> None:
    names = {p.stem for p in RUN_FIXTURES}
    assert names == {"run_complete", "run_rejected", "run_contradicted", "run_partial", "run_unverified"}


@pytest.mark.parametrize("path", RUN_FIXTURES, ids=lambda p: p.stem)
def test_run_fixture_validates(path: Path) -> None:
    errors = sorted(_validator("output.schema.json").iter_errors(_load(path)), key=str)
    assert not errors, [e.message for e in errors]


def _bound_claim_ids(run: dict) -> set[str]:
    ids: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            ids.update(value.get("claim_ids", []))
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk({k: v for k, v in run.items() if k != "claims"})
    return ids


def _pruned(run: dict) -> set[str]:
    """Claim ids that must not appear in the verified artefact: unsupported claims and dependants."""
    claims = {c["id"]: c for c in run["claims"]}
    bad = {cid for cid, c in claims.items() if c["verdict"] != "supported"}
    changed = True
    while changed:
        changed = False
        for cid, c in claims.items():
            if cid not in bad and any(d in bad for d in c["depends_on"]):
                bad.add(cid)
                changed = True
    return bad


@pytest.mark.parametrize("path", RUN_FIXTURES, ids=lambda p: p.stem)
def test_verified_artefact_has_no_pruned_claims(path: Path) -> None:
    run = _load(path)
    assert not (_bound_claim_ids(run) & _pruned(run))


@pytest.mark.parametrize("path", RUN_FIXTURES, ids=lambda p: p.stem)
def test_every_bound_claim_exists(path: Path) -> None:
    run = _load(path)
    known = {c["id"] for c in run["claims"]}
    assert _bound_claim_ids(run) <= known
    for claim in run["claims"]:
        assert set(claim["depends_on"]) <= known


def test_unverified_run_has_no_action() -> None:
    run = _load(FIXTURES / "run_unverified.json")
    assert run["action"]["value"] is None
    assert run["safety_disposition"] == "HOLD_FOR_HUMAN"


def test_event_names_match_contract_module() -> None:
    events = _load(SCHEMA / "events.json")
    assert tuple(events["event_names"]) == contracts.EVENT_NAMES
    assert set(events["properties"]["event"]["enum"]) == set(contracts.EVENT_NAMES)


def test_event_fixtures_validate() -> None:
    validator = _validator("events.json")
    for event in _load(FIXTURES / "events.json"):
        errors = list(validator.iter_errors(event))
        assert not errors, [e.message for e in errors]


def test_stream_reset_requires_reason_and_resume_from() -> None:
    validator = _validator("events.json")
    bad = {
        "event": "stream.reset", "event_id": "x", "emitted_at": "2026-07-30T12:00:00Z", "run_id": None,
        "authoritative_generation": None, "report_version": None, "correction_version": None, "data": {},
    }
    assert list(validator.iter_errors(bad))


def test_enums_match_schema() -> None:
    out = _load(SCHEMA / "output.schema.json")
    assert set(out["$defs"]["classification"]["enum"]) - {None} == {c.value for c in contracts.Classification}
    assert set(out["$defs"]["action"]["enum"]) - {None} == {a.value for a in contracts.Action}
    assert set(out["properties"]["run_status"]["enum"]) == {s.value for s in contracts.RunStatus}
    assert set(out["properties"]["safety_disposition"]["enum"]) == {s.value for s in contracts.SafetyDisposition}
    states = out["properties"]["security_check"]["properties"]["state"]["enum"]
    assert set(states) == {s.value for s in contracts.SecurityState}


def test_notification_catalog_is_complete_and_consistent() -> None:
    catalog = _load(SCHEMA / "notification_events.json")
    roles = set(catalog["roles"])
    assert roles == {r.value for r in contracts.Role}
    for name, entry in catalog["events"].items():
        assert entry["severity"] in catalog["severities"], name
        assert entry["kind"] in catalog["kinds"], name
        assert entry["roles"] and set(entry["roles"]) <= roles, name
        assert set(entry["backup_roles"]) <= roles, name
    events = catalog["events"]
    assert events["spend.80"]["severity"] == "warning" and set(events["spend.80"]["roles"]) == {"em", "audit"}
    assert events["spend.100"]["severity"] == "critical" and set(events["spend.100"]["roles"]) == {"em", "audit"}
    assert events["approval.nearing_expiry"]["kind"] == "reminder"
    assert events["approval.nearing_expiry"]["severity"] == "warning"
    assert "em" in events["approval.nearing_expiry"]["roles"] and events["approval.nearing_expiry"]["backup_roles"]


def test_task_lifecycle_includes_reapproval() -> None:
    assert contracts.TaskState.AWAITING_REAPPROVAL in set(contracts.TaskState)
    assert contracts.UNPUBLISHED_STATES.isdisjoint(contracts.PUBLISHED_STATES)
