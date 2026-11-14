"""The frozen public publication artefact.

After preparation, everything that will be published (each file's hunks, the commit message, the
pull-request title and body) passes through the authorisation projection for the anonymous public
principal and the secret and personal-data scans. Withheld content is removed per object and
recorded. The artefact's diff hash and resulting-tree hash are frozen; checks run on exactly this
artefact, Approval B binds both hashes, and the outgoing commit is compared with them before push.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from atlas.contracts import PUBLIC_PRINCIPAL, Principal, Projection
from atlas.execution import diffs
from atlas.execution.checks import personal_data_scan, secret_scan

PUBLIC = Principal(role=None, scopes=frozenset({"public"}))


class ArtefactError(ValueError):
    pass


@dataclass(frozen=True)
class SourceChange:
    """One prepared file change, its hunks each carrying the scopes of what produced them."""

    path: str
    old_path: str | None
    hunks: Sequence[Mapping[str, Any]]  # {"text": "@@ ...\n...", "required_scopes": [...]}
    required_scopes: Sequence[str] = ("public",)


def source_from_diff(diff_text: str, scopes: Sequence[str] = ("public",)) -> list[SourceChange]:
    out = []
    for fd in diffs.parse(diff_text):
        out.append(SourceChange(fd.path, fd.old_path, [{"text": h.text(), "required_scopes": list(scopes)} for h in fd.hunks],
                                list(scopes)))
    return out


def public_principal(project_id: str | None) -> Principal:
    """The public reader of one project's publication. Items tagged with that project stay; items of
    any other project are withheld, as are restricted items."""
    if not project_id:
        return PUBLIC
    return Principal(role=None, scopes=PUBLIC.scopes, project_id=project_id)


def _project(projection: Projection, changes: Sequence[SourceChange], message: str, title: str, body: str,
             scopes: Mapping[str, Sequence[str]], project_id: str | None = None) -> dict[str, Any]:
    raw = {
        "files": [
            {"path": c.path, "old_path": c.old_path, "required_scopes": list(c.required_scopes),
             "hunks": [dict(h) for h in c.hunks]}
            for c in changes
        ],
        "commit_message": {"text": message, "required_scopes": list(scopes.get("commit_message", ["public"]))},
        "title": {"text": title, "required_scopes": list(scopes.get("title", ["public"]))},
        "body": {"text": body, "required_scopes": list(scopes.get("body", ["public"]))},
    }
    return projection.project(raw, public_principal(project_id))


def build(projection: Projection, base_tree: Mapping[str, str], changes: Sequence[SourceChange], *,
          commit_message: str, title: str, body: str,
          field_scopes: Mapping[str, Sequence[str]] | None = None, project_id: str | None = None) -> dict[str, Any]:
    """Project, scan, apply and hash. Raises when a required field becomes empty.

    ``project_id`` is the task's project: the public principal is bound to it."""
    scopes = field_scopes or {}
    projected = _project(projection, changes, commit_message, title, body, scopes, project_id)
    withheld: list[dict[str, Any]] = []
    kept_files: list[diffs.FileDiff] = []
    by_path = {c.path: c for c in changes}
    projected_paths = {f["path"]: f for f in projected.get("files", []) if f}
    for change in changes:
        pf = projected_paths.get(change.path)
        if pf is None:
            withheld.append({"path": change.path, "withheld": "whole file"})
            continue
        kept_hunks = pf.get("hunks", [])
        if len(kept_hunks) < len(change.hunks):
            withheld.append({"path": change.path, "withheld": f"{len(change.hunks) - len(kept_hunks)} hunk(s)"})
        if not kept_hunks:
            continue
        text = f"--- {'a/' + change.old_path if change.old_path else '/dev/null'}\n+++ b/{change.path}\n" + "".join(
            h["text"] for h in kept_hunks)
        parsed = diffs.parse(text)
        kept_files.extend(parsed)
    for name in ("commit_message", "title", "body"):
        value = projected.get(name)
        if not value or not str(value.get("text", "")).strip():
            raise ArtefactError(f"required field {name} is empty after projection; publication fails closed")
    if not kept_files:
        raise ArtefactError("every change was withheld; nothing can be published")
    diff_text = diffs.render(kept_files)
    try:
        tree = diffs.apply(base_tree, kept_files)
    except diffs.DiffError as exc:
        raise ArtefactError(f"projected diff does not apply: {exc}") from exc
    scan_text = diff_text + projected["commit_message"]["text"] + projected["title"]["text"] + projected["body"]["text"]
    scans = {"secret_scan": secret_scan(scan_text).status, "personal_data_scan": personal_data_scan(scan_text).status}
    changed = {fd.path: tree.get(fd.path) for fd in kept_files}
    return {
        "principal": PUBLIC_PRINCIPAL,
        "diff": diff_text,
        "diff_sha256": diffs.sha256_text(diff_text),
        "tree_sha256": diffs.tree_sha256(tree),
        "changed_files": changed,
        "commit_message": projected["commit_message"]["text"],
        "title": projected["title"]["text"],
        "body": projected["body"]["text"],
        "withheld": withheld,
        "scans": scans,
        "source_paths": sorted(by_path),
    }


def verify_outgoing(projection: Projection, frozen: Mapping[str, Any], base_tree: Mapping[str, str],
                    changes: Sequence[SourceChange], *, commit_message: str, title: str, body: str,
                    field_scopes: Mapping[str, Sequence[str]] | None = None, project_id: str | None = None) -> list[str]:
    """Rebuild the outgoing commit and re-run the projection; any difference is a mismatch."""
    problems: list[str] = []
    try:
        rebuilt = build(projection, base_tree, changes, commit_message=commit_message, title=title, body=body,
                        field_scopes=field_scopes, project_id=project_id)
    except ArtefactError as exc:
        return [f"artefact can no longer be built: {exc}"]
    if rebuilt["diff_sha256"] != frozen.get("diff_sha256"):
        problems.append("projected diff differs from the approved artefact")
    if rebuilt["tree_sha256"] != frozen.get("tree_sha256"):
        problems.append("outgoing tree differs from the approved artefact")
    for key in ("commit_message", "title", "body"):
        if rebuilt[key] != frozen.get(key):
            problems.append(f"{key} differs from the approved artefact")
    return problems
