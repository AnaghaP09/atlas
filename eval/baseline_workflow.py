"""The simple conditional workflow ATLAS is compared against.

Fixed rules, written down before any ATLAS evaluation and never tuned afterwards:

1. If the failure log matches an environment signature, classify ENVIRONMENT and hand the failure to
   the infrastructure owner.
2. If the report carries no failure identifier at all (no job, test or issue id), ask for one:
   UNKNOWN, escalate to a person.
3. Otherwise blame the first commit merged before the first failure: CODE_DEFECT, assign a developer.

Each rule costs one tool call per evidence item it reads; the comparison uses that as the cost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

ENVIRONMENT_SIGNATURES = (
    r"\bmultus\b", r"\bCNI\b", r"network plugin", r"node(s)? not ready", r"no space left on device",
    r"\bImagePullBackOff\b", r"connection refused.*(api ?server|cluster)", r"etcd.*(timeout|saturat)",
    r"OOMKilled", r"context deadline exceeded.*(registry|pull)",
)
_ENV = re.compile("|".join(ENVIRONMENT_SIGNATURES), re.IGNORECASE)


@dataclass(frozen=True)
class WorkflowResult:
    classification: str
    action: str
    rule: str
    tool_calls: int
    blamed_commit: str | None = None


def run(case: Mapping[str, Any]) -> WorkflowResult:
    """``case``: ``{"log": str, "failure_ids": [...], "commits": [{"sha", "merged_at"}], "first_failure_at"}``."""
    calls = 1
    log = str(case.get("log", ""))
    if _ENV.search(log):
        return WorkflowResult("ENVIRONMENT", "HAND_TO_INFRA_OWNER", "environment_signature", calls)
    calls += 1
    if not case.get("failure_ids"):
        return WorkflowResult("UNKNOWN", "ESCALATE_DECISION", "missing_identifier", calls)
    calls += 1
    commits: Sequence[Mapping[str, Any]] = case.get("commits", [])
    first_failure = case.get("first_failure_at")
    before = [c for c in commits if first_failure is None or str(c.get("merged_at", "")) <= str(first_failure)]
    blamed = max(before, key=lambda c: str(c.get("merged_at", "")))["sha"] if before else None
    return WorkflowResult("CODE_DEFECT", "ASSIGN_DEVELOPER", "first_failing_commit", calls, blamed)
