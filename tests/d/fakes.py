"""Fakes for the ports the API needs from the data and execution layers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from atlas.api.ports import Refused
from atlas.contracts import EligibilityResult, PUBLISHED_STATES

CUTOFF = "2026-07-30T00:00:00+00:00"


class FakeSnapshots:
    def __init__(self) -> None:
        self.snapshots: dict[str, dict[str, Any]] = {
            "snap-v1.9.0": {"snapshot_id": "snap-v1.9.0", "project_id": "kubevirt", "release": "1.9", "kind": "prepared",
                            "cutoff": CUTOFF, "alias_variant": None, "mode": "replay"},
            "snap-v1.9.0-aliased": {"snapshot_id": "snap-v1.9.0-aliased", "project_id": "kubevirt", "release": "1.9",
                                    "kind": "aliased", "cutoff": CUTOFF, "alias_variant": "a1", "mode": "replay"},
        }
        self.records: dict[tuple[str, str], dict[str, Any]] = {
            ("snap-v1.9.0", "rec-public"): {"record_id": "rec-public", "content_version_id": "cv1", "source": "issues",
                                            "access_scopes": ["public"], "provenance": "observed",
                                            "url": "https://github.com/kubevirt/kubevirt/issues/18234", "text": "flake"},
            ("snap-v1.9.0", "rec-restricted"): {"record_id": "rec-restricted", "content_version_id": "cv1",
                                                "source": "security", "access_scopes": ["restricted:security"],
                                                "provenance": "observed", "text": "SECRET-FINDING-MARKER"},
        }
        self.graphs = {
            "snap-v1.9.0": {"nodes": [{"id": "virt-handler", "required_scopes": ["public"]},
                                      {"id": "scanner-node", "required_scopes": ["restricted:security"],
                                       "label": "SECRET-FINDING-MARKER"}], "edges": []},
            "snap-v1.9.0-aliased": {"nodes": [{"id": "component-a", "required_scopes": ["public"]}], "edges": []},
        }
        self.ingests: list[dict[str, Any]] = []

    def ingest(self, sources: Sequence[str], project_id: str) -> Mapping[str, Any]:
        rec = {"ingest_id": f"ing-{uuid.uuid4().hex[:8]}", "sources": list(sources), "project_id": project_id}
        self.ingests.append(rec)
        return rec

    def freeze(self, project_id: str) -> Mapping[str, Any]:
        sid = f"snap-live-{uuid.uuid4().hex[:6]}"
        self.snapshots[sid] = {"snapshot_id": sid, "project_id": project_id, "release": None, "kind": "live-frozen",
                               "cutoff": datetime.now(timezone.utc).isoformat(), "alias_variant": None, "mode": "replay"}
        return self.snapshots[sid]

    def list(self, release: str | None = None, project_id: str | None = None) -> list[Mapping[str, Any]]:
        return [s for s in self.snapshots.values() if release is None or s["release"] == release]

    def get(self, snapshot_id: str) -> Mapping[str, Any] | None:
        return self.snapshots.get(snapshot_id)

    def graph(self, snapshot_id: str) -> Mapping[str, Any]:
        return self.graphs.get(snapshot_id, {"nodes": [], "edges": []})

    def evidence(self, snapshot_id: str, record_id: str, content_version_id: str | None,
                 lineage: Sequence[str] = ()) -> Mapping[str, Any] | None:
        rec = self.records.get((snapshot_id, record_id))
        if rec is None or (content_version_id and rec["content_version_id"] != content_version_id):
            return None
        return rec


class FakeTasks:
    """Minimal operational task store honouring the lifecycle the API exposes."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.approval_rows: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.refuse_next: str | None = None

    def _maybe_refuse(self) -> None:
        if self.refuse_next:
            reason, self.refuse_next = self.refuse_next, None
            raise Refused(reason)

    def create(self, run_id: str, generation: int, action: Mapping[str, Any], principal_role: str, key: str) -> Mapping[str, Any]:
        self.calls.append(("create", run_id))
        self._maybe_refuse()
        tid = f"task-{uuid.uuid4().hex[:8]}"
        self.tasks[tid] = {"task_id": tid, "project_id": "kubevirt", "run_id": run_id, "state": "proposed",
                           "action_id": f"act-{uuid.uuid4().hex[:8]}", "bound_generation": generation,
                           "action": action.get("value"), "support_status": "supported",
                           "security": {"state": "passed", "decision_id": "sec-1", "valid_until": CUTOFF},
                           "approvals": [], "checks": [], "journal": []}
        aid = f"apr-{uuid.uuid4().hex[:8]}"
        self.approval_rows[aid] = {"approval_id": aid, "task_id": tid, "kind": "A", "state": "requested",
                                   "project_id": "kubevirt"}
        return self.tasks[tid]

    def list(self, project_id: str | None = None) -> list[Mapping[str, Any]]:
        return list(self.tasks.values())

    def get(self, task_id: str) -> Mapping[str, Any] | None:
        return self.tasks.get(task_id)

    def approvals(self, project_id: str | None = None) -> list[Mapping[str, Any]]:
        return list(self.approval_rows.values())

    def approval(self, approval_id: str) -> Mapping[str, Any] | None:
        return self.approval_rows.get(approval_id)

    def decide(self, approval_id: str, decision: str, review_minutes: float, role: str, key: str) -> Mapping[str, Any]:
        self.calls.append(("decide", approval_id))
        self._maybe_refuse()
        row = self.approval_rows[approval_id]
        row.update(state="approved" if decision == "approve" else "rejected", review_minutes=review_minutes)
        if decision == "approve":
            self.tasks[row["task_id"]]["state"] = "approved_a" if row["kind"] == "A" else "approved_b"
        return row

    def _step(self, name: str, task_id: str, new_state: str) -> Mapping[str, Any]:
        self.calls.append((name, task_id))
        self._maybe_refuse()
        t = self.tasks[task_id]
        t["state"] = new_state
        if new_state == "published":
            t["pull_request"] = "https://github.com/AnaghaP09/atlas-sandbox-target/pull/1"
        return t

    def prepare(self, task_id: str, role: str, key: str) -> Mapping[str, Any]:
        return self._step("prepare", task_id, "prepared")

    def publish(self, task_id: str, role: str, key: str) -> Mapping[str, Any]:
        return self._step("publish", task_id, "published")

    def adjudicate(self, task_id: str, verdict: str, note: str, role: str, key: str) -> Mapping[str, Any]:
        t = self._step("adjudicate", task_id, "accepted" if verdict == "correct" else "failed")
        t["adjudication"] = {"verdict": verdict, "note": note}
        return t

    def security_refresh(self, task_id: str, role: str, key: str) -> Mapping[str, Any]:
        t = self.tasks[task_id]
        new = "proposed" if t["state"] == "proposed" else "awaiting_reapproval"
        return self._step("security_refresh", task_id, new)

    def scorecard(self, project_id: str | None = None) -> Mapping[str, Any]:
        return {"data_mode": "SYNTHETIC", "rows": [{"task_class": "test-fix", "net_contribution": "unavailable",
                                                     "baseline": "SYNTHETIC"}]}

    def tasks_for_run(self, run_id: str) -> list[Mapping[str, Any]]:
        return [{"task_id": t["task_id"], "action": t["action"], "state": t["state"]}
                for t in self.tasks.values() if t["run_id"] == run_id]

    def apply_reverification(self, run_id: str, new_generation: int, assessments: Mapping[str, str]) -> None:
        for tid, assessment in assessments.items():
            t = self.tasks[tid]
            if t["state"] in {s.value for s in PUBLISHED_STATES}:
                t["support_status"] = "supported" if assessment == "still_supported" else "withdrawn"
            else:
                t["state"] = "awaiting_reapproval" if assessment == "still_supported" else "voided"
                t["bound_generation"] = new_generation


class FakeContainmentPort:
    def __init__(self) -> None:
        self.tripped: dict[str, str] = {}
        self.incidents: dict[str, dict[str, Any]] = {}
        self.corrections_by_id: dict[str, dict[str, Any]] = {}
        self.next_outcome = "done"
        self.writes = 0

    def trip(self, consumer: str, reason: str) -> str:
        self.tripped[consumer] = reason
        iid = f"inc-{uuid.uuid4().hex[:6]}"
        self.incidents[iid] = {"incident_id": iid, "switch": consumer, "prerequisites": [], "open": True,
                               "required_scopes": ["internal"]}
        return iid

    def permits(self, consumer: str) -> EligibilityResult:
        if consumer in self.tripped:
            return EligibilityResult(False, (self.tripped[consumer],))
        return EligibilityResult(True)

    def state(self) -> Mapping[str, Any]:
        return {"switches": [{"switch": k, "reason": v} for k, v in self.tripped.items()],
                "incidents": list(self.incidents.values())}

    def switch(self, switch_id: str) -> Mapping[str, Any] | None:
        if switch_id not in self.tripped:
            return None
        return {"switch": switch_id, "incidents": [i for i in self.incidents.values() if i["switch"] == switch_id]}

    def record_prerequisite(self, incident_id: str, kind: str, reference: str, note: str, role: str) -> Mapping[str, Any]:
        inc = self.incidents.get(incident_id)
        if inc is None:
            raise Refused("unknown incident", 404)
        if reference.startswith("other-incident"):
            raise Refused("the reference belongs to another incident", 409)
        pid = f"pre-{uuid.uuid4().hex[:6]}"
        inc["prerequisites"].append({"id": pid, "kind": kind})
        return {"prerequisite_id": pid, "incident_id": incident_id, "kind": kind}

    def resume(self, incident_id: str, prerequisite_ids: Sequence[str], role: str) -> Mapping[str, Any]:
        inc = self.incidents[incident_id]
        if not prerequisite_ids:
            raise Refused("required prerequisites are missing", 409)
        inc["open"] = False
        self.tripped.pop(inc["switch"], None)
        return inc

    def authorise_correction(self, incident_id: str, body: Mapping[str, Any], role: str) -> Mapping[str, Any]:
        return {"authorisation_id": f"auth-{uuid.uuid4().hex[:6]}", "incident_id": incident_id,
                "expected_body_hash": "h1", "required_scopes": ["internal"]}

    def corrections(self, incident_id: str) -> list[Mapping[str, Any]]:
        return [c for c in self.corrections_by_id.values() if c["incident_id"] == incident_id]

    def correction(self, correction_id: str) -> Mapping[str, Any] | None:
        return self.corrections_by_id.get(correction_id)

    def apply_correction(self, body: Mapping[str, Any], role: str, key: str) -> Mapping[str, Any]:
        if body.get("expected_body_hash") != "h1":
            raise Refused("stale_body: the live body changed; a combined authorisation is needed", 409)
        self.writes += 1
        cid = f"cor-{uuid.uuid4().hex[:6]}"
        rec = {"correction_id": cid, "incident_id": body["incident_id"], "state": self.next_outcome,
               "reason": "outcome uncertain" if self.next_outcome == "held" else None, "required_scopes": ["internal"]}
        self.corrections_by_id[cid] = rec
        return rec
