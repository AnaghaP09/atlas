"""Corrections: the narrow write allowed while publication is paused for a published error.

A correction authorisation (audit role, one incident) binds the publication, the offending item,
the exact replacement text and the expected current body hash, and expires after 24 hours. It
allows one thing: replacing the whole body of that publication with the bound text. No comment,
push, branch or new pull request; no Approval B needed.

Execution is serialised per publication. The live body is read first; if its hash differs from
the expected hash the write is refused as stale. An intent row is committed before the single
remote write, which is never retried automatically. After an uncertain outcome the correction is
held: reconciliation reads the body and records success only if it equals the authorised text.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta
from typing import Any, Protocol

from atlas.containment import ContainmentError, ContainmentStore
from atlas.execution.checks import personal_data_scan, secret_scan
from atlas.execution.db import OpsDB, iso, new_id, parse_iso
from atlas.execution.ports import PublicationAPI

AUTHORISATION_LIFETIME = timedelta(hours=24)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class BodyTarget(Protocol):
    def read(self, publication_id: str) -> str: ...

    def replace(self, publication_id: str, text: str) -> None: ...


class PullRequestBodies:
    """Publication bodies of draft pull requests: ``pr:<owner>/<repo>#<number>``."""

    def __init__(self, api: PublicationAPI) -> None:
        self.api = api

    @staticmethod
    def parse(publication_id: str) -> tuple[str, int]:
        kind, rest = publication_id.split(":", 1)
        if kind != "pr":
            raise ValueError("not a pull-request publication")
        repo, number = rest.rsplit("#", 1)
        return repo, int(number)

    def read(self, publication_id: str) -> str:
        repo, number = self.parse(publication_id)
        return self.api.get_pull(repo, number).body

    def replace(self, publication_id: str, text: str) -> None:
        repo, number = self.parse(publication_id)
        self.api.replace_pull_body(repo, number, text)


class CorrectionError(ValueError):
    pass


class CorrectionService:
    def __init__(self, db: OpsDB, containment: ContainmentStore, bodies: BodyTarget, notifier: Any = None) -> None:
        self.db = db
        self.containment = containment
        self.bodies = bodies
        self.notifier = notifier
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, publication_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(publication_id, threading.Lock())

    def publication_project(self, publication_id: str) -> str:
        """The stored project of a publication: the project of the task that published it."""
        try:
            repo, number = PullRequestBodies.parse(publication_id)
        except Exception:  # noqa: BLE001 - not a pull-request publication
            return ""
        row = self.db.one("SELECT project_id FROM tasks WHERE lower(destination_repo) = lower(?) AND pr_number = ?",
                          (repo, number))
        return (row["project_id"] or "") if row else ""

    @staticmethod
    def _check_project(stored: str, caller_project: str | None) -> None:
        """A caller acting in another project is told the object does not exist."""
        if caller_project is not None and stored and caller_project != stored and caller_project != "*":
            raise CorrectionError("unknown incident")

    def _incident(self, incident_id: str) -> dict[str, Any]:
        inc = self.db.one("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,))
        if inc is None:
            raise CorrectionError("unknown incident")
        if inc["status"] != "tripped" or inc["row"] not in (8, 10):
            raise CorrectionError("corrections are only for an open published-error incident")
        return dict(inc)

    def preauthorisation_read(self, incident_id: str, *, role: str, project_id: str | None = None) -> dict[str, str]:
        """Read the recorded publication's current body to obtain the expected hash. No write."""
        if role != "audit":
            raise CorrectionError("only the audit role may read for a correction")
        inc = self._incident(incident_id)
        self._check_project(self.publication_project(inc["publication_id"]), project_id)
        body = self.bodies.read(inc["publication_id"])
        return {"publication_id": inc["publication_id"], "body": body, "body_sha256": sha256(body)}

    def authorise(self, incident_id: str, *, replacement_text: str, expected_body_sha256: str, role: str,
                  now: datetime, idempotency_key: str | None = None, project_id: str | None = None) -> str:
        """Authorise one correction. The authorisation stores the publication's project; a caller
        acting in another project is refused before anything is written."""
        if role != "audit":
            raise CorrectionError("only the audit role may authorise a correction")
        if not replacement_text.strip():
            raise CorrectionError("the replacement text is empty")
        for check in (secret_scan(replacement_text), personal_data_scan(replacement_text)):
            if check.status != "passed":
                raise CorrectionError(f"replacement text failed {check.name}")
        inc = self._incident(incident_id)
        stored_project = self.publication_project(inc["publication_id"])
        self._check_project(stored_project, project_id)
        if idempotency_key is not None and stored_project:
            idempotency_key = f"{stored_project}::{idempotency_key}"
        request = {"op": "correction_authorise", "incident_id": incident_id, "text": sha256(replacement_text),
                   "expected": expected_body_sha256}
        with self.db.transaction() as conn:
            stored = self.db.idempotent(conn, idempotency_key, request)
            if stored is not None:
                return stored["authorisation_id"]
            auth_id = new_id("cauth")
            conn.execute(
                "INSERT INTO correction_authorisations (authorisation_id, incident_id, publication_id, offending_item_id,"
                " replacement_text, replacement_sha256, expected_body_sha256, created_at, expires_at, project_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (auth_id, incident_id, inc["publication_id"], inc["offending_item_id"], replacement_text,
                 sha256(replacement_text), expected_body_sha256, iso(now), iso(now + AUTHORISATION_LIFETIME),
                 stored_project),
            )
            self.db.remember(conn, idempotency_key, request, {"authorisation_id": auth_id})
        return auth_id

    def list(self, incident_id: str, project_id: str | None = None) -> list[dict[str, Any]]:
        return [
            {"correction_id": r["correction_id"], "state": r["state"], "reason": r["reason"],
             "publication_id": r["publication_id"], "project_id": r["project_id"] or None}
            for r in self.db.query(
                "SELECT c.* FROM corrections c JOIN correction_authorisations a USING (authorisation_id)"
                " WHERE a.incident_id = ? ORDER BY c.at", (incident_id,))
            if project_id in (None, "*") or not r["project_id"] or r["project_id"] == project_id
        ]

    def _set(self, correction_id: str, state: str, reason: str | None, now: datetime, body_version: int | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE corrections SET state = ?, reason = ?, body_version = COALESCE(?, body_version), at = ?"
                         " WHERE correction_id = ?", (state, reason, body_version, iso(now), correction_id))

    def _finish(self, correction_id: str, auth: dict[str, Any], now: datetime) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT body_version FROM publication_bodies WHERE publication_id = ?",
                               (auth["publication_id"],)).fetchone()
            version = (row["body_version"] if row else 0) + 1
            conn.execute("INSERT INTO publication_bodies (publication_id, body_version) VALUES (?, ?)"
                         " ON CONFLICT(publication_id) DO UPDATE SET body_version = excluded.body_version",
                         (auth["publication_id"], version))
            conn.execute("UPDATE corrections SET state = 'done', reason = NULL, body_version = ?, at = ? WHERE correction_id = ?",
                         (version, iso(now), correction_id))
            record_id = self.containment.record_prerequisite(
                auth["incident_id"], "correction", produced_by="corrections", now=now, conn=conn,
                detail={"correction_id": correction_id, "publication_id": auth["publication_id"],
                        "offending_item_id": auth["offending_item_id"], "replacement_sha256": auth["replacement_sha256"],
                        "body_version": version, "verified": True},
            )
            self.db.emit_event(conn, "report.corrected", at=now, run_id=None, generation=None, correction_version=version,
                               data={"publication_id": auth["publication_id"], "offending_item_id": auth["offending_item_id"],
                                     "correction_id": correction_id, "incident_id": auth["incident_id"]})
        return {"correction_id": correction_id, "state": "done", "verified_record_id": record_id, "body_version": version}

    def execute(self, authorisation_id: str, *, now: datetime, project_id: str | None = None) -> dict[str, Any]:
        auth_row = self.db.one("SELECT * FROM correction_authorisations WHERE authorisation_id = ?", (authorisation_id,))
        if auth_row is None:
            raise CorrectionError("unknown correction authorisation")
        auth = dict(auth_row)
        if project_id is not None and auth.get("project_id") and project_id not in (auth["project_id"], "*"):
            raise CorrectionError("unknown correction authorisation")
        with self._lock(auth["publication_id"]):
            existing = self.db.one("SELECT * FROM corrections WHERE authorisation_id = ?", (authorisation_id,))
            if existing is not None:
                if existing["state"] == "done":
                    rec = self.db.one("SELECT record_id FROM prerequisites WHERE kind = 'correction' AND detail_json LIKE ?",
                                      (f'%"{existing["correction_id"]}"%',))
                    return {"correction_id": existing["correction_id"], "state": "done",
                            "verified_record_id": rec["record_id"] if rec else None,
                            "body_version": existing["body_version"]}
                if existing["state"] in ("intent", "held"):
                    return self.reconcile(existing["correction_id"], now=now)
                return {"correction_id": existing["correction_id"], "state": existing["state"], "reason": existing["reason"]}
            if parse_iso(auth["expires_at"]) <= now:  # type: ignore[operator]
                raise CorrectionError("correction authorisation expired")
            inc = self.db.one("SELECT status FROM incidents WHERE incident_id = ?", (auth["incident_id"],))
            if inc is None or inc["status"] != "tripped":
                raise CorrectionError("the incident is no longer open")
            live = self.bodies.read(auth["publication_id"])
            if sha256(live) != auth["expected_body_sha256"]:
                raise CorrectionError("stale: the publication body changed since this correction was authorised; "
                                      "authorise a combined correction from the current body")
            correction_id = new_id("corr")
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO corrections (correction_id, authorisation_id, publication_id, state, at, project_id)"
                             " VALUES (?,?,?,?,?,?)", (correction_id, authorisation_id, auth["publication_id"], "intent",
                                                     iso(now), auth.get("project_id") or ""))
            try:
                self.bodies.replace(auth["publication_id"], auth["replacement_text"])
            except Exception as exc:  # noqa: BLE001 - uncertain outcome: hold, never re-send
                self._set(correction_id, "held", f"uncertain outcome: {type(exc).__name__}", now)
                if self.notifier is not None:
                    self.notifier.emit("correction.held", correction_id, [correction_id], frozenset({"public"}))
                return {"correction_id": correction_id, "state": "held", "reason": "uncertain outcome"}
            return self._finish(correction_id, auth, now)

    def reconcile(self, correction_id: str, *, now: datetime) -> dict[str, Any]:
        """Read the body: equal to the authorised text -> done; otherwise stay held. Never re-sends."""
        row = self.db.one("SELECT * FROM corrections WHERE correction_id = ?", (correction_id,))
        if row is None:
            raise CorrectionError("unknown correction")
        auth = dict(self.db.one("SELECT * FROM correction_authorisations WHERE authorisation_id = ?", (row["authorisation_id"],)))
        if row["state"] == "done":
            return {"correction_id": correction_id, "state": "done"}
        try:
            body = self.bodies.read(auth["publication_id"])
        except Exception as exc:  # noqa: BLE001
            self._set(correction_id, "held", f"reconciliation read failed: {type(exc).__name__}", now)
            return {"correction_id": correction_id, "state": "held", "reason": "reconciliation read failed"}
        if sha256(body) == auth["replacement_sha256"]:
            return self._finish(correction_id, auth, now)
        self._set(correction_id, "held", "the authorised text is not visible; a person must decide", now)
        return {"correction_id": correction_id, "state": "held", "reason": "authorised text not visible"}
