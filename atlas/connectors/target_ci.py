"""Target CI adapter: polls the demo target repository's workflow results for a published commit.

It is the only writer of CI observations, and it writes them only to the action-scoped
validation store. Reads are authorised by the durable publication record (destination,
action and published commit), not by any approval, so monitoring continues after an approval
expires and after the task is accepted or failed. Polling of one action is serialised by a lease.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from atlas.contracts import EgressPurpose, ValidationContext
from atlas.gateway import Gateway, PublicationRecord, RequestContext
from atlas.store import TARGET_CI_WRITER, ActionStore

API = "https://api.github.com"


class PollBusy(RuntimeError):
    pass


def observations_from_jobs(jobs_payload: Any, run_attempt: int) -> list[dict[str, Any]]:
    out = []
    for job in (jobs_payload or {}).get("jobs", []) if isinstance(jobs_payload, dict) else []:
        out.append({
            "job_id": job.get("id"),
            "run_attempt": job.get("run_attempt") or run_attempt,
            "name": job.get("name"),
            "status": job.get("status"),
            "conclusion": job.get("conclusion"),
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
            "head_sha": job.get("head_sha"),
        })
    return out


def observations_from_check_runs(payload: Any) -> list[dict[str, Any]]:
    out = []
    for cr in (payload or {}).get("check_runs", []) if isinstance(payload, dict) else []:
        out.append({
            "job_id": f"check-{cr.get('id')}",
            "run_attempt": 1,
            "name": cr.get("name"),
            "status": cr.get("status"),
            "conclusion": cr.get("conclusion"),
            "started_at": cr.get("started_at"),
            "completed_at": cr.get("completed_at"),
            "head_sha": cr.get("head_sha"),
        })
    return out


class TargetCIAdapter:
    def __init__(self, gateway: Gateway, store: ActionStore, *, clock: Callable[[], datetime] | None = None) -> None:
        self.gateway = gateway
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def poll(self, publication: PublicationRecord) -> int:
        """One poll. Returns the number of new observations stored."""
        vctx = ValidationContext(publication.destination_repo, publication.action_id, publication.published_commit)
        rctx = RequestContext(purpose=EgressPurpose.TARGET_CI_READ, publication=publication)
        holder = uuid.uuid4().hex
        if not self.store.acquire_poll(vctx, holder):
            raise PollBusy("another poll for this action is running")
        try:
            base = f"{API}/repos/{publication.destination_repo}"
            new = 0
            runs = self.gateway.request("target_ci", "GET", f"{base}/actions/runs?head_sha={publication.published_commit}", rctx)
            for run in (runs.body or {}).get("workflow_runs", []) if isinstance(runs.body, dict) else []:
                if run.get("head_sha") and run["head_sha"] != publication.published_commit:
                    continue
                jobs = self.gateway.request("target_ci", "GET", f"{base}/actions/runs/{run['id']}/jobs", rctx)
                for obs in observations_from_jobs(jobs.body, run.get("run_attempt") or 1):
                    new += self.store.write_ci_observation(vctx, TARGET_CI_WRITER, obs, self.clock())
            checks = self.gateway.request("target_ci", "GET", f"{base}/commits/{publication.published_commit}/check-runs", rctx)
            for obs in observations_from_check_runs(checks.body):
                new += self.store.write_ci_observation(vctx, TARGET_CI_WRITER, obs, self.clock())
            return new
        finally:
            self.store.release_poll(vctx, holder)
