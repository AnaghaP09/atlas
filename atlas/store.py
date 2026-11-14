"""Evidence store: SQLite files, one per immutable snapshot plus one live store.

Rules enforced here:

* one authoritative writer per source; any other writer is rejected and logged;
* records are keyed by (source, source_id, content_version_id) and never modified;
* where a source has a monotonic revision, an older revision never replaces a newer one (it is
  kept as history);
* commits are identities, not an ordering; branch movements are ref events, and a force push
  marks evidence tied to orphaned commits as superseded;
* repeated deliveries of the same source event are no-ops (de-duplicated by event key);
* two sources that disagree about one fact create an ``insufficient_evidence`` node naming both
  records; conflicts are attached to every read of either record;
* derived records carry the union of the access scopes of everything in their lineage.

The action-scoped stores for task security evidence and post-publication validation evidence
live in :mod:`atlas.action_store` style tables in this module (``ActionStore``); they are never a
snapshot and investigation code has no path to them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from atlas.contracts import Provenance, Record, RunContext, RunMode
from atlas.freshness import Freshness, evaluate, fresh_until

AUTHORITATIVE_WRITERS: dict[str, str] = {
    "issues": "connector.issues",
    "git": "connector.git",
    "ci": "connector.ci",
    "security": "connector.security",
    "architecture": "connector.architecture",
    "kubernetes": "connector.kubernetes",
    "telemetry": "connector.telemetry",
    "capacity": "connector.capacity",
    "target_ci": "connector.target_ci",
    "derived": "graph.builder",
}

LIVE_SNAPSHOT_ID = "live"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    record_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT '',
    revision TEXT,
    revision_num INTEGER,
    content_version_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    confirmed_through TEXT,
    fetched_at TEXT,
    freshness_basis TEXT,
    writer TEXT NOT NULL,
    provenance TEXT NOT NULL,
    synthetic_ancestry INTEGER NOT NULL,
    access_scopes TEXT NOT NULL,
    lineage TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    content TEXT,
    commit_sha TEXT,
    event_key TEXT,
    status TEXT NOT NULL DEFAULT 'current',
    superseded_by TEXT,
    seq INTEGER NOT NULL,
    UNIQUE (source, source_id, content_version_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS records_event_key ON records(event_key) WHERE event_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS records_entity ON records(source, source_id);
CREATE TABLE IF NOT EXISTS conflicts (
    node_id TEXT NOT NULL,
    fact TEXT NOT NULL,
    record_a TEXT NOT NULL,
    record_b TEXT NOT NULL,
    PRIMARY KEY (record_a, record_b, fact)
);
CREATE TABLE IF NOT EXISTS ref_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL,
    head TEXT NOT NULL,
    previous_head TEXT,
    forced INTEGER NOT NULL,
    orphaned TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejected_writes (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    writer TEXT NOT NULL,
    record_id TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS confirmations (
    record_id TEXT PRIMARY KEY,
    confirmed_through TEXT NOT NULL,
    method TEXT NOT NULL
);
"""


class WriterRejected(PermissionError):
    """A write from a component that is not the authoritative writer for that source."""


class ReadOnlyStore(PermissionError):
    """A write to a frozen snapshot."""


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("timestamps must carry a time zone")
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scope_union(records: Iterable[Record]) -> frozenset[str]:
    scopes: set[str] = set()
    for record in records:
        scopes |= set(record.access_scopes)
    return frozenset(scopes)


def _revision_num(revision: int | str | None) -> int | None:
    if isinstance(revision, bool):
        return None
    if isinstance(revision, int):
        return revision
    if isinstance(revision, str) and revision.isdigit():
        return int(revision)
    return None


class EvidenceDB:
    """One SQLite evidence file. Read-only files refuse every write."""

    def __init__(self, path: Path, *, read_only: bool = False, live: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        self.live = live and not read_only
        self._lock = threading.RLock()
        if read_only:
            if not self.path.exists():
                raise FileNotFoundError(f"snapshot file not found: {self.path.name}")
            uri = f"file:{self.path.as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise ReadOnlyStore(f"snapshot {self.path.stem} is frozen")
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------ writes

    def put(
        self,
        record: Record,
        writer: str,
        *,
        content: Any = None,
        entity_type: str = "",
        event_key: str | None = None,
        freshness_basis: str | None = None,
        commit_sha: str | None = None,
    ) -> str:
        """Insert one record version. Returns the record_id that now represents it.

        Duplicate deliveries (same event key, or same source, id and content version) are
        no-ops and return the existing record_id.
        """
        expected = AUTHORITATIVE_WRITERS.get(record.source)
        if expected is None or writer != expected or record.writer != writer:
            self._log_rejection(record, writer, "not the authoritative writer for this source")
            raise WriterRejected(f"{writer} may not write source {record.source}")
        with self._tx() as conn:
            if event_key is not None:
                row = conn.execute("SELECT record_id FROM records WHERE event_key = ?", (event_key,)).fetchone()
                if row:
                    return row["record_id"]
            row = conn.execute(
                "SELECT record_id FROM records WHERE source=? AND source_id=? AND content_version_id=?",
                (record.source, record.source_id, record.content_version_id),
            ).fetchone()
            if row:
                return row["record_id"]
            rev_num = _revision_num(record.revision)
            status = "current"
            current = conn.execute(
                "SELECT record_id, revision_num, valid_from FROM records "
                "WHERE source=? AND source_id=? AND status='current'",
                (record.source, record.source_id),
            ).fetchall()
            newer_exists = False
            for existing in current:
                if rev_num is not None and existing["revision_num"] is not None:
                    if existing["revision_num"] > rev_num:
                        newer_exists = True
                elif rev_num is None and existing["revision_num"] is None:
                    if existing["valid_from"] > to_iso(record.valid_from):
                        newer_exists = True
            if newer_exists:
                status = "history"
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM records").fetchone()[0]
            conn.execute(
                "INSERT INTO records (record_id, source, source_id, entity_type, revision, revision_num, "
                "content_version_id, valid_from, observed_at, confirmed_through, fetched_at, freshness_basis, "
                "writer, provenance, synthetic_ancestry, access_scopes, lineage, content_ref, content, "
                "commit_sha, event_key, status, superseded_by, seq) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.record_id,
                    record.source,
                    record.source_id,
                    entity_type,
                    None if record.revision is None else str(record.revision),
                    rev_num,
                    record.content_version_id,
                    to_iso(record.valid_from),
                    to_iso(record.observed_at),
                    to_iso(record.confirmed_through),
                    to_iso(record.fetched_at),
                    freshness_basis,
                    writer,
                    str(record.provenance),
                    int(record.synthetic_ancestry),
                    json.dumps(sorted(record.access_scopes)),
                    json.dumps(list(record.lineage)),
                    record.content_ref,
                    None if content is None else json.dumps(content, sort_keys=True, default=str),
                    commit_sha,
                    event_key,
                    status,
                    None,
                    seq,
                ),
            )
            if status == "current":
                for existing in current:
                    conn.execute(
                        "UPDATE records SET status='history', superseded_by=? WHERE record_id=?",
                        (record.record_id, existing["record_id"]),
                    )
            return record.record_id

    def _log_rejection(self, record: Record, writer: str, reason: str) -> None:
        if self.read_only:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO rejected_writes (source, writer, record_id, reason) VALUES (?,?,?,?)",
                (record.source, writer, record.record_id, reason),
            )
            self._conn.commit()

    def rejected_writes(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT source, writer, record_id, reason FROM rejected_writes ORDER BY seq").fetchall()
        return [dict(r) for r in rows]

    def record_conflict(self, fact: str, record_a: str, record_b: str) -> str:
        """Two records disagree about one fact. Creates (or returns) the insufficient-evidence node."""
        a, b = sorted((record_a, record_b))
        node_id = "ie-" + hashlib.sha256(f"{fact}|{a}|{b}".encode()).hexdigest()[:16]
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conflicts (node_id, fact, record_a, record_b) VALUES (?,?,?,?)",
                (node_id, fact, a, b),
            )
        return node_id

    def ref_event(
        self,
        ref: str,
        head: str,
        *,
        previous_head: str | None,
        forced: bool,
        orphaned_commits: Sequence[str] = (),
        recorded_at: datetime,
    ) -> int:
        """Record a branch movement. A forced update supersedes evidence on orphaned commits."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO ref_events (ref, head, previous_head, forced, orphaned, recorded_at) VALUES (?,?,?,?,?,?)",
                (ref, head, previous_head, int(forced), json.dumps(list(orphaned_commits)), to_iso(recorded_at)),
            )
            seq = int(cur.lastrowid)
            if forced and orphaned_commits:
                marker = f"ref-event:{seq}"
                placeholders = ",".join("?" for _ in orphaned_commits)
                conn.execute(
                    f"UPDATE records SET status='superseded', superseded_by=? WHERE commit_sha IN ({placeholders})",
                    (marker, *orphaned_commits),
                )
            return seq

    def set_meta(self, key: str, value: Any) -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, json.dumps(value)))

    def meta(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    # ------------------------------------------------------------------ reads

    def _row_to_record(self, row: sqlite3.Row) -> Record:
        conflicts = self._conflicts_for(row["record_id"])
        return Record(
            record_id=row["record_id"],
            source=row["source"],
            source_id=row["source_id"],
            revision=row["revision_num"] if row["revision_num"] is not None else row["revision"],
            content_version_id=row["content_version_id"],
            valid_from=from_iso(row["valid_from"]),
            observed_at=from_iso(row["observed_at"]),
            confirmed_through=self._effective_confirmation(row["record_id"], row["confirmed_through"]),
            writer=row["writer"],
            provenance=Provenance(row["provenance"]),
            synthetic_ancestry=bool(row["synthetic_ancestry"]),
            access_scopes=frozenset(json.loads(row["access_scopes"])),
            lineage=tuple(json.loads(row["lineage"])),
            content_ref=row["content_ref"],
            fetched_at=from_iso(row["fetched_at"]),
            conflicts=conflicts,
            superseded_by=row["superseded_by"],
        )

    def _conflicts_for(self, record_id: str) -> tuple[str, ...]:
        rows = self._conn.execute(
            "SELECT node_id, record_a, record_b FROM conflicts WHERE record_a=? OR record_b=? ORDER BY node_id",
            (record_id, record_id),
        ).fetchall()
        out: list[str] = []
        for row in rows:
            other = row["record_b"] if row["record_a"] == record_id else row["record_a"]
            out.extend([row["node_id"], other])
        return tuple(out)

    def _effective_confirmation(self, record_id: str, stored: str | None) -> datetime | None:
        """The stored confirmation, advanced by later live re-confirmations of the same content."""
        base = from_iso(stored)
        row = self._conn.execute("SELECT confirmed_through FROM confirmations WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            return base
        later = from_iso(row["confirmed_through"])
        return later if base is None or later > base else base

    def reconfirm(self, record_id: str, writer: str, confirmed_at: datetime, *, method: str = "fetch") -> datetime:
        """Record that a successful live fetch (or a 304 revalidation) saw this record's state again.

        Only the live store accepts confirmations, only from the source's authorised writer, and
        only for the record that is current for its entity. Confirmations never move backwards.
        Replayed deliveries and failed polls must not call this.
        """
        if not self.live:
            raise ReadOnlyStore("confirmations are accepted only by the live store")
        if method not in ("fetch", "not_modified"):
            raise ValueError("confirmation method must be 'fetch' or 'not_modified'")
        with self._tx() as conn:
            row = conn.execute("SELECT source, status, confirmed_through FROM records WHERE record_id=?", (record_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown record {record_id}")
            if AUTHORITATIVE_WRITERS.get(row["source"]) != writer:
                raise WriterRejected(f"{writer} may not confirm source {row['source']}")
            if row["status"] != "current":
                raise ValueError("only the current version of an entity can be re-confirmed")
            when = to_iso(confirmed_at)
            existing = conn.execute("SELECT confirmed_through FROM confirmations WHERE record_id=?", (record_id,)).fetchone()
            if existing is None or existing["confirmed_through"] < when:
                conn.execute(
                    "INSERT INTO confirmations (record_id, confirmed_through, method) VALUES (?,?,?) "
                    "ON CONFLICT(record_id) DO UPDATE SET confirmed_through=excluded.confirmed_through, method=excluded.method",
                    (record_id, when, method),
                )
        return self._effective_confirmation(record_id, row["confirmed_through"])

    def get(self, source: str, source_id: str) -> Record | None:
        row = self._conn.execute(
            "SELECT * FROM records WHERE source=? AND source_id=? AND status='current' ORDER BY seq DESC LIMIT 1",
            (source, source_id),
        ).fetchone()
        return None if row is None else self._row_to_record(row)

    def by_id(self, record_id: str) -> Record | None:
        row = self._conn.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        return None if row is None else self._row_to_record(row)

    def content(self, record_id: str) -> Any:
        row = self._conn.execute("SELECT content FROM records WHERE record_id=?", (record_id,)).fetchone()
        if row is None or row["content"] is None:
            return None
        return json.loads(row["content"])

    def meta_of(self, record_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT entity_type, freshness_basis, commit_sha, status, event_key FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return {} if row is None else dict(row)

    def history(self, source: str, source_id: str) -> list[Record]:
        rows = self._conn.execute(
            "SELECT * FROM records WHERE source=? AND source_id=? "
            "ORDER BY COALESCE(revision_num, 0), valid_from, seq",
            (source, source_id),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def search(self, query: Mapping[str, Any]) -> list[Record]:
        clauses = ["status='current'"]
        params: list[Any] = []
        for key in ("source", "entity_type", "source_id"):
            if query.get(key) is not None:
                clauses.append(f"{key}=?")
                params.append(query[key])
        if query.get("text"):
            clauses.append("content LIKE ?")
            params.append(f"%{query['text']}%")
        if query.get("include_history"):
            clauses[0] = "1=1"
        limit = int(query.get("limit", 500))
        sql = f"SELECT * FROM records WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?"
        rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [self._row_to_record(r) for r in rows]

    def insufficient_evidence(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT node_id, fact, record_a, record_b FROM conflicts ORDER BY node_id").fetchall()
        return [dict(r) for r in rows]

    def all_records(self) -> list[Record]:
        rows = self._conn.execute("SELECT * FROM records ORDER BY seq").fetchall()
        return [self._row_to_record(r) for r in rows]

    def export_rows(self) -> list[dict[str, Any]]:
        """Every stored row as plain JSON-ready data (used to build exports)."""
        rows = self._conn.execute("SELECT * FROM records ORDER BY seq").fetchall()
        return [dict(r) for r in rows]

    def import_rows(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Load rows produced by :meth:`export_rows` (snapshot building only)."""
        columns = [
            "record_id", "source", "source_id", "entity_type", "revision", "revision_num", "content_version_id",
            "valid_from", "observed_at", "confirmed_through", "fetched_at", "freshness_basis", "writer",
            "provenance", "synthetic_ancestry", "access_scopes", "lineage", "content_ref", "content",
            "commit_sha", "event_key", "status", "superseded_by", "seq",
        ]
        with self._tx() as conn:
            for row in rows:
                conn.execute(
                    f"INSERT INTO records ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    tuple(row.get(c) for c in columns),
                )

    def import_conflicts(self, rows: Iterable[Mapping[str, Any]]) -> None:
        with self._tx() as conn:
            for row in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO conflicts (node_id, fact, record_a, record_b) VALUES (?,?,?,?)",
                    (row["node_id"], row["fact"], row["record_a"], row["record_b"]),
                )

    def freshness(self, record: Record, evidence_clock: datetime) -> Freshness:
        return evaluate(record.source, record.confirmed_through, evidence_clock)


class EvidenceStore:
    """The serving process's view of evidence. Every read takes a RunContext.

    There is no global store handle: each read opens (and caches) only the file named by the
    context's snapshot id. Writes go only to the live store.
    """

    def __init__(self, registry: "SnapshotRegistryLike") -> None:
        self._registry = registry

    def put(self, record: Record, writer: str, **kwargs: Any) -> str:
        return self._registry.live().put(record, writer, **kwargs)

    def reconfirm(self, record_id: str, writer: str, confirmed_at: datetime, *, method: str = "fetch") -> datetime:
        """Advance a live record's confirmation after a successful fetch or 304 revalidation."""
        return self._registry.live().reconfirm(record_id, writer, confirmed_at, method=method)

    def current_live(self, source: str, source_id: str) -> Record | None:
        return self._registry.live().get(source, source_id)

    def _db(self, ctx: RunContext) -> EvidenceDB:
        return self._registry.open(ctx)

    def get(self, ctx: RunContext, source: str, source_id: str) -> Record | None:
        return self._db(ctx).get(source, source_id)

    def history(self, ctx: RunContext, source: str, source_id: str) -> list[Record]:
        return self._db(ctx).history(source, source_id)

    def search(self, ctx: RunContext, query: Mapping[str, Any]) -> list[Record]:
        return self._db(ctx).search(query)

    def content(self, ctx: RunContext, record_id: str) -> Any:
        return self._db(ctx).content(record_id)

    def by_id(self, ctx: RunContext, record_id: str) -> Record | None:
        return self._db(ctx).by_id(record_id)

    def freshness(self, ctx: RunContext, record: Record) -> Freshness:
        return evaluate(record.source, record.confirmed_through, ctx.evidence_clock)

    def fresh_until(self, record: Record) -> datetime | None:
        return fresh_until(record.source, record.confirmed_through)


class SnapshotRegistryLike:  # pragma: no cover - structural type for documentation
    def live(self) -> EvidenceDB: ...

    def open(self, ctx: RunContext) -> EvidenceDB: ...


def derived_record(
    *,
    record_id: str,
    source_id: str,
    lineage: Sequence[Record],
    valid_from: datetime,
    content_ref: str,
) -> Record:
    """A derived record: scopes are the union over lineage; synthetic if any input is."""
    return Record(
        record_id=record_id,
        source="derived",
        source_id=source_id,
        revision=None,
        content_version_id=record_id,
        valid_from=valid_from,
        observed_at=valid_from,
        confirmed_through=None,
        writer=AUTHORITATIVE_WRITERS["derived"],
        provenance=Provenance.DERIVED,
        synthetic_ancestry=any(r.synthetic_ancestry or r.provenance is Provenance.SYNTHETIC for r in lineage),
        access_scopes=scope_union(lineage),
        lineage=tuple(r.record_id for r in lineage),
        content_ref=content_ref,
    )


def with_writer(record: Record, writer: str) -> Record:
    return replace(record, writer=writer)


def is_live(ctx: RunContext) -> bool:
    return ctx.mode is RunMode.LIVE and ctx.snapshot_id == LIVE_SNAPSHOT_ID


# ---------------------------------------------------------------------------
# Action-scoped stores: task security evidence and post-publication validation evidence
# ---------------------------------------------------------------------------

_ACTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_runs (
    scan_run_id TEXT PRIMARY KEY,
    destination TEXT NOT NULL, action_id TEXT NOT NULL, revision TEXT NOT NULL,
    inventory TEXT NOT NULL, per_module TEXT NOT NULL, completion TEXT NOT NULL,
    coverage_ok INTEGER NOT NULL, coverage_total INTEGER NOT NULL,
    observed_at TEXT NOT NULL, confirmed_through TEXT NOT NULL, fresh_until TEXT NOT NULL,
    finding_ids TEXT NOT NULL, seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY, scan_run_id TEXT NOT NULL, content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ci_observations (
    destination TEXT NOT NULL, action_id TEXT NOT NULL, published_commit TEXT NOT NULL,
    job_id TEXT NOT NULL, run_attempt INTEGER NOT NULL, observation_version TEXT NOT NULL,
    name TEXT, status TEXT, conclusion TEXT, started_at TEXT, completed_at TEXT, head_sha TEXT,
    arrival INTEGER NOT NULL, observed_at TEXT NOT NULL,
    PRIMARY KEY (destination, action_id, published_commit, job_id, run_attempt, observation_version)
);
CREATE TABLE IF NOT EXISTS validation_evidence (
    evidence_id TEXT PRIMARY KEY,
    destination TEXT NOT NULL, action_id TEXT NOT NULL, published_commit TEXT NOT NULL,
    kind TEXT NOT NULL, content TEXT NOT NULL, writer TEXT NOT NULL, recorded_at TEXT NOT NULL, seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS poll_leases (key TEXT PRIMARY KEY, holder TEXT NOT NULL, taken_at TEXT NOT NULL);
"""

SECURITY_PARTITION_WRITER = "connector.security"
SECURITY_PARTITION_READERS = frozenset({"security_gate"})
TARGET_CI_WRITER = "connector.target_ci"
VALIDATION_EVIDENCE_WRITER = "execution"
VALIDATION_EVIDENCE_KINDS = frozenset({"check_result", "behavioural_check", "human_adjudication"})
VALIDATION_READERS = frozenset({"execution", "scorecard", "reassessment"})

STATUS_RANK = {"queued": 0, "waiting": 0, "pending": 0, "requested": 0, "in_progress": 1, "completed": 2}


class ActionStore:
    """Pre-publication task security evidence and post-publication validation evidence.

    Never a snapshot: investigation tools take a RunContext and have no path here. Reads need a
    SecurityContext or ValidationContext and a permitted reader; writes need the one authorised
    writer for the partition.
    """

    def __init__(self, path: Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_ACTION_SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()

    def close(self) -> None:
        self._conn.close()

    def _seq(self, table: str) -> int:
        return self._conn.execute(f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table}").fetchone()[0]

    # ------------------------------------------------------------------ security partition

    def write_scan_run(self, ctx: Any, writer: str, scan_run: Mapping[str, Any], findings: Sequence[Mapping[str, Any]]) -> str:
        if writer != SECURITY_PARTITION_WRITER:
            raise WriterRejected(f"{writer} may not write task security evidence")
        if (ctx.destination_repo, ctx.action_id, ctx.scanned_revision) != (
            scan_run["destination"], scan_run["action_id"], scan_run["revision"]
        ):
            raise WriterRejected("scan run does not belong to this task context")
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO scan_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scan_run["scan_run_id"], scan_run["destination"], scan_run["action_id"], scan_run["revision"],
                    json.dumps(scan_run["inventory"]), json.dumps(scan_run["per_module"]), scan_run["completion"],
                    scan_run["coverage_ok"], scan_run["coverage_total"], to_iso(scan_run["observed_at"]),
                    to_iso(scan_run["confirmed_through"]), to_iso(scan_run["fresh_until"]),
                    json.dumps([f["finding_id"] for f in findings]), self._seq("scan_runs"),
                ),
            )
            for f in findings:
                self._conn.execute(
                    "INSERT OR IGNORE INTO findings VALUES (?,?,?)",
                    (f["finding_id"], scan_run["scan_run_id"], json.dumps(dict(f), sort_keys=True, default=str)),
                )
            self._conn.commit()
        return scan_run["scan_run_id"]

    def scan_runs(self, ctx: Any, reader: str) -> list[dict[str, Any]]:
        if reader not in SECURITY_PARTITION_READERS:
            raise PermissionError(f"{reader} may not read task security evidence")
        rows = self._conn.execute(
            "SELECT * FROM scan_runs WHERE destination=? AND action_id=? AND revision=? ORDER BY seq",
            (ctx.destination_repo, ctx.action_id, ctx.scanned_revision),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("inventory", "per_module", "finding_ids"):
                d[k] = json.loads(d[k])
            for k in ("observed_at", "confirmed_through", "fresh_until"):
                d[k] = from_iso(d[k])
            d["findings"] = [
                json.loads(f["content"])
                for f in self._conn.execute("SELECT content FROM findings WHERE scan_run_id=?", (d["scan_run_id"],))
            ]
            out.append(d)
        return out

    # ------------------------------------------------------------------ validation partition

    @staticmethod
    def observation_version(status: str | None, conclusion: str | None, started_at: str | None, completed_at: str | None) -> str:
        blob = json.dumps([status, conclusion, started_at, completed_at], separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    def write_ci_observation(self, ctx: Any, writer: str, obs: Mapping[str, Any], observed_at: datetime) -> bool:
        """Store one poll observation. Returns False for a repeated identical observation."""
        if writer != TARGET_CI_WRITER:
            raise WriterRejected(f"{writer} may not write CI observations")
        head = obs.get("head_sha")
        if head and not (str(head).startswith(ctx.published_commit) or ctx.published_commit.startswith(str(head))):
            raise WriterRejected("observation is for a different commit")
        version = self.observation_version(obs.get("status"), obs.get("conclusion"), obs.get("started_at"), obs.get("completed_at"))
        with self._lock:
            arrival = self._conn.execute("SELECT COALESCE(MAX(arrival), 0) + 1 FROM ci_observations").fetchone()[0]
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO ci_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ctx.destination_repo, ctx.action_id, ctx.published_commit, str(obs["job_id"]),
                    int(obs.get("run_attempt") or 1), version, obs.get("name"), obs.get("status"),
                    obs.get("conclusion"), obs.get("started_at"), obs.get("completed_at"), head, arrival,
                    to_iso(observed_at),
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def ci_observations(self, ctx: Any, reader: str) -> list[dict[str, Any]]:
        if reader not in VALIDATION_READERS:
            raise PermissionError(f"{reader} may not read validation evidence")
        rows = self._conn.execute(
            "SELECT * FROM ci_observations WHERE destination=? AND action_id=? AND published_commit=? ORDER BY arrival",
            (ctx.destination_repo, ctx.action_id, ctx.published_commit),
        ).fetchall()
        return [dict(r) for r in rows]

    def effective_states(self, ctx: Any, reader: str) -> dict[tuple[str, int], dict[str, Any]]:
        """Effective state per (job, attempt): highest status rank wins; completed is never replaced."""
        best: dict[tuple[str, int], dict[str, Any]] = {}
        for obs in self.ci_observations(ctx, reader):
            key = (obs["job_id"], obs["run_attempt"])
            current = best.get(key)
            if current is None or _rank_key(obs) > _rank_key(current):
                best[key] = obs
        return best

    def write_evidence(self, ctx: Any, writer: str, kind: str, content: Mapping[str, Any], recorded_at: datetime) -> str:
        if writer != VALIDATION_EVIDENCE_WRITER:
            raise WriterRejected(f"{writer} may not write validation evidence")
        if kind not in VALIDATION_EVIDENCE_KINDS:
            raise ValueError(f"unknown validation evidence kind {kind}")
        if "state" in content or "lifecycle_state" in content:
            raise ValueError("task lifecycle state belongs in the task store, not in validation evidence")
        evidence_id = "ve-" + hashlib.sha256(
            json.dumps([ctx.destination_repo, ctx.action_id, ctx.published_commit, kind, dict(content)], sort_keys=True, default=str).encode()
        ).hexdigest()[:20]
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO validation_evidence VALUES (?,?,?,?,?,?,?,?,?)",
                (evidence_id, ctx.destination_repo, ctx.action_id, ctx.published_commit, kind,
                 json.dumps(dict(content), sort_keys=True, default=str), writer, to_iso(recorded_at),
                 self._seq("validation_evidence")),
            )
            self._conn.commit()
        return evidence_id

    def evidence(self, ctx: Any, reader: str, kind: str | None = None) -> list[dict[str, Any]]:
        if reader not in VALIDATION_READERS:
            raise PermissionError(f"{reader} may not read validation evidence")
        sql = "SELECT * FROM validation_evidence WHERE destination=? AND action_id=? AND published_commit=?"
        params: list[Any] = [ctx.destination_repo, ctx.action_id, ctx.published_commit]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        rows = self._conn.execute(sql + " ORDER BY seq", params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["content"] = json.loads(d["content"])
            out.append(d)
        return out

    def has_lifecycle_state(self) -> bool:
        """True if any validation evidence carries task lifecycle state (it must not)."""
        for row in self._conn.execute("SELECT content FROM validation_evidence"):
            c = json.loads(row["content"])
            if "state" in c or "lifecycle_state" in c:
                return True
        return False

    # ------------------------------------------------------------------ polling leases

    def acquire_poll(self, ctx: Any, holder: str) -> bool:
        key = f"{ctx.destination_repo}|{ctx.action_id}|{ctx.published_commit}"
        with self._lock:
            try:
                self._conn.execute("INSERT INTO poll_leases VALUES (?,?,?)", (key, holder, datetime.now(timezone.utc).isoformat()))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def release_poll(self, ctx: Any, holder: str) -> None:
        key = f"{ctx.destination_repo}|{ctx.action_id}|{ctx.published_commit}"
        with self._lock:
            self._conn.execute("DELETE FROM poll_leases WHERE key=? AND holder=?", (key, holder))
            self._conn.commit()


def _rank_key(obs: Mapping[str, Any]) -> tuple[int, int, str, int]:
    rank = STATUS_RANK.get((obs.get("status") or "").lower(), 0)
    has_completed = 1 if obs.get("completed_at") else 0
    when = obs.get("completed_at") or obs.get("started_at") or ""
    # Among otherwise equal observations the earlier arrival wins.
    return (rank, has_completed, when, -int(obs.get("arrival", 0)))
