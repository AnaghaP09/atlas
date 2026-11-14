"""Idempotency keys for every manual mutation.

The first request with a key reserves it (state ``pending``) before the effect runs, then stores the
outcome (state ``done``). A repeat with the same key, caller and payload returns the stored outcome
without a second effect. Reusing a key for a different payload or caller is refused. A key still
``pending`` (an effect whose outcome was not recorded, for example after a crash) is never run again
automatically: the caller is told the outcome is uncertain and reads the resource instead.
Effects that can be keyed (run admission) also receive the key, so even a lost reservation cannot
create a second run. Keys are kept for seven days.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

RETENTION = timedelta(days=7)


class IdempotencyError(Exception):
    def __init__(self, status: int, error: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.error = error
        self.reason = reason


@dataclass(frozen=True)
class Outcome:
    status: int
    body: dict[str, Any]
    replay: bool


def request_hash(method: str, path: str, body: Any) -> str:
    blob = json.dumps({"m": method, "p": path, "b": body}, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def validate_key(key: str | None) -> str:
    if not key:
        raise IdempotencyError(400, "idempotency_key_required", "this request needs an Idempotency-Key header (a UUID)")
    try:
        return str(uuid.UUID(key))
    except ValueError as exc:
        raise IdempotencyError(400, "idempotency_key_invalid", "the Idempotency-Key header must be a UUID") from exc


class IdempotencyStore:
    def __init__(self, path: str | Path = ":memory:", clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS idempotency (key TEXT PRIMARY KEY, principal TEXT, request_hash TEXT, "
            "state TEXT, status INTEGER, body TEXT, created TEXT)"
        )
        self._lock = threading.Lock()
        self.clock = clock

    def _purge(self) -> None:
        cutoff = (self.clock() - RETENTION).isoformat()
        self._db.execute("DELETE FROM idempotency WHERE created < ?", (cutoff,))

    def run(self, key: str, principal: str, req_hash: str, effect: Callable[[], tuple[int, dict[str, Any]]]) -> Outcome:
        with self._lock:
            self._purge()
            row = self._db.execute("SELECT principal, request_hash, state, status, body FROM idempotency WHERE key = ?",
                                   (key,)).fetchone()
            if row:
                if row[0] != principal or row[1] != req_hash:
                    raise IdempotencyError(409, "idempotency_key_reused",
                                           "this Idempotency-Key was already used for a different request")
                if row[2] == "done":
                    return Outcome(row[3], json.loads(row[4]), replay=True)
                raise IdempotencyError(409, "outcome_uncertain",
                                       "an earlier request with this key did not record its outcome; read the resource "
                                       "to see whether it took effect")
            self._db.execute("INSERT INTO idempotency VALUES (?,?,?,?,?,?,?)",
                             (key, principal, req_hash, "pending", None, None, self.clock().isoformat()))
        try:
            status, body = effect()
        except IdempotencyError:
            self._forget(key)
            raise
        except _Recordable as rec:
            status, body = rec.status, rec.body
        except Exception:
            # The effect failed before completing; nothing was recorded, so a retry may run it again.
            self._forget(key)
            raise
        with self._lock:
            self._db.execute("UPDATE idempotency SET state = 'done', status = ?, body = ? WHERE key = ?",
                             (status, json.dumps(body, default=str), key))
        return Outcome(status, body, replay=False)

    def _forget(self, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM idempotency WHERE key = ? AND state = 'pending'", (key,))

    def state(self, key: str) -> str | None:
        row = self._db.execute("SELECT state FROM idempotency WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


class _Recordable(Exception):
    """A refusal that is part of the outcome and must be replayed unchanged."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("reason", ""))
        self.status = status
        self.body = body


def recorded_refusal(status: int, error: str, reason: str) -> _Recordable:
    return _Recordable(status, {"error": error, "reason": reason})
