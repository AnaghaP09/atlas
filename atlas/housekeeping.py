"""Periodic housekeeping while the server runs.

Every interval (``ATLAS_SWEEP_SECONDS``, default 300) the housekeeper:
- sweeps approvals: expires old ones, sends nearing-expiry reminders and flags tasks waiting past
  the approval time limit (ATLAS only reminds; it takes no action on a missed limit);
- in sign-in mode, removes expired sessions, which also closes their live streams.

A failing job is reported through a notification and never stops the other jobs.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable

DEFAULT_INTERVAL_SECONDS = 300.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Housekeeper:
    def __init__(self, jobs: dict[str, Callable[[], Any]], *, interval: float = DEFAULT_INTERVAL_SECONDS,
                 on_error: Callable[[str, Exception], None] | None = None) -> None:
        self.jobs = dict(jobs)
        self.interval = max(1.0, float(interval))
        self.on_error = on_error
        self.last_results: dict[str, Any] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for name, job in self.jobs.items():
            try:
                results[name] = job()
            except Exception as exc:  # one broken job must not stop the others
                results[name] = f"failed: {type(exc).__name__}"
                if self.on_error is not None:
                    try:
                        self.on_error(name, exc)
                    except Exception:
                        pass
        self.last_results = results
        return results

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.run_once()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="atlas-housekeeping", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def build_housekeeper(services: Any, sessions: Any = None, *, interval: float = DEFAULT_INTERVAL_SECONDS,
                      clock: Callable[[], datetime] = _now) -> Housekeeper:
    jobs: dict[str, Callable[[], Any]] = {}
    wiring = getattr(services, "wiring", None)
    approvals = getattr(wiring, "approvals", None)
    if approvals is not None:
        jobs["approvals"] = lambda: approvals.sweep(clock())
    if sessions is not None:
        jobs["sessions"] = sessions.sweep

    def report(name: str, exc: Exception) -> None:
        notifier = getattr(services, "notifier", None)
        if notifier is not None:
            notifier.emit("scheduler.failed", name, [name], frozenset({"internal"}),
                          {"text": f"the {name} housekeeping job failed", "required_scopes": ["internal"]})

    return Housekeeper(jobs, interval=interval, on_error=report)
