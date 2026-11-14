"""Forecast scoring on canonical prediction identities.

A forecast can mention the same issue several times (in several reports, or as both an issue and a
failing test). Scoring first collapses every mention to one identity per (target release, issue).
An item that names a component and a failing test maps to every open bug or flake that reports that
test in that component at the forecast's snapshot. An item that maps to no issue counts once as an
"unmapped" prediction keyed by (release, component, test). Matching to proxy blockers is one to one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

LIMITATION = (
    "Measured on a proxy: KubeVirt does not record release blockers (observed 14 Nov 2026: release-blocker "
    "labels only for 1.6 with 2 items and 1.7 with 1 item, none for 1.8 to 1.10, no milestones). Proxy = bug or "
    "flake fixed on the release branch between branch cut and release, or critical-urgent and open at release. "
    "A pilot tracker with explicit blockers replaces this proxy."
)


def _norm_issue(value: Any) -> str:
    text = str(value).strip()
    for prefix in ("kubevirt#", "#"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def unalias(value: str, alias_map: Mapping[str, str] | None) -> str:
    if not alias_map:
        return value
    return alias_map.get(value, value)


def canonical_predictions(release: str, items: Iterable[Mapping[str, Any]],
                          test_index: Mapping[tuple[str, str], Sequence[str]] | None = None,
                          alias_map: Mapping[str, str] | None = None) -> set[tuple[str, ...]]:
    """One identity per (release, issue); unmapped component/test items as (release, component, test)."""
    test_index = test_index or {}
    out: set[tuple[str, ...]] = set()
    for item in items:
        issue = item.get("issue_id")
        if issue:
            out.add(("issue", release, _norm_issue(unalias(str(issue), alias_map))))
            continue
        component = unalias(str(item.get("component", "")), alias_map)
        test = unalias(str(item.get("test", "")), alias_map)
        mapped = test_index.get((component, test), ())
        if mapped:
            for m in mapped:
                out.add(("issue", release, _norm_issue(m)))
        else:
            out.add(("unmapped", release, component, test))
    return out


@dataclass
class CloseScore:
    release: str
    matched: int
    blockers: int
    predictions: int
    unmatched_predictions: list[tuple[str, ...]] = field(default_factory=list)
    missed_blockers: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        return self.matched / self.blockers if self.blockers else None

    @property
    def precision(self) -> float | None:
        return self.matched / self.predictions if self.predictions else None


def score_close(release: str, predictions: set[tuple[str, ...]], blockers: Iterable[str]) -> CloseScore:
    blocker_ids = {_norm_issue(b) for b in blockers}
    predicted_issues = {p[2] for p in predictions if p[0] == "issue"}
    matched = blocker_ids & predicted_issues
    return CloseScore(release=release, matched=len(matched), blockers=len(blocker_ids), predictions=len(predictions),
                      unmatched_predictions=sorted(p for p in predictions if not (p[0] == "issue" and p[2] in matched)),
                      missed_blockers=sorted(blocker_ids - matched))


def g13(closes: Sequence[Mapping[str, Any]], *, recall_bar: float = 0.60, precision_bar: float = 0.50) -> dict[str, Any]:
    """Pooled recall and precision over the closes, per-close numbers, and the limitation statement."""
    per_close = []
    matched = blockers = predictions = 0
    for c in closes:
        preds = canonical_predictions(c["release"], c.get("items", []), c.get("test_index"), c.get("alias_map"))
        s = score_close(c["release"], preds, c.get("blockers", []))
        per_close.append({"release": s.release, "matched": s.matched, "blockers": s.blockers,
                          "predictions": s.predictions, "recall": s.recall, "precision": s.precision,
                          "missed_blockers": s.missed_blockers})
        matched += s.matched
        blockers += s.blockers
        predictions += s.predictions
    recall = matched / blockers if blockers else None
    precision = matched / predictions if predictions else None
    if not closes:
        status = "unevaluated"
    elif recall is None or precision is None:
        status = "failed"  # a pooled denominator of zero fails
    else:
        status = "passed" if recall >= recall_bar and precision >= precision_bar else "failed"
    return {"status": status, "pooled": {"matched": matched, "blockers": blockers, "predictions": predictions,
                                         "recall": recall, "precision": precision},
            "per_close": per_close, "bars": {"recall": recall_bar, "precision": precision_bar},
            "limitation": LIMITATION}
