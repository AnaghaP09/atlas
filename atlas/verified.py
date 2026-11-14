"""Builds the verified artefact: the only form of a run's output that may be rendered, published or
executed.

Every claim carries a verdict. The transitive closure of rejected, contradicted and missing claims
is computed over ``depends_on``, and every output field bound to any claim in that closure (through
``claim_ids``) is removed: root cause, classification, action, options, findings, reasoning,
forecast and allocation entries. A dangling claim reference fails the run.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

BAD_VERDICTS = frozenset({"rejected", "contradicted", "missing"})
LIST_FIELDS = ("items", "options", "findings", "reasoning", "insufficient_evidence")
VALUE_FIELDS = ("classification", "action")


class DanglingReference(ValueError):
    """A claim or field refers to a claim id that does not exist."""


@dataclass(frozen=True)
class PruneResult:
    artefact: dict[str, Any]
    removed_claims: frozenset[str]
    removed_fields: tuple[str, ...]


def _claim_refs(obj: Any) -> Iterable[tuple[str, list[str]]]:
    """Yield (path, claim_ids) for every claim-bound object in the artefact."""
    if isinstance(obj, Mapping):
        if "claim_ids" in obj:
            yield "", list(obj["claim_ids"])
        for k, v in obj.items():
            if k == "claims":
                continue
            for path, ids in _claim_refs(v):
                yield f"{k}.{path}" if path else k, ids
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            for path, ids in _claim_refs(v):
                yield f"{i}.{path}" if path else str(i), ids


def removal_closure(claims: list[Mapping[str, Any]]) -> frozenset[str]:
    ids = {c["id"] for c in claims}
    for c in claims:
        for dep in c.get("depends_on", []):
            if dep not in ids:
                raise DanglingReference(f"claim {c['id']} depends on unknown claim {dep}")
    bad = {c["id"] for c in claims if c.get("verdict") in BAD_VERDICTS}
    changed = True
    while changed:
        changed = False
        for c in claims:
            if c["id"] not in bad and any(d in bad for d in c.get("depends_on", [])):
                bad.add(c["id"])
                changed = True
    return frozenset(bad)


def build_verified(artefact: Mapping[str, Any]) -> PruneResult:
    """Return a pruned copy. The input is never modified."""
    out = copy.deepcopy(dict(artefact))
    claims = out.get("claims", [])
    known = {c["id"] for c in claims}
    for path, ids in _claim_refs(out):
        for cid in ids:
            if cid not in known:
                raise DanglingReference(f"{path or 'field'} refers to unknown claim {cid}")
    removed = removal_closure(claims)
    removed_fields: list[str] = []

    def bound_to_removed(obj: Mapping[str, Any]) -> bool:
        return any(cid in removed for cid in obj.get("claim_ids", []))

    for name in VALUE_FIELDS:
        field = out.get(name)
        if isinstance(field, Mapping) and bound_to_removed(field):
            out[name] = {"value": None, "claim_ids": [], "required_scopes": list(field.get("required_scopes", []))}
            removed_fields.append(name)
    rc = out.get("root_cause")
    if isinstance(rc, Mapping) and bound_to_removed(rc):
        out["root_cause"] = None
        removed_fields.append("root_cause")
    for name in LIST_FIELDS:
        entries = out.get(name)
        if isinstance(entries, list):
            kept = [e for e in entries if not (isinstance(e, Mapping) and bound_to_removed(e))]
            if len(kept) != len(entries):
                removed_fields.append(name)
            out[name] = kept
    forecast = out.get("forecast")
    if isinstance(forecast, Mapping) and isinstance(forecast.get("items"), list):
        kept = [e for e in forecast["items"] if not bound_to_removed(e)]
        if len(kept) != len(forecast["items"]):
            removed_fields.append("forecast")
        out["forecast"] = {**forecast, "items": kept}
    sec = out.get("security_check")
    if isinstance(sec, Mapping) and isinstance(sec.get("findings"), list):
        out["security_check"] = {**sec, "findings": [f for f in sec["findings"] if not (isinstance(f, Mapping) and bound_to_removed(f))]}
    if out.get("evidence_recommendation") and (out.get("action") or {}).get("value") is None:
        removed_fields.append("evidence_recommendation")
        out["evidence_recommendation"] = None
    out["claims"] = [c for c in claims if c["id"] not in removed]
    return PruneResult(out, removed, tuple(removed_fields))
