"""Build as-of snapshots for the golden cases and the release closes, check them, export them.

Order of work for every snapshot (the export order of the data plan):

1. content-version reconstruction as of the cutoff (atlas.connectors.normalize);
2. leakage checks: temporal (nothing after the cutoff) and outcome (no prohibited record or
   relationship);
3. pseudonymisation of people;
4. aliasing (aliased variants only; see prep.alias);
5. provenance attachment under the variant's policy;
6. write of the export (records.jsonl, conflicts.jsonl, snapshot.json);
7. checks on the final export.

Usage: python -m prep.snapshot.build golden
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from atlas.connectors.ci import job_view
from atlas.connectors.normalize import AsOf, issue_as_of, pull_request_as_of, to_record
from atlas.connectors.synthetic import all_synthetic
from atlas.contracts import Record
from atlas.store import AUTHORITATIVE_WRITERS, EvidenceDB, from_iso
from atlas.snapshots import normalized_sha256
from prep.fetch.cases import GOLDEN_CASES, TRIAGE_WINDOW
from prep.fetch.raw import RAW, prow_paths
from prep.reference import paths, reference
from prep.snapshot.people import People

LOCAL = paths()["local"]
EXPORT = paths()["export"]
BUILD = paths()["build"]
ROOT = paths()["data"].parent if paths()["data"].is_absolute() else Path.cwd()

REF = reference()
PROHIBITED_PRS: dict[str, set[int]] = {cid: REF.prohibited_prs(cid) for cid in GOLDEN_CASES}
NOTES: dict[str, list[str]] = {cid: REF.notes(cid) for cid in GOLDEN_CASES}


class LeakageError(AssertionError):
    pass


def _load(kind: str, key: str) -> dict[str, Any] | None:
    path = RAW / kind / f"{key}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _put(db: EvidenceDB, record: Record, extra: dict[str, Any]) -> None:
    db.put(record, AUTHORITATIVE_WRITERS[record.source], **extra)


def first_outcome_time(case_id: str) -> datetime | None:
    """Earliest moment any prohibited outcome record becomes visible in the case issues."""
    prohibited = PROHIBITED_PRS.get(case_id, set())
    if not prohibited:
        return None
    times: list[datetime] = []
    for n in GOLDEN_CASES[case_id].get("issues", []):
        raw = (_load("issues", str(n)) or {}).get("data") or {}
        for e in raw.get("timeline", []):
            for node in (e.get("source"), e.get("closer"), e.get("subject"), e.get("canonical")):
                if isinstance(node, dict) and node.get("number") in prohibited:
                    times.append(from_iso(e["createdAt"]))
        texts = [(c.get("createdAt"), c.get("body")) for c in raw.get("comments_list", [])]
        texts += [(e.get("editedAt"), e.get("diff")) for e in (raw.get("userContentEdits") or {}).get("nodes", [])]
        for when, text in texts:
            if when and _numbers_in(text or "") & prohibited:
                times.append(from_iso(when))
    for pr in prohibited:
        raw = (_load("pulls", str(pr)) or {}).get("data")
        if raw and raw.get("createdAt"):
            times.append(from_iso(raw["createdAt"]))
    return min(times) if times else None


def case_cutoff(case_id: str) -> datetime:
    """Report time plus the triage window, but always before the first outcome record."""
    case = GOLDEN_CASES[case_id]
    if "release" in case:
        tag = _load("tags", case["release"])
        return from_iso(tag["data"]["tagged_at"])
    created = [from_iso(_load("issues", str(n))["data"]["createdAt"]) for n in case["issues"]]
    cutoff = max(created) + TRIAGE_WINDOW
    outcome = first_outcome_time(case_id)
    if outcome is not None and outcome <= cutoff:
        cutoff = outcome - timedelta(seconds=1)
    return cutoff


def _search_numbers(key: str) -> list[int]:
    data = _load("search", key)
    return [n["number"] for n in (data or {}).get("data", [])]


def issue_numbers_for(case_id: str) -> set[int]:
    case = GOLDEN_CASES[case_id]
    nums = set(case.get("issues", [])) | set(case.get("context", []))
    nums |= set(_search_numbers(f"{case_id}-flakes"))
    return nums


def pr_numbers_for(case_id: str) -> set[int]:
    case = GOLDEN_CASES[case_id]
    return set(case.get("context_prs", [])) | set(_search_numbers(f"{case_id}-merged"))


def build_issue_case(case_id: str, people: People) -> tuple[EvidenceDB, dict[str, Any]]:
    cutoff = case_cutoff(case_id)
    BUILD.mkdir(parents=True, exist_ok=True)
    path = BUILD / f"case-{case_id.lower()}.sqlite"
    if path.exists():
        path.unlink()
    db = EvidenceDB(path)
    stats: dict[str, Any] = {"issues": 0, "pull_requests": 0, "commits": 0, "ci_jobs": 0, "synthetic": 0, "missing_fields": {}}
    referenced: set[int] = set()
    for n in sorted(issue_numbers_for(case_id)):
        raw = _load("issues", str(n))
        if raw is None or raw["data"] is None:
            continue
        asof = issue_as_of(raw["data"], cutoff)
        if asof is None:
            continue
        fetched = from_iso(raw["fetched_at"])
        record, extra = to_record("issues", asof, cutoff=cutoff, fetched_at=fetched, history_complete_through=fetched)
        _put(db, record, extra)
        stats["issues"] += 1
        if asof.missing:
            stats["missing_fields"][asof.source_id] = asof.missing
        for ref in asof.view.get("references", []):
            if ref.get("number"):
                referenced.add(int(ref["number"]))
    prs = pr_numbers_for(case_id) | referenced
    for n in sorted(prs):
        raw = _load("pulls", str(n))
        if raw is None or raw["data"] is None:
            continue
        asof = pull_request_as_of(raw["data"], cutoff)
        if asof is None:
            continue
        fetched = from_iso(raw["fetched_at"])
        record, extra = to_record("git", asof, cutoff=cutoff, fetched_at=fetched, history_complete_through=fetched)
        _put(db, record, extra)
        stats["pull_requests"] += 1
        if asof.view.get("merge_commit"):
            commit = AsOf(
                f"commit/{asof.view['merge_commit']}",
                "commit",
                {
                    "sha": asof.view["merge_commit"],
                    "summary": asof.view["title"],
                    "committed_at": asof.view["merged_at"],
                    "pull_request": asof.view["number"],
                    "files": asof.view["files"],
                },
                from_iso(asof.view["merged_at"]),
                True,
                [],
                commit_sha=asof.view["merge_commit"],
            )
            crec, cextra = to_record("git", commit, cutoff=cutoff, fetched_at=fetched, history_complete_through=fetched)
            _put(db, crec, cextra)
            stats["commits"] += 1
    # CI jobs linked from the case issues as they stood at the cutoff.
    for n in GOLDEN_CASES[case_id].get("issues", []):
        asof = issue_as_of(_load("issues", str(n))["data"], cutoff)
        texts = [asof.view.get("body") or ""] + [c.get("body") or "" for c in asof.view.get("comments", [])]
        for text in texts:
            for p in prow_paths(text)[:2]:
                stats["ci_jobs"] += _add_job(db, p, cutoff)
    for record, content, etype in all_synthetic(case_id, cutoff):
        _put(db, record, {"content": content, "entity_type": etype, "freshness_basis": "series_end" if record.source == "telemetry" else "per_run"})
        stats["synthetic"] += 1
    return db, {"cutoff": cutoff, "stats": stats}


def _add_job(db: EvidenceDB, prow_path: str, cutoff: datetime) -> int:
    import hashlib

    key = hashlib.sha256(f"{prow_path}/build-log.txt".encode()).hexdigest()[:24]
    log = _load("prow", key)
    fkey = hashlib.sha256(f"{prow_path}/finished.json".encode()).hexdigest()[:24]
    fin = _load("prow", fkey)
    if log is None:
        return 0
    finished = None
    if fin and fin["data"].get("status") == "ok":
        try:
            finished = json.loads(fin["data"]["text"])
        except ValueError:
            finished = None
    finished_at = datetime.fromtimestamp(finished["timestamp"], tz=timezone.utc) if finished and finished.get("timestamp") else None
    if finished_at is not None and finished_at > cutoff:
        return 0
    data = log["data"]
    text = data.get("text") if data.get("status") == "ok" else None
    view = job_view(prow_path, text, finished, total_lines=data.get("line_count"), truncated=data.get("truncated"))
    from atlas.connectors.ci import job_record

    valid = finished_at or cutoff
    record = job_record(view, valid_from=valid, cutoff=cutoff, fetched_at=from_iso(log["fetched_at"]))
    db.put(record, AUTHORITATIVE_WRITERS["ci"], content=view, entity_type="ci_job", freshness_basis="per_run")
    return 1


# ---------------------------------------------------------------------------
# Leakage checks
# ---------------------------------------------------------------------------


def _numbers_in(text: str) -> set[int]:
    return {int(m) for m in re.findall(r"(?:#|/pull/|/issues/)(\d{4,6})\b", text or "")}


def check_leakage(db: EvidenceDB, cutoff: datetime, prohibited_prs: set[int]) -> list[str]:
    problems: list[str] = []
    for rec in db.all_records():
        if rec.valid_from > cutoff:
            problems.append(f"{rec.record_id}: content version after the cutoff")
        content = db.content(rec.record_id) or {}
        if rec.source == "git" and rec.source_id.startswith("pr/") and int(rec.source_id.split("/")[1]) in prohibited_prs:
            problems.append(f"{rec.record_id}: prohibited fix pull request present")
        blob = json.dumps(content, sort_keys=True)
        hit = _numbers_in(blob) & prohibited_prs
        if hit:
            problems.append(f"{rec.record_id}: mentions prohibited pull request {sorted(hit)}")
        for ref in content.get("references", []) if isinstance(content, dict) else []:
            at = from_iso(ref.get("at")) if ref.get("at") else None
            if at and at > cutoff:
                problems.append(f"{rec.record_id}: relationship dated after the cutoff")
        if isinstance(content, dict):
            for c in content.get("comments", []):
                if from_iso(c["created_at"]) > cutoff:
                    problems.append(f"{rec.record_id}: comment after the cutoff")
    return problems


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export(db: EvidenceDB, snapshot_id: str, kind: str, manifest: dict[str, Any], people: People, *, root: Path = EXPORT) -> Path:
    target = root / "snapshots" / kind / snapshot_id
    target.mkdir(parents=True, exist_ok=True)
    rows = db.export_rows()
    lines = []
    for row in rows:
        row = dict(row)
        if row.get("content"):
            row["content"] = json.dumps(people.scrub(json.loads(row["content"])), sort_keys=True)
        lines.append(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    records_path = target / "records.jsonl"
    records_path.write_bytes("".join(lines).encode("utf-8"))
    conflicts = db.insufficient_evidence()
    (target / "conflicts.jsonl").write_bytes("".join(json.dumps(c, sort_keys=True) + "\n" for c in conflicts).encode("utf-8"))
    manifest = dict(manifest)
    manifest.update({
        "snapshot_id": snapshot_id,
        "records": len(rows),
        "records_sha256": normalized_sha256(records_path),
    })
    (target / "snapshot.json").write_bytes((json.dumps(manifest, indent=1, sort_keys=True, default=str) + "\n").encode("utf-8"))
    return target


def check_export_people(target: Path, people: People) -> list[str]:
    problems = []
    text = (target / "records.jsonl").read_text(encoding="utf-8")
    for h in people.handles:
        if len(h) >= 3 and re.search(r"(?<![\w-])@?" + re.escape(h) + r"(?![\w-])", text):
            problems.append(f"{target.name}: handle survived pseudonymisation")
            break
    if re.search(r"\b[A-Za-z0-9._%+-]+@(?!users\.noreply\.github\.com)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text):
        problems.append(f"{target.name}: email address survived pseudonymisation")
    return problems


def collect_people(db: EvidenceDB, people: People) -> None:
    def walk(v: Any, key: str | None = None) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str) and key in ("author", "editor", "login"):
            people.collect([v])

    for rec in db.all_records():
        walk(db.content(rec.record_id))


def build_golden(root: Path = EXPORT) -> dict[str, Any]:
    people = People()
    built: dict[str, tuple[EvidenceDB, dict[str, Any]]] = {}
    for case_id in GOLDEN_CASES:
        if "release" in GOLDEN_CASES[case_id]:
            continue
        db, info = build_issue_case(case_id, people)
        problems = check_leakage(db, info["cutoff"], PROHIBITED_PRS.get(case_id, set()))
        if problems:
            db.close()
            raise LeakageError(f"{case_id}: " + "; ".join(problems[:10]))
        collect_people(db, people)
        built[case_id] = (db, info)
    report: dict[str, Any] = {}
    for case_id, (db, info) in built.items():
        manifest = {
            "release_or_case": case_id,
            "cutoff": info["cutoff"].isoformat(),
            "cutoff_rule": (f"latest case issue creation time plus a {int(TRIAGE_WINDOW.total_seconds() // 60)} minute triage "
                            "window, ending one second before the first outcome record if that comes sooner"),
            "alias_variant": None,
            "counts": info["stats"],
            "notes": NOTES.get(case_id, []),
            "data_modes": DATA_MODES,
        }
        target = export(db, f"case-{case_id.lower()}", "cases", manifest, people, root=root)
        db.close()
        report[case_id] = {"cutoff": info["cutoff"].isoformat(), **info["stats"], "export": target.as_posix()}
    people.write_handles_file()
    problems = []
    for case_id in report:
        problems += check_export_people(root / "snapshots" / "cases" / f"case-{case_id.lower()}", people)
    if problems:
        raise LeakageError("; ".join(problems))
    return report


DATA_MODES = {
    "issues": "REAL", "git": "REAL", "ci": "REAL",
    "kubernetes": "SYNTHETIC", "telemetry": "SYNTHETIC", "capacity": "SYNTHETIC",
    "security": "not captured for historical cases; investigation security checks return not_run",
}


def main(argv: list[str]) -> int:
    what = argv[1] if len(argv) > 1 else "golden"
    if what == "golden":
        for case_id, info in build_golden().items():
            print(case_id, info)
        return 0
    print("usage: python -m prep.snapshot.build golden")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
