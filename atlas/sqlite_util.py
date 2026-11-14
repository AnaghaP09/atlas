"""One SQLite connection shared safely by several threads.

The API serves requests on many threads while runs write from their own threads. A plain
``sqlite3`` connection must not run two statements at once, even with ``check_same_thread=False``:
doing so fails with "bad parameter or other API misuse". ``connect`` returns a thin wrapper that
runs every statement under one lock and reads all of its rows before the lock is released, so a
caller in another thread can never interleave with a half-read result.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Iterable, Iterator, Sequence


class Result:
    """The rows of one finished statement. Offers the parts of a cursor the stores use."""

    def __init__(self, rows: list[Any], rowcount: int, lastrowid: int | None, description: Any) -> None:
        self._rows = rows
        self._next = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid
        self.description = description

    def fetchone(self) -> Any:
        if self._next >= len(self._rows):
            return None
        row = self._rows[self._next]
        self._next += 1
        return row

    def fetchall(self) -> list[Any]:
        rows = self._rows[self._next:]
        self._next = len(self._rows)
        return rows

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())


class SafeCursor:
    """A cursor whose statements run under the connection's lock."""

    def __init__(self, owner: "SafeConnection") -> None:
        self._owner = owner
        self._cur = owner._conn.cursor()
        self._result = Result([], -1, None, None)

    def execute(self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()) -> "SafeCursor":
        with self._owner.lock:
            self._cur.execute(sql, parameters)
            rows = self._cur.fetchall() if self._cur.description is not None else []
            self._result = Result(rows, self._cur.rowcount, self._cur.lastrowid, self._cur.description)
        return self

    def fetchone(self) -> Any:
        return self._result.fetchone()

    def fetchall(self) -> list[Any]:
        return self._result.fetchall()

    def __iter__(self) -> Iterator[Any]:
        return iter(self._result)

    @property
    def rowcount(self) -> int:
        return self._result.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._result.lastrowid

    def close(self) -> None:
        with self._owner.lock:
            self._cur.close()


class SafeConnection:
    """Wraps one connection. Hold ``lock`` around several statements that must not be split."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.lock = threading.RLock()

    def execute(self, sql: str, parameters: Sequence[Any] | dict[str, Any] = ()) -> Result:
        with self.lock:
            cur = self._conn.execute(sql, parameters)
            rows = cur.fetchall() if cur.description is not None else []
            return Result(rows, cur.rowcount, cur.lastrowid, cur.description)

    def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> Result:
        with self.lock:
            cur = self._conn.executemany(sql, parameters)
            return Result([], cur.rowcount, cur.lastrowid, None)

    def executescript(self, script: str) -> None:
        with self.lock:
            self._conn.executescript(script)

    def cursor(self) -> SafeCursor:
        return SafeCursor(self)

    def commit(self) -> None:
        with self.lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self.lock:
            self._conn.rollback()

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_conn", "lock"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)


def connect(database: str, **kwargs: Any) -> SafeConnection:
    """Open a connection that several threads may use at the same time."""
    kwargs.setdefault("check_same_thread", False)
    return SafeConnection(sqlite3.connect(database, **kwargs))
