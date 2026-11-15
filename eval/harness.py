"""Run ATLAS in this process for evaluation.

The harness builds the same application the server builds and calls it through its HTTP API, so an
evaluation exercises the real routes, guardrails and budgets. It keeps every run's output in a
session folder so that a stopped evaluation can continue without paying for the same run twice.

Settings come from the ``.env`` file named on the command line. The key in it is read by the
application at run time; the harness never prints or copies it.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
DONE = ("complete", "partial", "unverified", "failed")


class SpendCeilingReached(RuntimeError):
    """The evaluation stopped because its model spend ceiling was reached."""


class RunCache:
    """Run outputs by key, saved after every run."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.items: dict[str, Any] = {}
        if path is not None and path.exists():
            self.items = json.loads(path.read_text(encoding="utf-8"))

    def get(self, key: str) -> Any:
        return self.items.get(key)

    def put(self, key: str, value: Any) -> None:
        self.items[key] = value
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, indent=1, default=str), encoding="utf-8")
            tmp.replace(self.path)

    def spend(self) -> float:
        return sum(float((v.get("cost") or {}).get("usd") or 0) for v in self.items.values() if isinstance(v, dict))


class Harness:
    """Calls one ATLAS application. ``call`` is ``(method, path, role, body) -> (status, json)``."""

    def __init__(self, call: Callable[[str, str, str, Any], tuple[int, dict[str, Any]]], *, services: Any = None,
                 session_dir: Path | None = None, max_usd: float = 0.0, poll_seconds: float = 2.0,
                 wait_seconds: float = 1200.0) -> None:
        self.call = call
        self.services = services
        self.cache = RunCache(session_dir / "runs.json" if session_dir else None)
        self.max_usd = max_usd
        self.poll_seconds = poll_seconds
        self.wait_seconds = wait_seconds

    # ------------------------------------------------------------------ construction

    @classmethod
    def in_process(cls, env_file: str | Path, *, data_dir: str | Path | None = None, session_dir: Path | None = None,
                   max_usd: float = 0.0) -> "Harness":
        """Build the application from the settings file, without opening a network port."""
        from fastapi.testclient import TestClient

        from atlas.api.__main__ import build_server

        if data_dir is not None:
            os.environ["ATLAS_DATA_DIR"] = str(data_dir)
        os.environ["ATLAS_DASHBOARD_POLL"] = "off"  # an evaluation must not fetch live data
        settings, app = build_server(str(env_file), False)
        base = f"http://{settings.host}:{settings.port}"
        client = TestClient(app, base_url=base)

        def call(method: str, path: str, role: str, body: Any = None) -> tuple[int, dict[str, Any]]:
            headers = {"X-Atlas-Role": role, "Origin": base}
            if method == "POST":
                headers["Idempotency-Key"] = str(uuid.uuid4())
            response = client.request(method, path, headers=headers, json=body if method == "POST" else None)
            try:
                return response.status_code, response.json()
            except ValueError:
                return response.status_code, {}

        return cls(call, services=app.state.services, session_dir=session_dir, max_usd=max_usd)

    # ------------------------------------------------------------------ runs

    def _guard(self) -> None:
        if self.max_usd and self.cache.spend() >= self.max_usd:
            raise SpendCeilingReached(f"model spend reached {self.cache.spend():.2f} USD of {self.max_usd:.2f}")

    def _finish(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.wait_seconds
        view: dict[str, Any] = {}
        while time.monotonic() < deadline:
            _, view = self.call("GET", f"/api/runs/{run_id}", "audit")
            if view.get("run_status") in DONE and view.get("admission_status") not in ("admitted", "dispatching"):
                break
            time.sleep(self.poll_seconds)
        out: dict[str, Any] = {"run_id": run_id, "run_status": view.get("run_status"), "cost": view.get("cost") or {},
                               "reports": {}}
        for rep in view.get("reports", []):
            _, body = self.call("GET", f"/api/reports/{rep['report_id']}", "audit")
            out["reports"][rep["report_type"]] = body.get("report", {})
        return out

    def _start(self, key: str, role: str, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        self._guard()
        started = time.monotonic()
        status, answer = self.call("POST", path, role, dict(body))
        run_id = answer.get("run_id")
        if not run_id:
            out = {"error": answer.get("reason") or answer.get("error") or f"HTTP {status}", "cost": {}, "reports": {}}
        else:
            out = self._finish(run_id)
            out["wall_seconds"] = round(time.monotonic() - started, 1)
        self.cache.put(key, out)
        return out

    def investigate(self, snapshot_id: str, event_id: str, rep: str = "R1") -> dict[str, Any]:
        return self._start(f"investigate|{snapshot_id}|{event_id}|{rep}", "em", "/api/triggers/manual/investigate",
                           {"snapshot_id": snapshot_id, "event_id": str(event_id),
                            "summary": f"failure reported in issue {event_id}"})

    def release_close(self, snapshot_id: str, release_id: str, rep: str = "R1") -> dict[str, Any]:
        return self._start(f"release_close|{snapshot_id}|{release_id}|{rep}", "pm", "/api/triggers/release-close",
                           {"snapshot_id": snapshot_id, "release_id": release_id})

    def question(self, snapshot_id: str, question: str, rep: str = "R1") -> dict[str, Any]:
        return self._start(f"question|{snapshot_id}|{question}|{rep}", "em", "/api/triggers/question",
                           {"snapshot_id": snapshot_id, "question": question})

    def evidence(self, run_id: str, record_id: str) -> dict[str, Any] | None:
        status, body = self.call("GET", f"/api/evidence/{record_id}?run_id={run_id}", "audit")
        return body.get("record") if status == 200 else None

    # ------------------------------------------------------------------ direct verifier access

    def verify(self, snapshot_id: str, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Give claims to Sentinel with the records they cite, outside any run. In-process only."""
        from atlas.agents.verifier import Verifier

        if self.services is None:
            raise RuntimeError("the verifier check needs the in-process harness")
        self._guard()
        orchestrator = self.services.orchestrator
        ctx = self.services.snapshots._ctx(snapshot_id)
        if ctx is None:
            raise LookupError(f"unknown data set {snapshot_id}")
        resolver = orchestrator._resolver(ctx)
        for claim in claims:
            for record_id in claim.get("evidence_ids", []):
                orchestrator._preload(resolver, ctx, record_id)
        result = Verifier(orchestrator.d.outbound, resolver.by_id).verify(claims)
        return result.claims


def report_of(run: Mapping[str, Any], *preferred: str) -> dict[str, Any]:
    """The run's main report: the first of ``preferred`` that exists, otherwise any report."""
    reports = run.get("reports") or {}
    for name in preferred:
        if name in reports:
            return reports[name]
    return next(iter(reports.values()), {})
