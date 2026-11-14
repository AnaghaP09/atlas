"""Plain-code router: maps a trigger to a run type. Manual and automatic triggers share it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from atlas.guardrail import PolicyDecision, people_policy

NIGHTLY_LIMIT = 10
BACKLOG_ALERT = 10


@dataclass(frozen=True)
class Route:
    run_type: str  # forecast | investigation | nightly | question | refused
    items: tuple[Any, ...] = ()
    carried_forward: tuple[Any, ...] = ()
    backlog: int = 0
    policy: PolicyDecision | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


def route(trigger: Mapping[str, Any], queued_failures: Sequence[Any] = ()) -> Route:
    kind = trigger.get("trigger")
    if kind == "release_close":
        return Route("forecast", items=(trigger.get("release_id"),))
    if kind == "failure":
        return Route("investigation", items=(trigger.get("event_id"),))
    if kind == "nightly":
        selected = tuple(queued_failures[:NIGHTLY_LIMIT])
        rest = tuple(queued_failures[NIGHTLY_LIMIT:])
        return Route("nightly", items=selected, carried_forward=rest, backlog=len(rest))
    if kind == "question":
        decision = people_policy(str(trigger.get("question", "")))
        if not decision.allowed:
            return Route("refused", policy=decision)
        return Route("question", items=(trigger.get("question"),), policy=decision)
    return Route("refused", notes=(f"unknown trigger {kind!r}",))
