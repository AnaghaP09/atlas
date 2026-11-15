"""Live listing of a project's CI jobs from its public job-artefact store (read-only).

The store keeps one folder per job below the configured listing prefixes, a small
``latest-build.txt`` in each job folder and a ``finished.json`` in each build folder. This
connector lists the job folders (bounded pages), then checks each job's latest build with a
conditional request, so an unchanged job costs one ``304`` and no download.

Everything comes from the project's configuration; nothing here names a project. Jobs whose latest
build finished long ago are treated as dormant and checked only when the job list is refreshed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.parse import quote

from atlas.contracts import EgressPurpose, RunMode
from atlas.gateway import Gateway, GatewayRefused, RequestContext, SourceUnavailable

_PREFIX = re.compile(r"<Prefix>([^<]+)</Prefix>")
_NEXT = re.compile(r"<NextMarker>([^<]+)</NextMarker>")
_TRUNCATED = re.compile(r"<IsTruncated>true</IsTruncated>")
FAILED = {"FAILURE", "ERROR", "ABORTED"}


def parse_listing(xml: str, parent: str) -> tuple[list[str], str | None]:
    """Job folders in one listing page and the marker of the next page (or None)."""
    folders = [p for p in _PREFIX.findall(xml) if p != parent and p.endswith("/")]
    nxt = _NEXT.search(xml)
    return folders, (nxt.group(1) if nxt and _TRUNCATED.search(xml) else None)


def _as_mapping(body: Any) -> Mapping[str, Any] | None:
    """The store serves small JSON files with a plain-text content type; accept both forms."""
    if isinstance(body, Mapping):
        return body
    if isinstance(body, str):
        try:
            data = json.loads(body)
        except ValueError:
            return None
        return data if isinstance(data, Mapping) else None
    return None


def job_kind(folder: str) -> str:
    return "presubmit" if "pr-logs" in folder else "periodic"


class JobListing:
    """Keeps the latest result of every recently active job below the listing prefixes."""

    def __init__(self, gateway: Gateway, *, artifact_base: str, listing_prefixes: tuple[str, ...],
                 clock: Callable[[], datetime] | None = None, max_pages: int = 6, max_jobs: int = 300,
                 checks_per_poll: int = 40, recent: timedelta = timedelta(days=7),
                 relist_every: timedelta = timedelta(hours=1)) -> None:
        self.gateway = gateway
        self.base = artifact_base.rstrip("/")
        self.prefixes = tuple(p for p in listing_prefixes if p)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_pages, self.max_jobs, self.checks_per_poll = max_pages, max_jobs, checks_per_poll
        self.recent, self.relist_every = recent, relist_every
        self.ctx = RequestContext(purpose=EgressPurpose.LIVE_INGEST, run_mode=RunMode.LIVE)
        self.jobs: list[str] = []
        self.listed_at: datetime | None = None
        self.listing_complete = True
        self.state: dict[str, dict[str, Any]] = {}  # folder -> {etag, build_id, result, finished_at, checked_at}
        self._cursor = 0
        self.requests = 0

    # ------------------------------------------------------------------ transport

    def _get(self, url: str, etag: str | None = None) -> Any:
        self.requests += 1
        return self.gateway.request("ci", "GET", url, self.ctx,
                                    conditional={"If-None-Match": etag} if etag else None)

    # ------------------------------------------------------------------ listing

    def list_jobs(self) -> list[str]:
        """List job folders below every configured prefix, at most ``max_pages`` pages each."""
        found: list[str] = []
        complete = True
        for prefix in self.prefixes:
            marker: str | None = None
            for _ in range(self.max_pages):
                url = f"{self.base}?prefix={quote(prefix, safe='/-._~')}&delimiter=/&max-keys=1000"
                if marker:
                    url += f"&marker={quote(marker, safe='/-._~')}"
                resp = self._get(url)
                if resp.status != 200 or not isinstance(resp.body, str):
                    raise SourceUnavailable(f"job listing returned status {resp.status}")
                parent = prefix.rsplit("/", 1)[0] + "/" if "/" in prefix else ""
                folders, marker = parse_listing(resp.body, parent)
                found.extend(folders)
                if not marker:
                    break
            else:
                complete = False
        self.jobs = sorted(dict.fromkeys(found))[: self.max_jobs]
        self.listing_complete = complete and len(found) <= self.max_jobs
        self.listed_at = self.clock()
        return self.jobs

    # ------------------------------------------------------------------ latest builds

    def _check(self, folder: str) -> bool:
        """Refresh one job's latest build. Returns True when its result changed."""
        st = self.state.setdefault(folder, {})
        now = self.clock()
        latest = self._get(f"{self.base}/{folder}latest-build.txt", st.get("etag"))
        st["checked_at"] = now
        if latest.status == 304:
            return False
        if latest.status != 200:
            st.update(build_id=None, result=None)
            return False
        build_id = str(latest.body).strip()
        headers = {str(k).lower(): v for k, v in (latest.headers or {}).items()}
        fin = self._get(f"{self.base}/{folder}{build_id}/finished.json")
        finished = _as_mapping(fin.body) if fin.status == 200 else None
        if finished is None:
            # The build is still running: keep the previous result and look again next time.
            st["running_build"] = build_id
            return False
        changed = st.get("build_id") != build_id or st.get("result") != finished.get("result")
        ts = finished.get("timestamp")
        st.update(etag=headers.get("etag"), build_id=build_id, result=finished.get("result"), running_build=None,
                  finished_at=datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None)
        return changed

    def _dormant(self, folder: str) -> bool:
        st = self.state.get(folder) or {}
        done = st.get("finished_at")
        return bool(done and self.clock() - done > self.recent)

    def is_recent(self, folder: str) -> bool:
        return bool((self.state.get(folder) or {}).get("result")) and not self._dormant(folder)

    def poll(self) -> dict[str, Any]:
        """One bounded round: refresh the job list when due, then check up to ``checks_per_poll`` jobs."""
        now = self.clock()
        relisted = self.listed_at is None or now - self.listed_at >= self.relist_every
        if relisted:
            self.list_jobs()
        due = [j for j in self.jobs if j not in self.state] + [j for j in self.jobs if j in self.state and (relisted or not self._dormant(j))]
        due = list(dict.fromkeys(due))
        if due:
            self._cursor %= len(due)
            batch = (due[self._cursor:] + due[:self._cursor])[: self.checks_per_poll]
            self._cursor += len(batch)
        else:
            batch = []
        changed: list[str] = []
        for folder in batch:
            try:
                if self._check(folder):
                    changed.append(folder)
            except (GatewayRefused, SourceUnavailable):
                continue
        return {"changed": changed, "checked": len(batch), "jobs": len(self.jobs)}

    # ------------------------------------------------------------------ views

    def view(self, folder: str) -> dict[str, Any]:
        st = self.state.get(folder) or {}
        name = folder.rstrip("/").rsplit("/", 1)[-1]
        done = st.get("finished_at")
        return {"job": name, "kind": job_kind(folder), "build_id": st.get("build_id"), "result": st.get("result"),
                "finished_at": done.isoformat() if done else None, "path": f"{folder}{st.get('build_id')}"}

    def recent_jobs(self) -> list[dict[str, Any]]:
        return [self.view(j) for j in self.jobs if (self.state.get(j) or {}).get("result") and not self._dormant(j)]

    def failing(self) -> list[dict[str, Any]]:
        rows = [v for v in self.recent_jobs() if v["result"] in FAILED]
        rows.sort(key=lambda v: (v["kind"] != "periodic", v["finished_at"] or ""), reverse=False)
        return rows

    def coverage(self) -> dict[str, Any]:
        known = sum(1 for j in self.jobs if j in self.state)
        return {"jobs_listed": len(self.jobs), "jobs_checked": known, "listing_complete": self.listing_complete,
                "recent_jobs": len(self.recent_jobs())}
