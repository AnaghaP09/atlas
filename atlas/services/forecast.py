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
            "component": i.component,
            "kind": "carry_over",
            "probability_range": "illustrative",
            "score": score(i, at),
            "evidence": list(i.record_ids),
            "provenance": "derived",
            "synthetic_ancestry": False,
            "required_scopes": sorted(i.required_scopes),
            "claim_ids": [cid],
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
