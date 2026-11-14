"""Immutable evaluation results.

An evaluation is written once, with the corpus identity and the code revision, and never changed.
A later evaluation gets a new id. The HTTP API reads evaluations from here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class EvaluationStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS evaluations (evaluation_id TEXT PRIMARY KEY, created TEXT, code_revision TEXT, "
            "corpus_hash TEXT, status TEXT, body TEXT, body_hash TEXT)"
        )
        self._lock = threading.Lock()

    def add(self, result: Mapping[str, Any], *, code_revision: str, corpus: Mapping[str, Any], status: str = "complete") -> str:
        evaluation_id = f"eval-{uuid.uuid4().hex[:10]}"
        corpus_hash = hashlib.sha256(json.dumps(corpus, sort_keys=True, default=str).encode()).hexdigest()
        body = dict(result, evaluation_id=evaluation_id, code_revision=code_revision, corpus=dict(corpus),
                    corpus_hash=corpus_hash, status=status)
        blob = json.dumps(body, sort_keys=True, default=str)
        with self._lock:
            self._db.execute("INSERT INTO evaluations VALUES (?,?,?,?,?,?,?)",
                             (evaluation_id, datetime.now(timezone.utc).isoformat(), code_revision, corpus_hash, status,
                              blob, hashlib.sha256(blob.encode()).hexdigest()))
        return evaluation_id

    def list(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT evaluation_id, created, code_revision, corpus_hash, status FROM evaluations ORDER BY created").fetchall()
        return [dict(zip(("evaluation_id", "created", "code_revision", "corpus_hash", "status"), r)) for r in rows]

    def get(self, evaluation_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT body, body_hash FROM evaluations WHERE evaluation_id = ?", (evaluation_id,)).fetchone()
        if not row:
            return None
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise RuntimeError("evaluation record changed after it was written")
        return json.loads(row[0])

    def latest(self) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT evaluation_id FROM evaluations WHERE status = 'complete' ORDER BY created DESC LIMIT 1").fetchone()
        return self.get(row[0]) if row else None

    def update(self, *_: Any, **__: Any) -> None:
        raise PermissionError("evaluations are immutable")
