"""Forecast scoring on canonical prediction identities.

A forecast can mention the same issue several times (in several reports, or as both an issue and a
failing test). Scoring first collapses every mention to one identity per (target release, issue).
An item that names a component and a failing test maps to every open bug or flake that reports that
test in that component at the forecast's snapshot. An item that maps to no issue counts once as an
"unmapped" prediction keyed by (release, component, test). Matching to proxy blockers is one to one.

Recall counts only blockers that were already open when the forecast was made (open in the data set
the forecast used); a blocker reported later could not have been named. Precision is unchanged: the
share of named items that turned out to be blockers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

NOTE = ("Counts only blockers already open when the forecast was made; uses a substitute for release blockers "
        "because KubeVirt does not record them.")
LIMITATION = (
    NOTE + " Substitute: a bug or flaky-test issue fixed on the release branch between the first beta and the "
    "release, or critical-urgent and still open at the release, that was already open in the data set the "
    "forecast was made from (observed 14 Nov 2026: release-blocker labels only for 1.6 with 2 items and 1.7 with "
    "1 item, none for 1.8 to 1.10, no milestones). A tracker with explicit release blockers would replace it."
)


def _norm_issue(value: Any) -> str:
    """``owner/repo#123``, ``repo#123``, ``#123`` and ``123`` all become ``123``."""
    text = str(value).strip()
    return text.rsplit("#", 1)[-1] if "#" in text else text


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
    """``matched``/``blockers`` is recall over blockers open at forecast time; ``named_blockers``/``predictions``
    is precision over every named item that turned out to be a blocker."""

    release: str
    matched: int
    blockers: int
    predictions: int
    named_blockers: int = -1
    unmatched_predictions: list[tuple[str, ...]] = field(default_factory=list)
    missed_blockers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.named_blockers < 0:
            self.named_blockers = self.matched

    @property
    def recall(self) -> float | None:
        return self.matched / self.blockers if self.blockers else None

    @property
    def precision(self) -> float | None:
        return self.named_blockers / self.predictions if self.predictions else None


def score_close(release: str, predictions: set[tuple[str, ...]], blockers: Iterable[str],
                all_blockers: Iterable[str] | None = None) -> CloseScore:
    """``blockers``: labelled blockers open at forecast time (the recall denominator).

    ``all_blockers``: every labelled blocker, for precision; defaults to ``blockers``."""
    blocker_ids = {_norm_issue(b) for b in blockers}
    any_blocker = blocker_ids | {_norm_issue(b) for b in (all_blockers or ())}
    predicted_issues = {p[2] for p in predictions if p[0] == "issue"}
    matched = blocker_ids & predicted_issues
    named = any_blocker & predicted_issues
    return CloseScore(release=release, matched=len(matched), blockers=len(blocker_ids), predictions=len(predictions),
                      named_blockers=len(named),
                      unmatched_predictions=sorted(p for p in predictions if not (p[0] == "issue" and p[2] in named)),
                      missed_blockers=sorted(blocker_ids - matched))


def _pct(part: int, whole: int) -> str:
    return f"{part} of {whole} ({part / whole:.0%})" if whole else f"{part} of 0 (no denominator)"


def line(label: str, matched: int, blockers: int, named: int, predictions: int) -> str:
    """One result line. The note sits beside the numbers, every time."""
    return (f"{label}: recall {_pct(matched, blockers)}, precision {_pct(named, predictions)}. {NOTE}")


def g13(closes: Sequence[Mapping[str, Any]], *, recall_bar: float = 0.60, precision_bar: float = 0.50) -> dict[str, Any]:
    """Pooled and per-release recall and precision, each with the note, and the limitation statement.

    Each close holds ``blockers`` (labelled blockers open at forecast time) and may hold ``all_blockers``
    (every labelled blocker, used only for precision). A pooled denominator of zero fails; a release with
    an empty denominator is listed in ``empty_denominators`` and its number reads "no denominator"."""
    per_close = []
    matched = blockers = predictions = named = 0
    empty: list[str] = []
    for c in closes:
        preds = canonical_predictions(c["release"], c.get("items", []), c.get("test_index"), c.get("alias_map"))
        s = score_close(c["release"], preds, c.get("blockers", []), c.get("all_blockers"))
        if not s.blockers:
            empty.append(f"{s.release}: no labelled blocker open when the forecast was made")
        if not s.predictions:
            empty.append(f"{s.release}: the forecast named nothing")
        per_close.append({"release": s.release, "matched": s.matched, "blockers": s.blockers,
                          "named_blockers": s.named_blockers, "predictions": s.predictions,
                          "recall": s.recall, "precision": s.precision, "missed_blockers": s.missed_blockers,
                          "note": NOTE, "text": line(f"Release {s.release}", s.matched, s.blockers,
                                                      s.named_blockers, s.predictions)})
        matched += s.matched
        blockers += s.blockers
        predictions += s.predictions
        named += s.named_blockers
    recall = matched / blockers if blockers else None
    precision = named / predictions if predictions else None
    if not closes:
        status = "unevaluated"
    elif recall is None or precision is None:
        status = "failed"  # a pooled denominator of zero fails; an empty release shows "no denominator"
    else:
        status = "passed" if recall >= recall_bar and precision >= precision_bar else "failed"
    pooled = {"matched": matched, "blockers": blockers, "named_blockers": named, "predictions": predictions,
              "recall": recall, "precision": precision, "note": NOTE,
              "text": line("Pooled", matched, blockers, named, predictions)}
    return {"status": status, "pooled": pooled, "per_close": per_close, "empty_denominators": empty,
            "bars": {"recall": recall_bar, "precision": precision_bar}, "limitation": LIMITATION, "note": NOTE,
            "text": [pooled["text"]] + [c["text"] for c in per_close]}
