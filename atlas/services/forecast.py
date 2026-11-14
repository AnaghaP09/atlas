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
    s += min(item.component_churn / 20, 1.0)
    s += min(item.fan_in / 5, 1.0)
    return round(s, 3)


HARDWARE_HINT = re.compile(r"(?i)(?<![a-z0-9])(s390x|arm64|aarch64|ppc64le|gpu|sr-?iov|usb passthrough)(?![a-z0-9])")


def propose_owner(item: OpenItem) -> tuple[str, str]:
    """Propose who should take a carried-over item. A rule over labels, shown as a proposal only.

    Returns ``(owner, reason)`` where owner is ``decision_maker``, ``developer`` or ``coding_agent``.
    People decide the real assignment; this never names a person.
    """
    labels = {l.lower() for l in item.labels}
    text = f"{item.title} {' '.join(sorted(labels))}"
    if any(l.startswith("release-blocker") for l in labels) or "priority/critical-urgent" in labels:
        return "decision_maker", "urgent or release-blocking"
    if HARDWARE_HINT.search(text):
        return "developer", "needs special hardware"
    if item.fan_in >= 5 or item.component is None:
        return "developer", "touches several components"
    if "kind/flake" in labels:
        return "coding_agent", "single-component flaky test"
    return "developer", "code change in one component"


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
            "evidence": list(i.record_ids),
            "provenance": "derived",
            "synthetic_ancestry": False,
            "required_scopes": sorted(i.required_scopes),
            "claim_ids": [cid],
            "allocation": propose_owner(i)[0],
            "allocation_reason": propose_owner(i)[1],
            "allocation_basis": "proposal",
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
