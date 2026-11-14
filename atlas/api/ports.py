"""Interfaces the HTTP API needs from the data and execution layers.

The API depends only on these protocols. The running server wires the real implementations; until
a component is connected, an ``Unwired`` adapter answers every call with a clear 503 reason instead
of guessing. Every object returned by a port carries its ``project_id`` so the API can check the
caller's project before any effect.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence


class NotWired(RuntimeError):
    """A component the endpoint needs is not connected in this build."""


class Refused(RuntimeError):
    """A port refused the operation for a stated reason (mapped to 403 or 409 by the API)."""

    def __init__(self, reason: str, status: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class SnapshotPort(Protocol):
    def ingest(self, sources: Sequence[str], project_id: str) -> Mapping[str, Any]: ...

    def freeze(self, project_id: str) -> Mapping[str, Any]: ...

    def list(self, release: str | None = None, project_id: str | None = None) -> list[Mapping[str, Any]]: ...

    def get(self, snapshot_id: str) -> Mapping[str, Any] | None:
        """``{snapshot_id, project_id, release, kind, cutoff, alias_variant, counts}`` or None."""

    def graph(self, snapshot_id: str) -> Mapping[str, Any]: ...

    def evidence(self, snapshot_id: str, record_id: str, content_version_id: str | None,
                 lineage: Sequence[str] = ()) -> Mapping[str, Any] | None:
        """The record version as the snapshot holds it, or None if it is not in the snapshot."""


class TaskPort(Protocol):
    def create(self, run_id: str, generation: int, action: Mapping[str, Any], principal_role: str,
               idempotency_key: str) -> Mapping[str, Any]: ...

    def list(self, project_id: str | None = None) -> list[Mapping[str, Any]]: ...

    def get(self, task_id: str) -> Mapping[str, Any] | None:
        """Operational task view; post-publication fields are explicit None before publication."""

    def approvals(self, project_id: str | None = None) -> list[Mapping[str, Any]]: ...

    def approval(self, approval_id: str) -> Mapping[str, Any] | None: ...

    def decide(self, approval_id: str, decision: str, review_minutes: float, principal_role: str,
               idempotency_key: str) -> Mapping[str, Any]: ...

    def prepare(self, task_id: str, principal_role: str, idempotency_key: str) -> Mapping[str, Any]: ...

    def publish(self, task_id: str, principal_role: str, idempotency_key: str) -> Mapping[str, Any]: ...

    def adjudicate(self, task_id: str, verdict: str, note: str, principal_role: str,
                   idempotency_key: str) -> Mapping[str, Any]: ...

    def security_refresh(self, task_id: str, principal_role: str, idempotency_key: str) -> Mapping[str, Any]: ...

    def scorecard(self, project_id: str | None = None) -> Mapping[str, Any]: ...

    def tasks_for_run(self, run_id: str) -> list[Mapping[str, Any]]: ...

    def apply_reverification(self, run_id: str, new_generation: int, assessments: Mapping[str, str]) -> None: ...


class ContainmentPort(Protocol):
    def permits(self, consumer: str) -> Any: ...

    def state(self) -> Mapping[str, Any]: ...

    def switch(self, switch_id: str) -> Mapping[str, Any] | None: ...

    def record_prerequisite(self, incident_id: str, kind: str, reference: str, note: str,
                            principal_role: str) -> Mapping[str, Any]: ...

    def resume(self, incident_id: str, prerequisite_ids: Sequence[str], principal_role: str) -> Mapping[str, Any]: ...

    def authorise_correction(self, incident_id: str, body: Mapping[str, Any], principal_role: str) -> Mapping[str, Any]: ...

    def corrections(self, incident_id: str) -> list[Mapping[str, Any]]: ...

    def correction(self, correction_id: str) -> Mapping[str, Any] | None: ...

    def apply_correction(self, body: Mapping[str, Any], principal_role: str, idempotency_key: str) -> Mapping[str, Any]: ...


class Unwired:
    """Stand-in for a component that is not connected yet; every call raises NotWired."""

    def __init__(self, component: str) -> None:
        self._component = component

    def permits(self, consumer: str) -> Any:  # containment must answer even when unwired
        from atlas.contracts import EligibilityResult

        return EligibilityResult(True)

    def __getattr__(self, name: str) -> Any:
        component = self._component

        def _missing(*args: Any, **kwargs: Any) -> Any:
            raise NotWired(f"{component} is not connected in this build yet")

        return _missing
