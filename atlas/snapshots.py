"""Snapshot registry: the live store, frozen snapshots, and safe content resolution.

* The live store receives live ingestion only.
* Freezing copies the live store into a new SQLite file, records its SHA256 and marks it read
  only. Frozen files are never written again.
* Prepared snapshots (golden cases, release closes) are committed as a portable export
  (``records.jsonl`` plus ``snapshot.json``) and built into a read-only SQLite file on first use;
  the build is deterministic and its SHA256 is checked against the export manifest.
* Every read opens only the file named by the run's snapshot id; caches are keyed by snapshot id.
* Content files are resolved only below the snapshots root; traversal, absolute paths, symlinks
  and alternate separators are rejected.
* Projects are kept apart: each project has its own live store (``stores/<project>/live.sqlite``),
  frozen snapshot ids start with the project id, and a run can open only snapshots of its own
  project. Prepared snapshots belong to the project whose configured repository they were built
  from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from atlas.contracts import RunContext, RunMode
from atlas.connectors.project import SourceSet
from atlas.store import LIVE_SNAPSHOT_ID, EvidenceDB, EvidenceStore, file_sha256, to_iso

_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class SnapshotError(LookupError):
    pass


class UnsafePath(PermissionError):
    pass


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: str
    sha256: str
    path: Path
    frozen_at: str | None
    kind: str


class SnapshotRegistry:
    """Opens evidence files by snapshot id. Holds no store handle outside these files."""

    def __init__(self, data_dir: Path | str, *, prepared_dirs: tuple[str, ...] = ("cases", "cases-aliased", "releases", "releases-aliased", "injection", "new_evidence"),
                 sources: SourceSet | None = None, reference_repository: str | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.snapshots_root = self.data_dir / "snapshots"
        # "Data as of" copies built in this server (Add project) live in the private data directory.
        self.local_snapshots_root = self.data_dir / "local" / "snapshots"
        self.stores_dir = self.data_dir / "local" / "stores"
        self.prepared_dirs = prepared_dirs
        self.sources = sources or SourceSet()
        # Repository the committed prepared snapshots were built from, when their manifest does
        # not say (it comes from the reference dataset file, never from code).
        self.reference_repository = (reference_repository or "").lower() or None
        self._open: dict[str, EvidenceDB] = {}
        self._live: dict[str, EvidenceDB] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ projects

    @staticmethod
    def _check_project(project_id: str) -> str:
        project_id = project_id or ""
        if project_id and not _SNAPSHOT_ID.match(project_id):
            raise SnapshotError("invalid project id")
        return project_id

    def _project_dir(self, project_id: str) -> Path:
        project_id = self._check_project(project_id)
        return self.stores_dir / project_id if project_id else self.stores_dir

    def prepared_project(self, manifest: dict[str, Any]) -> str | None:
        """The project a prepared snapshot belongs to (None when no configured project matches)."""
        if manifest.get("project_id"):
            return str(manifest["project_id"])
        repo = (manifest.get("repository") or self.reference_repository or "").lower()
        if not repo:
            return None
        match = self.sources.by_repository(repo)
        return match.project_id if match else None

    # ------------------------------------------------------------------ live store

    def live(self, project_id: str = "") -> EvidenceDB:
        """The project's live store. Each project has its own file."""
        project_id = self._check_project(project_id)
        with self._lock:
            db = self._live.get(project_id)
            if db is None:
                db = EvidenceDB(self._project_dir(project_id) / f"{LIVE_SNAPSHOT_ID}.sqlite", live=True)
                if project_id:
                    db.set_meta("project_id", project_id)
                self._live[project_id] = db
            return db

    def store(self, project_id: str = "") -> EvidenceStore:
        return EvidenceStore(self, project_id=project_id)

    def live_evidence(self, project_id: str, record_id: str, content_version_id: str | None = None) -> dict[str, Any] | None:
        """One record version from the project's live store (for dashboard activity items).

        Returns exactly the named content version even after later updates; None if the record is
        not in this project's live store.
        """
        db = self.live(project_id)
        rec = db.by_id(record_id)
        if rec is None:
            return None
        if content_version_id and rec.content_version_id != content_version_id:
            versions = [r for r in db.history(rec.source, rec.source_id) if r.content_version_id == content_version_id]
            if not versions:
                return None
            rec = versions[-1]
        return {
            "project_id": project_id, "record_id": rec.record_id, "source": rec.source, "source_id": rec.source_id,
            "content_version_id": rec.content_version_id, "valid_from": to_iso(rec.valid_from),
            "observed_at": to_iso(rec.observed_at),
            "confirmed_through": to_iso(rec.confirmed_through) if rec.confirmed_through else None,
            "provenance": rec.provenance.value if hasattr(rec.provenance, "value") else str(rec.provenance),
            "synthetic_ancestry": rec.synthetic_ancestry, "access_scopes": sorted(rec.access_scopes),
            "content": db.content(rec.record_id),
        }

    # ------------------------------------------------------------------ freezing

    def freeze(self, *, frozen_at: datetime, label: str = "snap", project_id: str = "") -> SnapshotInfo:
        """Copy the project's live store into a new immutable snapshot file."""
        project_id = self._check_project(project_id)
        live = self.live(project_id)
        snapshot_id = f"{project_id + '.' if project_id else ''}{label}-{uuid.uuid4().hex[:12]}"
        self.stores_dir.mkdir(parents=True, exist_ok=True)
        target = self.stores_dir / f"{snapshot_id}.sqlite"
        with self._lock:
            backup = sqlite3.connect(str(target))
            try:
                live._conn.backup(backup)
            finally:
                backup.close()
        sha = file_sha256(target)
        index = self._index()
        index[snapshot_id] = {"sha256": sha, "frozen_at": to_iso(frozen_at), "kind": "frozen", "project_id": project_id}
        self._write_index(index)
        _make_read_only(target)
        return SnapshotInfo(snapshot_id, sha, target, to_iso(frozen_at), "frozen")

    def _index_path(self) -> Path:
        return self.stores_dir / "index.json"

    def _index(self) -> dict[str, Any]:
        path = self._index_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_index(self, index: dict[str, Any]) -> None:
        self.stores_dir.mkdir(parents=True, exist_ok=True)
        self._index_path().write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")

    # ------------------------------------------------------------------ prepared snapshots

    def _prepared_roots(self) -> list[tuple[Path, str]]:
        local = [(self.local_snapshots_root / "releases", "releases")]
        return [(self.snapshots_root / sub, sub) for sub in self.prepared_dirs] + local

    def _prepared_export(self, snapshot_id: str) -> Path | None:
        if not _SNAPSHOT_ID.match(snapshot_id or ""):
            return None
        for root, _ in self._prepared_roots():
            candidate = root / snapshot_id
            if (candidate / "snapshot.json").exists():
                return candidate
        return None

    def build_prepared(self, snapshot_id: str) -> Path:
        """Build (once) the read-only SQLite file for a committed snapshot export."""
        export = self._prepared_export(snapshot_id)
        if export is None:
            raise SnapshotError(f"unknown snapshot {snapshot_id}")
        manifest = json.loads((export / "snapshot.json").read_text(encoding="utf-8"))
        target = self.stores_dir / "prepared" / f"{snapshot_id}.sqlite"
        records_path = export / "records.jsonl"
        export_sha = normalized_sha256(records_path)
        if manifest.get("records_sha256") and manifest["records_sha256"] != export_sha:
            raise SnapshotError(f"snapshot export {snapshot_id} does not match its manifest")
        marker = target.with_suffix(".sha")
        if target.exists() and marker.exists() and marker.read_text(encoding="utf-8").strip() == export_sha:
            return target
        if target.exists():
            _make_writable(target)
            target.unlink()
        db = EvidenceDB(target)
        rows = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows:
            raise SnapshotError(f"snapshot export {snapshot_id} is empty")
        db.import_rows(rows)
        conflicts_path = export / "conflicts.jsonl"
        if conflicts_path.exists():
            db.import_conflicts(
                json.loads(line) for line in conflicts_path.read_text(encoding="utf-8").splitlines() if line.strip()
            )
        for key, value in manifest.items():
            db.set_meta(key, value)
        db.close()
        marker.write_text(export_sha, encoding="utf-8")
        _make_read_only(target)
        return target

    def prepared_manifest(self, snapshot_id: str) -> dict[str, Any]:
        export = self._prepared_export(snapshot_id)
        if export is None:
            raise SnapshotError(f"unknown snapshot {snapshot_id}")
        return json.loads((export / "snapshot.json").read_text(encoding="utf-8"))

    def list_snapshots(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Snapshots, filtered to one project when ``project_id`` is given."""
        out = []
        for root, sub in self._prepared_roots():
            if not root.exists():
                continue
            for child in sorted(root.iterdir()):
                if (child / "snapshot.json").exists():
                    manifest = json.loads((child / "snapshot.json").read_text(encoding="utf-8"))
                    owner = self.prepared_project(manifest)
                    if project_id is not None and owner != project_id:
                        continue
                    out.append(
                        {
                            "snapshot_id": child.name,
                            "kind": sub,
                            "release_or_case": manifest.get("release_or_case"),
                            "cutoff": manifest.get("cutoff"),
                            "alias_variant": manifest.get("alias_variant"),
                            "project_id": owner,
                            "records": manifest.get("records"),
                            **({"title": manifest["title"]} if manifest.get("title") else {}),
                        }
                    )
        for snapshot_id, info in sorted(self._index().items()):
            owner = info.get("project_id") or ""
            if project_id is not None and owner != project_id:
                continue
            out.append({"snapshot_id": snapshot_id, "kind": info.get("kind", "frozen"), "frozen_at": info.get("frozen_at"),
                        "project_id": owner})
        return out

    # ------------------------------------------------------------------ opening by context

    def open(self, ctx: RunContext) -> EvidenceDB:
        snapshot_id = ctx.snapshot_id
        run_project = getattr(ctx, "project_id", "") or ""
        if snapshot_id == LIVE_SNAPSHOT_ID:
            if ctx.mode is not RunMode.LIVE:
                raise SnapshotError("a replay run cannot read the live store")
            return self.live(run_project)
        if not _SNAPSHOT_ID.match(snapshot_id):
            raise SnapshotError("invalid snapshot id")
        with self._lock:
            index = self._index()
            # The project check runs on every open, before the cache, so a snapshot opened by one
            # project's run is never handed to another project's run.
            if run_project:
                owner = (index[snapshot_id].get("project_id") or "") if snapshot_id in index else                     (self.prepared_project(self.prepared_manifest(snapshot_id)) or "")
                if owner and owner != run_project:
                    raise SnapshotError("snapshot belongs to another project")
            cached = self._open.get(snapshot_id)
            if cached is not None:
                return cached
            if snapshot_id in index:
                path = self.stores_dir / f"{snapshot_id}.sqlite"
                if file_sha256(path) != index[snapshot_id]["sha256"]:
                    raise SnapshotError(f"snapshot {snapshot_id} was modified after freezing")
            else:
                path = self.build_prepared(snapshot_id)
            db = EvidenceDB(path, read_only=True)
            self._open[snapshot_id] = db
            return db

    def close(self) -> None:
        with self._lock:
            for db in self._open.values():
                db.close()
            self._open.clear()
            for db in self._live.values():
                db.close()
            self._live.clear()

    # ------------------------------------------------------------------ content files

    def resolve_content(self, content_ref: str) -> Path:
        """Resolve a ``file:`` content reference to a path below the snapshots root."""
        return resolve_under(self.snapshots_root, content_ref)


def resolve_under(root: Path, content_ref: str) -> Path:
    """Safely resolve a relative reference below ``root``.

    Rejected: empty references, absolute paths, drive letters, backslashes, ``..`` components,
    encoded separators, and anything that resolves (including through a symlink) outside root.
    """
    if not isinstance(content_ref, str) or not content_ref:
        raise UnsafePath("empty content reference")
    ref = content_ref[5:] if content_ref.startswith("file:") else content_ref
    lowered = ref.lower()
    if "\\" in ref or "%2f" in lowered or "%5c" in lowered or "%2e" in lowered or "\x00" in ref:
        raise UnsafePath("alternate separators or encoded characters are not allowed")
    if ref.startswith("/") or re.match(r"^[A-Za-z]:", ref) or ref.startswith("~"):
        raise UnsafePath("absolute references are not allowed")
    raw_parts = ref.split("/")
    if any(p in ("..", ".", "") for p in raw_parts):
        raise UnsafePath("relative traversal or empty path segments are not allowed")
    parts = PurePosixPath(ref).parts
    if not parts:
        raise UnsafePath("relative traversal is not allowed")
    root_resolved = Path(root).resolve()
    candidate = Path(root, *parts)
    for i in range(1, len(parts) + 1):
        partial = Path(root, *parts[:i])
        if partial.is_symlink():
            raise UnsafePath("symlinks are not allowed")
    resolved = candidate.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise UnsafePath("reference resolves outside the snapshots root")
    return resolved


def _make_read_only(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _make_writable(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def normalized_sha256(path: Path) -> str:
    """SHA256 of a text export with line endings normalised to LF (stable across checkouts)."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def clear_prepared_cache(registry: SnapshotRegistry) -> None:
    """Drop built prepared files (used by preparation after re-exporting)."""
    registry.close()
    prepared = registry.stores_dir / "prepared"
    if prepared.exists():
        for child in prepared.iterdir():
            _make_writable(child)
        shutil.rmtree(prepared)
