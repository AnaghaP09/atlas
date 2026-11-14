"""Containment: kill switches that stop parts of the system when a safety rule is broken.

Every trip creates an immutable incident. Consumers (model calls, preparation, publication, report
delivery, execution, full output) ask ``permits`` before acting. Resumption needs prerequisite
records bound to that incident and validated by the server; nothing resumes by default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from atlas.contracts import EligibilityResult
from atlas.execution.db import OpsDB, iso, new_id

MODEL_CALLS = "model_calls"
PREPARATION = "preparation"
PUBLICATION = "publication"
REPORT_DELIVERY = "report_delivery"
EXECUTION = "execution"
FULL_OUTPUT = "full_output"
CONSUMERS = (MODEL_CALLS, PREPARATION, PUBLICATION, REPORT_DELIVERY, EXECUTION, FULL_OUTPUT)

# Kinds a person may record through the general prerequisite endpoint.
GENERIC_KINDS = frozenset({
    "cause_fixed", "owner_signoff", "devops_audit", "investigation", "golden_case",
    "cause_recorded", "disclosure", "fix",
})
# Kinds only a specific operation may produce, with the producer that must have made them.
RESTRICTED_KINDS: dict[str, str] = {
    "drill_acknowledged": "drill",
    "correction": "corrections",
    "regression_run": "harness",
    "eval_run": "harness",
    "sandbox_escape_test_run": "harness",
}
REQUIRED_SUITES = {
    "regression_run": "golden-regression",
    "eval_run": "acceptance",
    "sandbox_escape_test_run": "sandbox-escape",
}


@dataclass(frozen=True)
class Rule:
    row: int
    trigger: str
    blocks: tuple[str, ...]
    prerequisites: tuple[str, ...]
    voids_pending_approvals: bool = False


RULES: dict[int, Rule] = {r.row: r for r in (
    Rule(1, "write outside the bound branch or destination", (PREPARATION, PUBLICATION, EXECUTION),
         ("cause_fixed", "owner_signoff")),
    Rule(2, "diff changed after Approval B", (PREPARATION, PUBLICATION, EXECUTION),
         ("cause_fixed", "owner_signoff"), voids_pending_approvals=True),
    Rule(3, "sandbox violation", (PREPARATION, PUBLICATION, EXECUTION),
         ("cause_fixed", "owner_signoff", "sandbox_escape_test_run")),
    Rule(4, "outbound attempt in Offline mode", (MODEL_CALLS, PUBLICATION), ("devops_audit", "owner_signoff")),
    Rule(5, "call outside the allow-list in Connected mode", (MODEL_CALLS, PUBLICATION), ("devops_audit", "owner_signoff")),
    Rule(6, "unredacted personal data found outbound", (MODEL_CALLS, PUBLICATION), ("devops_audit", "owner_signoff")),
    Rule(7, "acted-on injection", (EXECUTION, PREPARATION, PUBLICATION),
         ("investigation", "golden_case", "regression_run", "owner_signoff"), voids_pending_approvals=True),
    Rule(8, "unsupported claim published", (PUBLICATION,), ("correction", "cause_recorded", "owner_signoff")),
    Rule(9, "unauthorised disclosure", (PUBLICATION, REPORT_DELIVERY), ("disclosure", "fix", "owner_signoff")),
    Rule(10, "illustrative number published as calibrated", (PUBLICATION,), ("correction", "owner_signoff")),
    Rule(11, "quality collapse", (FULL_OUTPUT,), ("eval_run", "owner_signoff")),
)}


# What people see for each switch: a stable id, a plain title and what it stops, in plain words.
@dataclass(frozen=True)
class SwitchInfo:
    switch_id: str
    title: str


SWITCHES: dict[int, SwitchInfo] = {
    1: SwitchInfo("write-outside-sandbox", "Write outside the sandbox branch"),
    2: SwitchInfo("change-after-approval", "Change altered after approval"),
    3: SwitchInfo("sandbox-rule-broken", "Sandbox rule broken"),
    4: SwitchInfo("outside-connection-while-offline", "Outside connection attempted while offline"),
    5: SwitchInfo("connection-outside-allowed-list", "Connection outside the allowed list"),
    6: SwitchInfo("personal-data-outbound", "Personal data found in outgoing content"),
    7: SwitchInfo("hidden-instruction-acted-on", "Hidden instruction acted on"),
    8: SwitchInfo("unsupported-claim-published", "Unsupported claim published"),
    9: SwitchInfo("restricted-content-disclosed", "Restricted content disclosed"),
    10: SwitchInfo("example-number-published-as-measured", "Example number published as measured"),
    11: SwitchInfo("quality-below-floor", "Quality dropped below the floor"),
}
ROW_BY_SWITCH_ID = {info.switch_id: row for row, info in SWITCHES.items()}

BLOCKS_IN_WORDS = {
    MODEL_CALLS: "Model calls (new runs by Scout and Sentinel)",
    PREPARATION: "Preparing coding tasks",
    PUBLICATION: "Publishing changes and corrections",
    REPORT_DELIVERY: "Delivering reports",
    EXECUTION: "Running coding tasks",
    FULL_OUTPUT: "Full reports (only first-look output stays on)",
}
DRILL_PREREQUISITE = "drill_acknowledged"


class ContainmentError(ValueError):
    pass


class ContainmentStore:
    """Durable containment table implementing the shared ``Containment`` interface."""

    def __init__(self, db: OpsDB, *, deployed_revision: Callable[[], str] = lambda: "unknown",
                 on_void_approvals: Callable[[Any, str, datetime], None] | None = None) -> None:
        self.db = db
        self.deployed_revision = deployed_revision
        self.on_void_approvals = on_void_approvals
        with self.db.transaction() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(incidents)").fetchall()}
            if "drill" not in cols:
                conn.execute("ALTER TABLE incidents ADD COLUMN drill INTEGER NOT NULL DEFAULT 0")

    # -- trips ---------------------------------------------------------------------------------

    def trip(self, row: int, *, producer: str, detail: Mapping[str, Any], now: datetime,
             publication_id: str | None = None, offending_item_id: str | None = None, drill: bool = False) -> str:
        """Trip a switch. A drill takes this same path and really blocks the switch's consumers.

        A drill cannot start while any incident is open on the switch. A real trip while a drill
        is open turns that drill into a real incident (full prerequisites apply). A drill does
        not void approvals: it must be safe to run at any time.
        """
        rule = RULES[row]
        if drill:
            publication_id, offending_item_id = publication_id or "drill", offending_item_id or "drill"
        if row in (8, 10) and not (publication_id and offending_item_id):
            raise ContainmentError("rows 8 and 10 record the publication and the offending item")
        incident_id = new_id("inc")
        with self.db.transaction() as conn:
            open_here = [dict(r) for r in conn.execute(
                "SELECT * FROM incidents WHERE row = ? AND status = 'tripped' ORDER BY tripped_at", (row,)).fetchall()]
            if drill and open_here:
                kind = "a drill" if all(r["drill"] for r in open_here) else "a real incident"
                raise ContainmentError(f"{kind} is already open on this switch")
            open_drill = next((r for r in open_here if r["drill"]), None)
            if not drill and open_drill is not None:
                # A real trip during a drill: the drill becomes a real incident.
                incident_id = open_drill["incident_id"]
                conn.execute(
                    "UPDATE incidents SET drill = 0, producer = ?, detail_json = ?, publication_id = ?,"
                    " offending_item_id = ? WHERE incident_id = ?",
                    (producer, json.dumps(dict(detail, converted_from_drill=True), sort_keys=True, default=str),
                     publication_id, offending_item_id, incident_id))
                conn.execute("DELETE FROM prerequisites WHERE incident_id = ? AND kind = ?",
                             (incident_id, DRILL_PREREQUISITE))
            else:
                conn.execute(
                    "INSERT INTO incidents (incident_id, row, trigger, producer, detail_json, publication_id,"
                    " offending_item_id, status, tripped_at, drill) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (incident_id, row, rule.trigger, producer,
                     json.dumps(dict(detail), sort_keys=True, default=str),
                     publication_id, offending_item_id, "tripped", iso(now), int(drill)),
                )
            if rule.voids_pending_approvals and not drill:
                conn.execute(
                    "UPDATE approvals SET status = 'voided', voided_at = ?, void_reason = ? WHERE status = 'granted'",
                    (iso(now), f"containment incident {incident_id}"),
                )
                if self.on_void_approvals:
                    self.on_void_approvals(conn, incident_id, now)
            self.db.emit_event(conn, "containment.tripped", at=now, run_id=detail.get("run_id"), generation=None,
                               data={"incident_id": incident_id, "row": row, "trigger": rule.trigger,
                                     "switch_id": SWITCHES[row].switch_id, "title": SWITCHES[row].title,
                                     "drill": bool(drill), "producer": producer, "blocks": list(rule.blocks)})
        return incident_id

    def last_tripped(self, row: int) -> str | None:
        r = self.db.one("SELECT MAX(tripped_at) AS at FROM incidents WHERE row = ?", (row,))
        return r["at"] if r else None

    # -- consumers -----------------------------------------------------------------------------

    def open_incidents(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM incidents WHERE status = 'tripped' ORDER BY tripped_at")]

    def permits(self, consumer: str) -> EligibilityResult:
        if consumer not in CONSUMERS:
            raise ContainmentError(f"unknown consumer {consumer}")
        reasons = tuple(
            (f"kill switch {SWITCHES[r['row']].title} (drill)" if r["drill"]
             else f"containment incident {r['incident_id']} ({r['trigger']}) blocks {consumer}")
            for r in self.open_incidents() if consumer in RULES[r["row"]].blocks
        )
        return EligibilityResult(not reasons, reasons)

    def incident(self, incident_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
        if row is None:
            raise KeyError(incident_id)
        out = dict(row)
        rule = RULES[row["row"]]
        out["drill"] = bool(out.get("drill"))
        out["required_prerequisites"] = [DRILL_PREREQUISITE] if out["drill"] else list(rule.prerequisites)
        out["blocks"] = list(rule.blocks)
        out["prerequisites"] = [
            dict(p) for p in self.db.query("SELECT * FROM prerequisites WHERE incident_id = ? ORDER BY created_at", (incident_id,))
        ]
        out["corrections"] = [
            {"correction_id": c["correction_id"], "state": c["state"], "reason": c["reason"]}
            for c in self.db.query(
                "SELECT c.* FROM corrections c JOIN correction_authorisations a USING (authorisation_id)"
                " WHERE a.incident_id = ? ORDER BY c.at", (incident_id,))
        ]
        return out

    # -- prerequisites -------------------------------------------------------------------------

    def record_prerequisite(self, incident_id: str, kind: str, *, produced_by: str,
                            detail: Mapping[str, Any], now: datetime, conn: Any = None) -> str:
        """Record a prerequisite. The general endpoint (``produced_by='api'``) may only create
        the generic kinds; correction and run kinds come only from their producing operation."""
        if kind in RESTRICTED_KINDS:
            if produced_by != RESTRICTED_KINDS[kind]:
                raise ContainmentError(f"a {kind} record can only be produced by {RESTRICTED_KINDS[kind]}")
        elif kind not in GENERIC_KINDS:
            raise ContainmentError(f"unknown prerequisite kind {kind}")
        record_id = new_id("pre")
        with self.db.transaction() as c:
            c = conn or c
            if c.execute("SELECT 1 FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone() is None:
                raise ContainmentError("unknown incident")
            c.execute(
                "INSERT INTO prerequisites (record_id, kind, incident_id, produced_by, detail_json, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (record_id, kind, incident_id, produced_by, json.dumps(dict(detail), sort_keys=True, default=str), iso(now)),
            )
        return record_id

    def resume(self, incident_id: str, prerequisite_ids: Sequence[str], *, role: str, now: datetime) -> None:
        if role != "audit":
            raise ContainmentError("only the audit role may resume (standing in for the product owner)")
        inc = self.db.one("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
        if inc is None:
            raise ContainmentError("unknown incident")
        if inc["status"] != "tripped":
            raise ContainmentError("incident is not tripped")
        rule = RULES[inc["row"]]
        if inc["drill"]:
            # A drill ends on the audit role's confirmation alone; the acknowledgement is recorded.
            with self.db.transaction() as conn:
                ack = self.record_prerequisite(incident_id, DRILL_PREREQUISITE, produced_by="drill",
                                               detail={"role": role}, now=now, conn=conn)
                conn.execute("UPDATE incidents SET status = 'resumed', resumed_at = ? WHERE incident_id = ?",
                             (iso(now), incident_id))
                self.db.emit_event(conn, "containment.resumed", at=now, run_id=None, generation=None,
                                   data={"incident_id": incident_id, "row": rule.row, "drill": True,
                                         "switch_id": SWITCHES[rule.row].switch_id, "prerequisites": [ack],
                                         "signed_off_by": "audit"})
            return
        satisfied: set[str] = set()
        problems: list[str] = []
        for pid in prerequisite_ids:
            rec = self.db.one("SELECT * FROM prerequisites WHERE record_id = ?", (pid,))
            if rec is None:
                problems.append(f"{pid}: no such record")
                continue
            if rec["incident_id"] != incident_id:
                problems.append(f"{pid}: belongs to another incident")
                continue
            kind = rec["kind"]
            detail = json.loads(rec["detail_json"])
            if kind in RESTRICTED_KINDS and rec["produced_by"] != RESTRICTED_KINDS[kind]:
                problems.append(f"{pid}: {kind} not produced by {RESTRICTED_KINDS[kind]}")
                continue
            if kind == "correction":
                if detail.get("publication_id") != inc["publication_id"] or detail.get("offending_item_id") != inc["offending_item_id"]:
                    problems.append(f"{pid}: correction is for another publication or item")
                    continue
                if not detail.get("verified"):
                    problems.append(f"{pid}: correction not verified")
                    continue
            if kind in REQUIRED_SUITES:
                if detail.get("suite") != REQUIRED_SUITES[kind]:
                    problems.append(f"{pid}: wrong suite")
                    continue
                if detail.get("revision") != self.deployed_revision():
                    problems.append(f"{pid}: run is not at the deployed revision")
                    continue
                if not detail.get("passed"):
                    problems.append(f"{pid}: run did not pass")
                    continue
            satisfied.add(kind)
        missing = [k for k in rule.prerequisites if k not in satisfied]
        if missing or problems:
            raise ContainmentError("; ".join(problems + [f"missing prerequisite: {m}" for m in missing]))
        with self.db.transaction() as conn:
            conn.execute("UPDATE incidents SET status = 'resumed', resumed_at = ? WHERE incident_id = ?", (iso(now), incident_id))
            self.db.emit_event(conn, "containment.resumed", at=now, run_id=None, generation=None,
                               data={"incident_id": incident_id, "row": rule.row, "prerequisites": list(prerequisite_ids),
                                     "signed_off_by": "audit (standing in for the product owner)"})
