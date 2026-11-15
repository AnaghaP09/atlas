"""Plain display fields for data sets ("snapshots"), so people never have to read snapshot ids.

Release closes read as "KubeVirt at release v1.9.0", example cases as "Example case: <short title>",
copies with disguised names are marked as test copies, and every item says when its data is from.
Counts and case titles come from the frozen data itself and are cached per data set.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

_CACHE: dict[str, dict[str, Any]] = {}


def is_test_copy(snap: Mapping[str, Any]) -> bool:
    kind = str(snap.get("kind") or "")
    return bool(snap.get("alias_variant")) or kind.endswith("-aliased") or kind in ("injection", "new_evidence")


def kind_of(snap: Mapping[str, Any]) -> str:
    kind = str(snap.get("kind") or "")
    if kind.startswith("releases"):
        return "release"
    if kind.startswith("cases") or kind in ("injection", "new_evidence"):
        return "case"
    return "live"


def short_title(title: str, limit: int = 60) -> str:
    t = re.sub(r"\s+", " ", title).strip()
    t = re.sub(r"^\[[^\]]+\]\s*", "", t)  # drop leading tags such as "[sig-network]"
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _summary(snapshots: Any, snap: Mapping[str, Any]) -> dict[str, Any]:
    """Open issues and the first issue's title, read from the frozen data. Empty when the data layer has none."""
    sid = str(snap.get("snapshot_id"))
    if sid in _CACHE:
        return _CACHE[sid]
    out: dict[str, Any] = {}
    registry = getattr(snapshots, "registry", None)
    ctx_of = getattr(snapshots, "_ctx", None)
    if registry is not None and ctx_of is not None:
        try:
            ctx = ctx_of(sid)
            db = registry.open(ctx) if ctx is not None else None
            if db is not None:
                issues = []
                for rec in db.search({"source": "issues", "limit": 2000}):
                    view = db.content(rec.record_id) or {}
                    if isinstance(view, Mapping) and view.get("number") is not None:
                        issues.append(view)
                open_issues = [v for v in issues if str(v.get("state", "open")).lower() == "open"]
                out["open_issues"] = len(open_issues)
                out["linked_issues"] = len(issues)
                first = min(issues, key=lambda v: str(v.get("created_at") or ""), default=None)
                if first and first.get("title"):
                    out["case_title"] = short_title(str(first["title"]))
        except Exception:  # a display nicety must never break the list
            out = {}
    _CACHE[sid] = out
    return out


_EVENTS: dict[str, list[dict[str, Any]]] = {}
_FAILURE_WORDS = re.compile(r"\b(flak\w*|fail\w*|broken|timeout|times out|error|crash\w*|panic\w*)\b", re.I)


def events(snapshots: Any, snap: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The issues in one data set that can be investigated, newest first, with their titles.

    Failures (flaky tests, failing jobs, bugs) carry ``failure: true`` so pickers can list them first.
    Empty when the data layer has none.
    """
    sid = str(snap.get("snapshot_id"))
    if sid in _EVENTS:
        return _EVENTS[sid]
    out: list[dict[str, Any]] = []
    registry = getattr(snapshots, "registry", None)
    ctx_of = getattr(snapshots, "_ctx", None)
    if registry is not None and ctx_of is not None:
        try:
            ctx = ctx_of(sid)
            db = registry.open(ctx) if ctx is not None else None
            if db is not None:
                seen: set[str] = set()
                for rec in db.search({"source": "issues", "limit": 5000}):
                    view = db.content(rec.record_id) or {}
                    if not isinstance(view, Mapping) or view.get("number") is None:
                        continue
                    number = str(view["number"])
                    if number in seen:
                        continue
                    seen.add(number)
                    labels = [str(lb.get("name") if isinstance(lb, Mapping) else lb) for lb in (view.get("labels") or [])]
                    title = str(view.get("title") or "")
                    failure = any(("flake" in lb or "bug" in lb) for lb in labels) or bool(_FAILURE_WORDS.search(title))
                    out.append({"event_id": number, "number": int(number) if number.isdigit() else None,
                                "title": short_title(title, 120) if title else f"Issue {number}",
                                "state": str(view.get("state") or "open").lower(),
                                "created_at": view.get("created_at"), "labels": labels[:6], "failure": failure})
        except Exception:  # a picker list must never break the page
            out = []
    out.sort(key=lambda e: str(e.get("created_at") or ""), reverse=True)
    _EVENTS[sid] = out
    return out


def _date(iso: str | None) -> str:
    if not iso:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso)
    if not m:
        return iso
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{int(m.group(3))} {months[int(m.group(2)) - 1]} {m.group(1)}"


def describe(snapshots: Any, snap: Mapping[str, Any], project_name: str | None) -> dict[str, Any]:
    out = dict(snap)
    kind = kind_of(snap)
    test_copy = is_test_copy(snap)
    as_of = snap.get("cutoff") or snap.get("frozen_at")
    summary = _summary(snapshots, snap) if not test_copy or kind == "case" else {}
    name = project_name or str(snap.get("project_id") or "Project")
    release = str(snap.get("release_or_case") or snap.get("release") or "")
    release_tag = re.search(r"v\d+\.\d+\.\d+", str(snap.get("snapshot_id", "")))
    if kind == "release" and snap.get("title"):
        title = str(snap["title"])  # a copy built by Add project names itself
    elif kind == "release":
        title = f"{name} at release {release_tag.group(0) if release_tag else release}"
    elif kind == "case":
        title = f"Example case: {summary.get('case_title') or release}"
    else:
        title = f"{name} live data"
    out.update({
        "display_kind": kind,
        "is_test_copy": test_copy,
        "as_of": as_of,
        "as_of_label": _date(str(as_of) if as_of else None),
        "title": title,
        "open_issues": summary.get("open_issues"),
        "linked_issues": summary.get("linked_issues"),
    })
    return out
