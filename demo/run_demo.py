"""Run the demo manifest against the two servers, only through the HTTP API.

    python -m demo.run_demo                        # every row
    python -m demo.run_demo --rows T1 T2 T6a      # chosen rows
    python -m demo.run_demo --sign-in              # the HTTPS site with built-in sign-in

Base URLs come from ATLAS_BASE_URL and ATLAS_SCENARIO_URL, or are built from ATLAS_HOST and
ATLAS_PORT (the scenario server uses ATLAS_PEER_UI_URL, or the next port). Snapshot, release and case
ids come from the ATLAS_DEMO_* settings named in the manifest. Before each step the runner
asks the target server for /api/health and stops if it is not the server the row names. Each call
can save values from its response for later calls (``{name}`` in paths and bodies), wait until a
condition holds, and assert machine-checkable conditions.

With ``--sign-in`` (or ATLAS_IDENTITY=local) the runner signs in as the demo user for each role
instead of sending a role header; see ``demo/session_client.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from atlas.api.demo_vars import demo_variables, missing_variables

MANIFEST = Path(__file__).resolve().parent / "manifest.json"


def settings_env(env_file: Path) -> dict[str, str]:
    """Settings from .env, overridden by the environment."""
    from atlas.config import parse_env_file

    env = parse_env_file(env_file)
    env.update({k: v for k, v in os.environ.items() if k.startswith("ATLAS_")})
    return env
Send = Callable[[str, str, str, Mapping[str, str], Any, Mapping[str, Any] | None], tuple[int, Mapping[str, str], Any]]


class DemoStop(Exception):
    """The demo cannot continue (wrong server, failed assertion, unexpected status)."""


def get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            if not part.isdigit() or int(part) >= len(cur):
                return _MISSING
            cur = cur[int(part)]
        elif isinstance(cur, Mapping):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        else:
            return _MISSING
    return cur


_MISSING = object()


def check(obj: Any, assertion: Mapping[str, Any]) -> bool:
    got = get_path(obj, assertion["path"])
    op, want = assertion["op"], assertion.get("value")
    if op == "exists":
        return got is not _MISSING
    if op == "absent":
        return got is _MISSING
    if got is _MISSING:
        return False
    if op == "eq":
        return got == want
    if op == "ne":
        return got != want
    if op == "in":
        return got in want
    if op == "contains":
        return want in got
    if op == "not_contains":
        return want not in got
    if op == "gte":
        return got >= want
    if op == "len_gte":
        return len(got) >= want
    if op == "contains_where":
        return any(isinstance(x, Mapping) and all(x.get(k) == v for k, v in want.items()) for x in got)
    raise DemoStop(f"unknown assertion op {op}")


def fill(value: Any, saved: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        whole = re.fullmatch(r"\{(\w+)\}", value)
        if whole and whole.group(1) in saved:
            return saved[whole.group(1)]
        return re.sub(r"\{(\w+)\}", lambda m: str(saved.get(m.group(1), m.group(0))), value)
    if isinstance(value, Mapping):
        return {k: fill(v, saved) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, saved) for v in value]
    return value


@dataclass
class Runner:
    send: Send
    urls: Mapping[str, str]
    log: Callable[[str], None] = print
    wait_seconds: float = 30.0
    poll_seconds: float = 0.5
    saved: dict[str, Any] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)

    def _health(self, server: str) -> None:
        status, _, body = self.send("GET", self.urls[server], "/api/health", {}, None, None)
        env = (body or {}).get("environment") if isinstance(body, Mapping) else None
        if status != 200 or env != server:
            raise DemoStop(f"expected the {server} server at {self.urls[server]}, found {env or 'nothing'}")

    def _call(self, server: str, call: Mapping[str, Any]) -> Any:
        self._health(server)
        headers = {"X-Atlas-Role": call.get("role", "em")}
        if call["method"] == "POST":
            name = call.get("key")
            headers["Idempotency-Key"] = self.keys.setdefault(name, str(uuid.uuid4())) if name else str(uuid.uuid4())
        path = fill(call["path"], self.saved)
        body = fill(call.get("body"), self.saved)
        params = fill(call.get("query"), self.saved)
        deadline = time.monotonic() + (self.wait_seconds if call.get("wait") or call.get("wait_for") else 0)
        while True:
            status, resp_headers, data = self.send(call["method"], self.urls[server], path, headers, body, params)
            waiting = call.get("wait_for")
            ready = status in call.get("expect_status", [200]) and (not waiting or check(data, waiting))
            if ready or time.monotonic() >= deadline:
                break
            time.sleep(self.poll_seconds)
        if status not in call.get("expect_status", [200]):
            raise DemoStop(f"{call['method']} {path}: status {status}, expected {call.get('expect_status')}: {data}")
        for h, v in (call.get("expect_header") or {}).items():
            if resp_headers.get(h) != v and resp_headers.get(h.lower()) != v:
                raise DemoStop(f"{call['method']} {path}: header {h} is not {v}")
        for a in call.get("assert", []):
            if not check(data, fill(a, self.saved)):
                raise DemoStop(f"{call['method']} {path}: assertion failed: {a}")
        for name, p in (call.get("save") or {}).items():
            self.saved[name] = get_path(data, p)
        return data

    def run_row(self, row: Mapping[str, Any]) -> None:
        server = row["server"]
        self.log(f"[{row['row_id']}] {row['title']} (canvas box {row['canvas_box']}, {row['data_mode']}, {server} server)")
        for call in list(row.get("setup", [])) + list(row.get("calls", [])):
            self._call(server, call)
        for call in row.get("restoration", []):
            self._call(server, call)

    def run(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            self.run_row(row)


def load_rows(wanted: Sequence[str] | None = None, path: Path = MANIFEST) -> list[dict[str, Any]]:
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    return [r for r in rows if not wanted or r["row_id"] in wanted]


def httpx_send() -> Send:
    import httpx

    client = httpx.Client(timeout=30.0)

    def send(method: str, base: str, path: str, headers: Mapping[str, str], body: Any, params: Any):
        r = client.request(method, base + path, headers=dict(headers), json=body, params=params)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, r.headers, data

    return send


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m demo.run_demo")
    parser.add_argument("--rows", nargs="*")
    parser.add_argument("--sign-in", action="store_true", help="sign in per role on the HTTPS site")
    args = parser.parse_args(argv)
    env = settings_env(Path(".env"))
    sign_in = args.sign_in or env.get("ATLAS_IDENTITY", "header").lower() == "local"
    if sign_in:
        org = env.get("ATLAS_ORG_SLUG", "demo")
        urls = {"normal": env.get("ATLAS_BASE_URL") or f"https://atlas.{org}.internal",
                "scenario": env.get("ATLAS_SCENARIO_URL") or f"https://scenario.{org}.internal:8443"}
    else:
        host = env.get("ATLAS_HOST", "127.0.0.1")
        port = int(env.get("ATLAS_PORT", "8000"))
        host = f"[{host}]" if ":" in host else host
        urls = {"normal": env.get("ATLAS_BASE_URL") or f"http://{host}:{port}",
                "scenario": env.get("ATLAS_SCENARIO_URL") or env.get("ATLAS_PEER_UI_URL") or f"http://{host}:{port + 1}"}
    missing = missing_variables(env)
    if missing:
        print("set these demo settings first: " + ", ".join(missing), file=sys.stderr)
        return 2
    try:
        if sign_in:
            from demo.session_client import httpx_session_send

            send = httpx_session_send(Path(env.get("ATLAS_DATA_DIR", "data")))
        else:
            send = httpx_send()
        Runner(send, urls, saved=dict(demo_variables(env))).run(load_rows(args.rows))
    except DemoStop as exc:
        print(f"demo stopped: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:  # sign-in problems, with the setting to fix
        print(f"demo stopped: {exc}", file=sys.stderr)
        return 1
    print("demo finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
