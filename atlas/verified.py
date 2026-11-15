"""Builds the verified artefact: the only form of a run's output that may be rendered, published or
executed.

Every claim carries a verdict. The transitive closure of rejected, contradicted and missing claims
is computed over ``depends_on``, and every output field bound to any claim in that closure (through
``claim_ids``) is removed: root cause, options, findings, reasoning, forecast and allocation entries.
The classification follows a minimum-support rule: it stays while at least one of the claims it rests
on is still supported, and is removed when none is. The action follows the classification, except
that an action that would start code work needs every one of its claims and the root cause. A
dangling claim reference fails the run.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

BAD_VERDICTS = frozenset({"rejected", "contradicted", "missing"})
LIST_FIELDS = ("items", "options", "findings", "reasoning", "insufficient_evidence")
VALUE_FIELDS = ("classification", "action")
MIN_SUPPORT = 1
EXECUTABLE_ACTIONS = frozenset({"DELEGATE_TO_CODING_AGENT"})
FALLBACK_ACTION = "ASSIGN_DEVELOPER"


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
    by_id = {c["id"]: c for c in claims}
    removed_fields: list[str] = []

    def bound_to_removed(obj: Mapping[str, Any]) -> bool:
        return any(cid in removed for cid in obj.get("claim_ids", []))

    # The root cause is a statement: it goes when any claim it rests on goes.
    rc = out.get("root_cause")
    root_cause_removed = isinstance(rc, Mapping) and bound_to_removed(rc)
    if root_cause_removed:
        out["root_cause"] = None
        removed_fields.append("root_cause")

    def surviving(field: Mapping[str, Any]) -> dict[str, Any] | None:
        """The field resting only on its still-supported claims, or None when too few remain."""
        kept_ids = [cid for cid in field.get("claim_ids", []) if cid not in removed]
        if len(kept_ids) < MIN_SUPPORT:
            return None
        scopes: set[str] = set()
        for cid in kept_ids:
            scopes |= set(by_id[cid].get("required_scopes", ["internal"]))
        return {**field, "claim_ids": kept_ids, "required_scopes": sorted(scopes or {"public"})}

    def empty(field: Mapping[str, Any]) -> dict[str, Any]:
        return {"value": None, "claim_ids": [], "required_scopes": list(field.get("required_scopes", []))}

    # Minimum support: the classification stays while at least MIN_SUPPORT of the claims it rests on
    # are still supported. Claims that depend on a removed claim are already in the removal closure.
    cls = out.get("classification")
    classification_removed = False
    if isinstance(cls, Mapping) and bound_to_removed(cls):
        kept = surviving(cls)
        if kept is None:
            out["classification"] = empty(cls)
            removed_fields.append("classification")
            classification_removed = True
        else:
            out["classification"] = kept
    # The action follows the classification. An action that would start code work is stricter: it
    # needs every claim it rests on and the root cause, so a rejected cause can never leave a coding
    # task behind.
    act = out.get("action")
    if isinstance(act, Mapping):
        touched = bound_to_removed(act)
        executable = act.get("value") in EXECUTABLE_ACTIONS
        if classification_removed:
            out["action"] = empty(act)
            removed_fields.append("action")
        elif executable and (touched or root_cause_removed):
            # Code work needs full support. With partial support the item still needs an owner, so it
            # goes to a developer instead of being left without an action.
            kept = surviving(act) if touched else dict(act)
            out["action"] = empty(act) if kept is None else {**kept, "value": FALLBACK_ACTION}
            removed_fields.append("action")
        elif touched:
            kept = surviving(act)
            if kept is None:
                out["action"] = empty(act)
                removed_fields.append("action")
            else:
                out["action"] = kept
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
