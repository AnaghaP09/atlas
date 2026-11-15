"""Next-release carry-over forecast (a calculation, not an agent).

Each open item gets a carry-over risk score from age, priority, staleness, CI recurrence, component
churn and dependency fan-in. Probabilities are never shown as calibrated: every item carries
``probability_range: "illustrative"``. Quarantine-tracker issues are excluded by rule.
"""

from __future__ import annotations

from atlas.contracts import OpenItem

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

TRACKER_TITLE = re.compile(r"(?i)quarantine (transition )?tracker")


CHURN_WEIGHT = 1.0  # most the churn term can add to a score


def is_tracker(item: OpenItem) -> bool:
    return bool(TRACKER_TITLE.search(item.title)) or "lifecycle/frozen" in item.labels and "tracker" in item.title.lower()


def score(item: OpenItem, at: datetime) -> float:
    age_days = max((at - item.created).days, 0)
    stale_days = max((at - item.last_activity).days, 0)
    s = min(age_days / 90, 2.0)
    s += 1.5 if "priority/critical-urgent" in item.labels else 0.0
    s += 0.5 if any(l.startswith("lifecycle/stale") or l == "lifecycle/rotten" for l in item.labels) else 0.0
    s += min(stale_days / 60, 1.0)
    s += min(item.ci_recurrences / 5, 1.5)
    # Component churn: 0 to 20, where 20 is the owner team with the most commits in the 120 days
    # before the release. It adds at most CHURN_WEIGHT to the score.
    s += CHURN_WEIGHT * min(item.component_churn / 20, 1.0)
    s += min(item.fan_in / 5, 1.0)
    return round(s, 3)


HARDWARE_HINT = re.compile(
    r"(?i)(?<![a-z0-9])(s390x|arm64|aarch64|ppc64le|gpu|sr-?iov|usb passthrough|usb|sev|tdx|numa|hugepages?)(?![a-z0-9])")
# Many tests failing at once, or a whole test lane: the cause is not yet clear.
UNCLEAR_HINT = re.compile(
    r"(?i)(clustered failures?|lane instability|instability|tests failing|failing on every run|"
    r"\d+% failure|many tests|multiple tests|several tests|intermittent .* across)")
DEPENDENCY_HINT = re.compile(r"(?i)\b(bump|upgrade|update)\b.*\b(dependenc(y|ies)|module|go\.mod|library|version)\b")
DECISION_LABELS = ("release-blocker", "needs-decision", "kind/policy", "security-exception")
TEST_LABELS = {"kind/flake", "kind/failing-test"}
LARGE_LABELS = {"kind/enhancement", "kind/feature", "kind/api-change", "kind/design"}
OWNER_PREFIXES = ("sig/", "team/", "area/")


def owner_team(item: OpenItem) -> str | None:
    """The owner group named by the item's own labels (a group label, never a person), if any."""
    for prefix in OWNER_PREFIXES:
        found = sorted(l for l in item.labels if l.lower().startswith(prefix))
        if found:
            return found[0]
    return None


def propose_owner(item: OpenItem) -> tuple[str, str]:
    """Propose who should take a carried-over item, from the nature of the work. A proposal only.

    Returns ``(owner, reason)`` where owner is ``decision_maker``, ``developer`` or ``coding_agent`` and
    the reason is a few words. People decide the real assignment; this never names a person.

    * decide first: only real decisions (marked as a release blocker, a policy or security exception);
    * developer: several components, special hardware, an unclear cause or a large change;
    * coding agent: a bounded change in one component with a clear cause (a flaky or failing test,
      a dependency bump, a small fix that a failing test shows).

    Priority labels raise the forecast score; they do not decide the owner on their own.
    """
    labels = {l.lower() for l in item.labels}
    text = f"{item.title} {' '.join(sorted(labels))}"
    if any(l.startswith(d) for l in labels for d in DECISION_LABELS):
        return "decision_maker", "Marked as a release blocker" if any(
            l.startswith("release-blocker") for l in labels) else "Needs a policy decision"
    if item.fan_in >= 5:
        return "developer", "Touches several components"
    if item.component is None:
        return "developer", "No single owning component"
    if HARDWARE_HINT.search(text):
        return "developer", "Needs special hardware"
    if UNCLEAR_HINT.search(item.title):
        return "developer", "Many tests fail, cause unclear"
    if labels & LARGE_LABELS:
        return "developer", "Large change or new feature"
    if labels & TEST_LABELS:
        return "coding_agent", "One flaky test, one component"
    if DEPENDENCY_HINT.search(text) or "kind/dependency" in labels or "area/dependencies" in labels:
        return "coding_agent", "Dependency update in one component"
    if "kind/bug" in labels and ("kind/failing-test" in labels or "good-first-issue" in labels):
        return "coding_agent", "Small fix shown by a test"
    return "developer", "Code change, cause not shown"


def forecast(items: Sequence[OpenItem], at: datetime, top: int = 10, threshold: float = 2.0) -> dict[str, Any]:
    """Rank open items; return forecast items and the claims that bind them to their evidence."""
    candidates = [i for i in items if not is_tracker(i)]
    excluded = [i.issue_id for i in items if is_tracker(i)]
    ranked = sorted(candidates, key=lambda i: score(i, at), reverse=True)
    chosen = [i for i in ranked if score(i, at) >= threshold][:top]
    out_items, claims, at_risk = [], [], []
    for n, i in enumerate(chosen, start=1):
        cid = f"fc-{n}"
        claims.append({
            "id": cid,
            "text": f"Issue {i.issue_id} is likely to carry over into the next release (score {score(i, at)}).",
            "kind": "forecast_item",
            "evidence_ids": list(i.record_ids),
            "depends_on": [],
            "required_scopes": sorted(i.required_scopes),
        })
        out_items.append({
            "id": f"item-{i.issue_id}",
            "issue_id": i.issue_id,
            "title": i.title,
            "component": i.component,
            "kind": "carry_over",
            "probability_range": "illustrative",
            "score": score(i, at),
            "component_churn": i.component_churn,
            "evidence": list(i.record_ids),
            "provenance": "derived",
            "synthetic_ancestry": False,
            "required_scopes": sorted(i.required_scopes),
            "claim_ids": [cid],
            "allocation": propose_owner(i)[0],
            "allocation_reason": propose_owner(i)[1],
            "allocation_basis": "proposal",
            "owner_team": owner_team(i),
        })
        if i.requirement_bound:
            at_risk.append(i.issue_id)
    return {
        "items": out_items,
        "claims": claims,
        "excluded_trackers": excluded,
        "requirements_at_risk": at_risk,
        "date_range": "illustrative",
    }
