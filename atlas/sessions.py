"""Server-side sign-in sessions.

A session lives in a small SQLite database. The browser only holds a random token in the
``__Host-atlas_session`` cookie; the database stores a hash of it, never the token itself.

Rules:
- A session ends 8 hours after sign-in (absolute limit) or after 30 minutes without a user request
  (idle limit). Live streams never extend the idle time.
- The token changes at sign-in (rotation), so a token seen before sign-in is useless afterwards.
- Every state-changing request must carry the session's CSRF token.
- A live stream is bound to the session that opened it. Signing out or expiry closes every stream of
  that session at once, and a stream rechecks its session before each event.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

COOKIE_NAME = "__Host-atlas_session"
ABSOLUTE_SECONDS = 8 * 60 * 60
IDLE_SECONDS = 30 * 60
STREAM_RECHECK_SECONDS = 15


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Session:
    session_id: str  # hash of the cookie token; safe to log and to bind streams to
    user: str
    created_at: float
    last_seen: float
    csrf_token: str

    def csrf_ok(self, presented: str | None) -> bool:
        if not presented:
            return False
        return hmac.compare_digest(self.csrf_token.encode(), presented.encode())


class StreamHandle:
    """One open live stream bound to one session."""

    def __init__(self, store: "SessionStore", session_id: str) -> None:
        self._store = store
        self.session_id = session_id
        self._closed = threading.Event()
        self._on_close: list[Callable[[], None]] = []

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def on_close(self, callback: Callable[[], None]) -> None:
        self._on_close.append(callback)
        if self.closed:
            callback()

    def still_valid(self) -> bool:
        """Call before sending each event. Checks the session without extending its idle time."""
        if self.closed:
            return False
        if self._store.lookup(self.session_id) is None:
            self.close()
            return False
        return True

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        for callback in self._on_close:
            try:
                callback()
            except Exception:  # a broken listener must not keep other streams open
                pass
        self._store._forget_stream(self)


class SessionStore:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time,
                 absolute_seconds: int = ABSOLUTE_SECONDS, idle_seconds: int = IDLE_SECONDS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.absolute_seconds = absolute_seconds
        self.idle_seconds = idle_seconds
        self._lock = threading.Lock()
        self._streams: dict[str, set[StreamHandle]] = {}
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                " session_id TEXT PRIMARY KEY, user TEXT NOT NULL, created_at REAL NOT NULL,"
                " last_seen REAL NOT NULL, csrf_token TEXT NOT NULL)"
            )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            yield db
        finally:
            db.close()

    # -- life cycle ---------------------------------------------------------------------------

    def create(self, user: str) -> tuple[str, Session]:
        """Start a session for a signed-in user. Returns the cookie token and the session."""
        token = secrets.token_urlsafe(32)
        now = self._clock()
        session = Session(_hash(token), user, now, now, secrets.token_urlsafe(32))
        with self._lock, self._db() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                       (session.session_id, user, now, now, session.csrf_token))
        return token, session

    def rotate(self, token: str) -> tuple[str, Session] | None:
        """Replace a valid session with a new token and CSRF token, keeping the same user."""
        current = self.validate(token, touch=False)
        if current is None:
            return None
        self.revoke(token)
        return self.create(current.user)

    def validate(self, token: str | None, *, touch: bool = True) -> Session | None:
        """The live session for a cookie token, or None. ``touch`` marks a user request."""
        if not token:
            return None
        session = self.lookup(_hash(token))
        if session is None:
            return None
        if touch:
            now = self._clock()
            with self._lock, self._db() as db:
                db.execute("UPDATE sessions SET last_seen = ? WHERE session_id = ?", (now, session.session_id))
            session = Session(session.session_id, session.user, session.created_at, now, session.csrf_token)
        return session

    def lookup(self, session_id: str) -> Session | None:
        """Read a session by id, ending it if a limit has passed. Never extends idle time."""
        with self._db() as db:
            row = db.execute(
                "SELECT session_id, user, created_at, last_seen, csrf_token FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        session = Session(*row)
        now = self._clock()
        if now - session.created_at >= self.absolute_seconds or now - session.last_seen >= self.idle_seconds:
            self._end(session.session_id)
            return None
        return session

    def revoke(self, token: str) -> None:
        """Sign out: delete the session and close all of its streams."""
        self._end(_hash(token))

    def revoke_user(self, user: str) -> None:
        with self._db() as db:
            ids = [r[0] for r in db.execute("SELECT session_id FROM sessions WHERE user = ?", (user,))]
        for session_id in ids:
            self._end(session_id)

    def sweep(self) -> int:
        """End every expired session (and close its streams). Returns how many ended."""
        with self._db() as db:
            ids = [r[0] for r in db.execute("SELECT session_id FROM sessions")]
        before = len(ids)
        for session_id in ids:
            self.lookup(session_id)
        with self._db() as db:
            after = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return before - after

    def _end(self, session_id: str) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            handles = list(self._streams.pop(session_id, set()))
        for handle in handles:
            handle.close()

    # -- streams ------------------------------------------------------------------------------

    def bind_stream(self, session: Session) -> StreamHandle:
        handle = StreamHandle(self, session.session_id)
        with self._lock:
            self._streams.setdefault(session.session_id, set()).add(handle)
        return handle

    def open_streams(self, session_id: str) -> int:
        with self._lock:
            return len(self._streams.get(session_id, ()))

    def _forget_stream(self, handle: StreamHandle) -> None:
        with self._lock:
            handles = self._streams.get(handle.session_id)
            if handles is not None:
                handles.discard(handle)
                if not handles:
                    self._streams.pop(handle.session_id, None)


def session_cookie(token: str, max_age: int = ABSOLUTE_SECONDS) -> str:
    """The Set-Cookie value. ``__Host-`` requires Secure, Path=/ and no Domain."""
    return f"{COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Strict"


def clear_cookie() -> str:
    return f"{COOKIE_NAME}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"
