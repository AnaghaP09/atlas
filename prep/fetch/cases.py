"""Fetch the raw data behind the golden cases and the release-close snapshots.

Usage (needs GITHUB_TOKEN in the environment; writes only to data/local and the manifest):

    python -m prep.fetch.cases golden
    python -m prep.fetch.cases releases
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from typing import Any

from atlas.connectors.normalize import issue_as_of
from atlas.store import from_iso
from prep.fetch.raw import RawCache, prow_paths
from prep.reference import reference

REF = reference()
GOLDEN_CASES: dict[str, dict[str, Any]] = REF.cases
TRIAGE_WINDOW = timedelta(minutes=int(REF.windows["triage_minutes"]))
BACKGROUND_DAYS = int(REF.windows["background_days"])
MERGED_PR_DAYS = int(REF.windows["merged_pr_days"])
RELEASE_TAGS = list(REF.releases["tags"])
REPO = REF.project.repository
ISSUE_REF = re.compile(r"(?:" + re.escape(REPO or "-") + r"/(?:issues|pull)/|(?<![\w/])#)(\d{4,6})\b")


def day(t: datetime) -> str:
    return t.strftime("%Y-%m-%d")


def case_cutoff(cache: RawCache, case: dict[str, Any]) -> datetime:
    if "release" in case:
        tag = cache.tag(case["release"])["data"]
        return from_iso(tag["tagged_at"])
    created = [from_iso(cache.issue(n)["data"]["createdAt"]) for n in case["issues"]]
    return max(created) + TRIAGE_WINDOW


def referenced_numbers(view: dict[str, Any]) -> set[int]:
    nums: set[int] = set()
    texts = [view.get("body") or ""] + [c.get("body") or "" for c in view.get("comments", [])]
    for text in texts:
        nums.update(int(m) for m in ISSUE_REF.findall(text))
    for ref in view.get("references", []):
        if ref.get("number"):
            nums.add(int(ref["number"]))
    return nums


def fetch_golden(cache: RawCache) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for case_id, case in GOLDEN_CASES.items():
        cutoff = case_cutoff(cache, case)
        info: dict[str, Any] = {"cutoff": cutoff.isoformat()}
        numbers = set(case.get("issues", [])) | set(case.get("context", []))
        # Background: flaky-test issues open in the 90 days before the cutoff.
        since = cutoff - timedelta(days=BACKGROUND_DAYS)
        found = cache.search(
            f"{case_id}-flakes",
            f"repo:{REPO} is:issue label:{REF.windows['background_label']} created:{day(since)}..{day(cutoff)}",
            limit=200,
        )["data"]
        numbers |= {n["number"] for n in found}
        # References made in the case issues as they stood at the cutoff.
        pr_numbers: set[int] = set(case.get("context_prs", []))
        for n in list(case.get("issues", [])):
            view = issue_as_of(cache.issue(n)["data"], cutoff)
            if view:
                for ref in referenced_numbers(view.view):
                    numbers.add(ref)
        # Recently merged pull requests on the main branch.
        merged = cache.search(
            f"{case_id}-merged",
            f"repo:{REPO} is:pr is:merged base:{REF.windows['main_branch']} merged:{day(cutoff - timedelta(days=MERGED_PR_DAYS))}..{day(cutoff)}",
            limit=400,
        )["data"]
        pr_numbers |= {n["number"] for n in merged}
        issues_ok, prs_ok, logs = 0, 0, []
        for n in sorted(numbers):
            data = cache.issue(n)["data"]
            if data is None:
                # The number may be a pull request.
                pr_numbers.add(n)
                continue
            issues_ok += 1
        for n in sorted(pr_numbers):
            if cache.pull_request(n)["data"] is not None:
                prs_ok += 1
        for n in case.get("issues", []):
            view = issue_as_of(cache.issue(n)["data"], cutoff)
            texts = [view.view.get("body") or ""] + [c.get("body") or "" for c in view.view.get("comments", [])] if view else []
            for text in texts:
                for path in prow_paths(text)[:2]:
                    log = cache.prow_artifact(path, "build-log.txt")["data"]
                    cache.prow_artifact(path, "finished.json", max_lines=2000)
                    logs.append({"path": path, "status": log.get("status"), "lines": log.get("line_count")})
        info.update({"issues": issues_ok, "pull_requests": prs_ok, "logs": logs, "issue_numbers": sorted(numbers), "pr_numbers": sorted(pr_numbers)})
        summary[case_id] = info
        print(case_id, cutoff.isoformat(), "issues", issues_ok, "prs", prs_ok, "logs", logs, flush=True)
        cache.manifest.save()
    return summary


def fetch_releases(cache: RawCache) -> dict[str, Any]:
    tags = {t: cache.tag(t)["data"] for t in RELEASE_TAGS}
    summary: dict[str, Any] = {"tags": tags}
    rel = REF.releases
    major = rel["major"]
    for minor in rel["minors"]:
        beta = from_iso(tags[f"v{major}.{minor}.0-beta.0"]["tagged_at"])
        final = from_iso(tags[f"v{major}.{minor}.0"]["tagged_at"])
        prev = from_iso(tags[f"v{major}.{minor - 1}.0"]["tagged_at"])
        backports = cache.search(
            f"backports-{major}.{minor}",
            f"repo:{REPO} is:pr is:merged base:{rel['branch_pattern'].format(major=major, minor=minor)} merged:{day(beta)}..{day(final)}",
            limit=400,
        )["data"]
        for pr in backports:
            cache.pull_request(pr["number"])
        # Candidate items open at the previous release close.
        open_now = cache.search(
            f"open-at-{major}.{minor - 1}-open",
            f"repo:{REPO} is:issue is:open label:{rel['candidate_labels']} created:<{day(prev)}",
            limit=1000,
        )["data"]
        closed_later = cache.search(
            f"open-at-{major}.{minor - 1}-closed-later",
            f"repo:{REPO} is:issue is:closed label:{rel['candidate_labels']} created:<{day(prev)} closed:>{day(prev)}",
            limit=1000,
        )["data"]
        candidates = sorted({n["number"] for n in open_now + closed_later})
        for n in candidates:
            cache.issue(n)
        summary[f"{major}.{minor}"] = {"backports": len(backports), "candidates": len(candidates)}
        print(f"{major}.{minor}", summary[f"{major}.{minor}"], flush=True)
        cache.manifest.save()
    return summary


def main(argv: list[str]) -> int:
    cache = RawCache()
    what = argv[1] if len(argv) > 1 else "golden"
    if what == "golden":
        fetch_golden(cache)
    elif what == "releases":
        fetch_releases(cache)
    else:
        print("usage: python -m prep.fetch.cases golden|releases")
        return 2
    cache.manifest.save()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
