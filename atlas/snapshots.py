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

    def __init__(self, data_dir: Path | str, *, prepared_dirs: tuple[str, ...] = ("cases", "cases-aliased", "releases", "releases-aliased")) -> None:
        self.data_dir = Path(data_dir)
        self.snapshots_root = self.data_dir / "snapshots"
        self.stores_dir = self.data_dir / "local" / "stores"
        self.prepared_dirs = prepared_dirs
        self._open: dict[str, EvidenceDB] = {}
        self._live: EvidenceDB | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ live store

    def live(self) -> EvidenceDB:
        with self._lock:
            if self._live is None:
                self._live = EvidenceDB(self.stores_dir / f"{LIVE_SNAPSHOT_ID}.sqlite")
            return self._live

    def store(self) -> EvidenceStore:
        return EvidenceStore(self)

    # ------------------------------------------------------------------ freezing

    def freeze(self, *, frozen_at: datetime, label: str = "snap") -> SnapshotInfo:
        """Copy the live store into a new immutable snapshot file."""
        live = self.live()
        snapshot_id = f"{label}-{uuid.uuid4().hex[:12]}"
        target = self.stores_dir / f"{snapshot_id}.sqlite"
        with self._lock:
            backup = sqlite3.connect(str(target))
            try:
                live._conn.backup(backup)
            finally:
                backup.close()
        sha = file_sha256(target)
        index = self._index()
        index[snapshot_id] = {"sha256": sha, "frozen_at": to_iso(frozen_at), "kind": "frozen"}
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

    def _prepared_export(self, snapshot_id: str) -> Path | None:
        for sub in self.prepared_dirs:
            candidate = self.snapshots_root / sub / snapshot_id
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
        export_sha = file_sha256(records_path)
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

    def list_snapshots(self) -> list[dict[str, Any]]:
        out = []
        for sub in self.prepared_dirs:
            root = self.snapshots_root / sub
            if not root.exists():
                continue
            for child in sorted(root.iterdir()):
                if (child / "snapshot.json").exists():
                    manifest = json.loads((child / "snapshot.json").read_text(encoding="utf-8"))
                    out.append(
                        {
                            "snapshot_id": child.name,
                            "kind": sub,
                            "release_or_case": manifest.get("release_or_case"),
                            "cutoff": manifest.get("cutoff"),
                            "alias_variant": manifest.get("alias_variant"),
                        }
                    )
        for snapshot_id, info in sorted(self._index().items()):
            out.append({"snapshot_id": snapshot_id, "kind": info.get("kind", "frozen"), "frozen_at": info.get("frozen_at")})
        return out

    # ------------------------------------------------------------------ opening by context

    def open(self, ctx: RunContext) -> EvidenceDB:
        snapshot_id = ctx.snapshot_id
        if snapshot_id == LIVE_SNAPSHOT_ID:
            if ctx.mode is not RunMode.LIVE:
                raise SnapshotError("a replay run cannot read the live store")
            return self.live()
        if not _SNAPSHOT_ID.match(snapshot_id):
            raise SnapshotError("invalid snapshot id")
        with self._lock:
            cached = self._open.get(snapshot_id)
            if cached is not None:
                return cached
            index = self._index()
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
            if self._live is not None:
                self._live.close()
                self._live = None

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


def records_sha256(lines: list[str]) -> str:
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def clear_prepared_cache(registry: SnapshotRegistry) -> None:
    """Drop built prepared files (used by preparation after re-exporting)."""
    registry.close()
    prepared = registry.stores_dir / "prepared"
    if prepared.exists():
        for child in prepared.iterdir():
            _make_writable(child)
        shutil.rmtree(prepared)
