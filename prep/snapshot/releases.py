"""Release-close snapshots and proxy-blocker candidates for forecast validity.

For each release close (the tags listed in the reference definition) the snapshot holds the
bug and flaky-test issues open at the tag, as they stood at the tag, plus the architecture
graph of that tag and synthetic capacity. Nothing created, edited, labelled, back-ported or
merged after the tag is visible.

Proxy-blocker candidates for release N (issues whose fix was back-ported into the release branch
between its first beta and its final tag, and critical-urgent bugs or flakes still open at the
final tag) are written only to the evaluator zone, unlabelled: people adjudicate them.

Usage: python -m prep.snapshot.releases
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from typing import Any

from atlas.connectors.normalize import issue_as_of, pull_request_as_of, to_record
from atlas.connectors.synthetic import capacity_records
from atlas.graph import ArchitectureGraph, architecture_records
from atlas.store import AUTHORITATIVE_WRITERS, EvidenceDB, from_iso
from prep.fetch.raw import RawCache
from prep.reference import paths, reference
from prep.snapshot.build import LeakageError, _load, check_export_people, collect_people, export
from prep.snapshot.people import People

REF = reference()
REPO = REF.project.repository
CHERRY = re.compile(r"cherry[- ]pick(?:ed)? of #(\d+)", re.I)
FIXES = re.compile(r"(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)\s*:?\s*(?:#|https://github\.com/" + re.escape(REPO or "-") + r"/issues/)(\d+)", re.I)
CRITICAL = "priority/critical-urgent"


def tag_time(tag: str) -> datetime:
    return from_iso(_load("tags", tag)["data"]["tagged_at"])


def _numbers(key: str) -> list[int]:
    data = _load("search", key)
    return [n["number"] for n in ((data or {}).get("data") or [])]


def close_candidates(cache: RawCache, tag: str, labels: str) -> list[int]:
    """Bug and flake issues created before the tag that are open now or were closed after it."""
    cutoff = tag_time(tag)
    day = cutoff.strftime("%Y-%m-%d")
    open_now = cache.search(f"close-{tag}-open", f"repo:{REPO} is:issue is:open label:{labels} created:<{day}", 1000)["data"]
    closed_later = cache.search(f"close-{tag}-closed", f"repo:{REPO} is:issue is:closed label:{labels} created:<{day} closed:>{day}", 1000)["data"]
    nums = sorted({n["number"] for n in open_now + closed_later})
    for n in nums:
        cache.issue(n)
    return nums


def build_close(tag: str, numbers: list[int], *, label_filter: set[str], people: People) -> tuple[EvidenceDB, dict[str, Any]]:
    cutoff = tag_time(tag)
    build = paths()["build"]
    build.mkdir(parents=True, exist_ok=True)
    path = build / f"release-close-{tag}.sqlite"
    if path.exists():
        path.unlink()
    db = EvidenceDB(path)
    stats: dict[str, Any] = {"issues": 0, "excluded_not_open": 0, "excluded_label": 0, "unknown_state": 0, "architecture": 0, "synthetic": 0}
    for n in numbers:
        raw = _load("issues", str(n))
        if not raw or not raw.get("data"):
            continue
        asof = issue_as_of(raw["data"], cutoff)
        if asof is None:
            continue
        if asof.view["state"] == "UNKNOWN":
            stats["unknown_state"] += 1
            continue
        if asof.view["state"] != "OPEN":
            stats["excluded_not_open"] += 1
            continue
        if not (set(asof.view["labels"]) & label_filter):
            stats["excluded_label"] += 1
            continue
        fetched = from_iso(raw["fetched_at"])
        record, extra = to_record("issues", asof, cutoff=cutoff, fetched_at=fetched, history_complete_through=fetched)
        db.put(record, AUTHORITATIVE_WRITERS["issues"], **extra)
        stats["issues"] += 1
    arch_path = paths()["data"] / "snapshots" / "architecture" / f"{tag}.json"
    if arch_path.exists():
        graph = ArchitectureGraph.from_json(json.loads(arch_path.read_text(encoding="utf-8")))
        for record, content, etype in architecture_records(graph, cutoff - timedelta(seconds=1)):
            db.put(record, AUTHORITATIVE_WRITERS["architecture"], content=content, entity_type=etype, freshness_basis="identity")
            stats["architecture"] += 1
    for record, content, etype in capacity_records(cutoff):
        db.put(record, AUTHORITATIVE_WRITERS["capacity"], content=content, entity_type=etype, freshness_basis="per_run")
        stats["synthetic"] += 1
    return db, {"cutoff": cutoff, "stats": stats}


def check_close_leakage(db: EvidenceDB, cutoff: datetime, prohibited_prs: set[int]) -> list[str]:
    problems = []
    for rec in db.all_records():
        if rec.valid_from > cutoff:
            problems.append(f"{rec.record_id}: content after the close")
        if rec.source == "git" and rec.source_id.startswith("pr/") and int(rec.source_id.split("/")[1]) in prohibited_prs:
            problems.append(f"{rec.record_id}: later back-port present")
        content = db.content(rec.record_id) or {}
        if isinstance(content, dict):
            for ref in content.get("references", []):
                if ref.get("number") in prohibited_prs:
                    problems.append(f"{rec.record_id}: relationship to a later back-port")
    return problems


def proxy_candidates(cache: RawCache, minor: int) -> dict[str, Any]:
    rel = REF.releases
    major = rel["major"]
    beta, final = tag_time(f"v{major}.{minor}.0-beta.0"), tag_time(f"v{major}.{minor}.0")
    prev_close = tag_time(f"v{major}.{minor - 1}.0")
    backports = _numbers(f"backports-{major}.{minor}")
    rows: dict[int, dict[str, Any]] = {}
    pr_records = []
    for b in backports:
        pr = (_load("pulls", str(b)) or {}).get("data")
        if not pr or not pr.get("mergedAt") or not (beta <= from_iso(pr["mergedAt"]) <= final):
            continue
        origin = CHERRY.search(pr.get("body") or "")
        linked: set[int] = set(int(x) for x in FIXES.findall(pr.get("body") or ""))
        main_pr = int(origin.group(1)) if origin else None
        if main_pr:
            main = (cache.pull_request(main_pr) or {}).get("data")
            if main:
                linked |= set(int(x) for x in FIXES.findall(main.get("body") or ""))
        pr_records.append({"backport_pr": b, "merged_at": pr["mergedAt"], "origin_pr": main_pr, "linked_issues": sorted(linked), "title": pr.get("title")})
        for n in linked:
            issue = (cache.issue(n) or {}).get("data")
            if not issue:
                continue
            labels_now = {l["name"] for l in (issue.get("labels") or {}).get("nodes", [])}
            rows.setdefault(n, {"issue": n, "reasons": [], "created_at": issue["createdAt"],
                                "open_at_previous_close": bool(issue_as_of(issue, prev_close) and issue_as_of(issue, prev_close).view["state"] == "OPEN"),
                                "kinds_now": sorted(l for l in labels_now if l.startswith("kind/"))})
            rows[n]["reasons"].append({"kind": "backport", "backport_pr": b, "origin_pr": main_pr})
    for n in _numbers(f"close-v{major}.{minor}.0-open") + _numbers(f"close-v{major}.{minor}.0-closed"):
        issue = (_load("issues", str(n)) or {}).get("data")
        asof = issue_as_of(issue, final) if issue else None
        if asof and asof.view["state"] == "OPEN" and CRITICAL in asof.view["labels"]:
            rows.setdefault(n, {"issue": n, "reasons": [], "created_at": issue["createdAt"],
                                "open_at_previous_close": bool(issue_as_of(issue, prev_close) and issue_as_of(issue, prev_close).view["state"] == "OPEN"),
                                "kinds_now": sorted(l for l in asof.view["labels"] if l.startswith("kind/"))})
            rows[n]["reasons"].append({"kind": "critical_open_at_final_tag"})
    return {
        "release": f"{major}.{minor}",
        "window": {"from_beta": beta.isoformat(), "to_final": final.isoformat(), "forecast_cutoff": prev_close.isoformat()},
        "definition": "Candidate proxy blockers: issues linked to a fix merged into the release branch between the first beta and the final tag, and critical-urgent issues still open at the final tag. Unlabelled: two people adjudicate every candidate (features and chores are excluded).",
        "limitation": "The project does not record release blockers, so this proxy is used.",
        "backports": pr_records,
        "candidates": [rows[k] for k in sorted(rows)],
    }


def main(argv: list[str]) -> int:
    cache = RawCache()
    people = People()
    rel = REF.releases
    major = rel["major"]
    closes = [f"v{major}.{m - 1}.0" for m in rel["minors"]] + [f"v{major}.{rel['minors'][-1]}.0"]
    built = {}
    for tag in closes:
        nums = close_candidates(cache, tag, rel["candidate_labels"])
        label_filter = set(rel["candidate_labels"].split(","))
        db, info = build_close(tag, nums, label_filter=label_filter, people=people)
        later_backports = set()
        for m in rel["minors"]:
            later_backports |= set(_numbers(f"backports-{major}.{m}"))
        problems = check_close_leakage(db, info["cutoff"], later_backports)
        if problems:
            raise LeakageError(f"{tag}: " + "; ".join(problems[:5]))
        collect_people(db, people)
        built[tag] = (db, info)
    cache.manifest.save()
    out_root = paths()["export"]
    for tag, (db, info) in built.items():
        manifest = {
            "release_or_case": f"release close {tag}",
            "cutoff": info["cutoff"].isoformat(),
            "cutoff_rule": "the release tag time",
            "alias_variant": None,
            "counts": info["stats"],
            "notes": ["Open bug and flaky-test issues as they stood at the release tag; capacity is synthetic."],
            "data_modes": {"issues": "REAL", "architecture": "REAL", "capacity": "SYNTHETIC"},
        }
        target = export(db, f"release-close-{tag}", "releases", manifest, people, root=out_root)
        db.close()
        print(tag, info["cutoff"].isoformat(), info["stats"], flush=True)
    people.write_handles_file()
    problems = []
    for tag in built:
        problems += check_export_people(out_root / "snapshots" / "releases" / f"release-close-{tag}", people)
    if problems:
        raise LeakageError("; ".join(problems))
    outcomes = out_root / "evaluator" / "release_outcomes"
    outcomes.mkdir(parents=True, exist_ok=True)
    for m in rel["minors"]:
        data = people.scrub(proxy_candidates(cache, m))
        (outcomes / f"{major}.{m}.json").write_bytes((json.dumps(data, indent=1, sort_keys=True) + "\n").encode("utf-8"))
        print(f"{major}.{m}", "backports", len(data["backports"]), "candidates", len(data["candidates"]), flush=True)
    cache.manifest.save()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
