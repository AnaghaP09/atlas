"""The coding agent: asks a model for a unified diff for one bounded task.

The model call never runs inside the sandbox; it goes through the outbound boundary (which redacts,
checks the mode and containment, and counts attempts). The agent sees only the task, its permitted
scope and the files inside that scope.
"""

from __future__ import annotations

import fnmatch
from typing import Mapping, Sequence

from atlas.contracts import EgressPurpose, Outbound

INSTRUCTIONS = (
    "You are fixing one bounded task in a small repository. Change only files matching the "
    "permitted scope. Return a single unified diff (with ---/+++ headers and @@ hunks) and nothing "
    "else. Do not change tests, CI configuration or dependency files."
)


class AgentError(RuntimeError):
    pass


class CodingAgent:
    def __init__(self, outbound: Outbound, role: str = "coding_agent") -> None:
        self.outbound = outbound
        self.role = role

    def propose(self, task_text: str, scope: Sequence[str], tree: Mapping[str, str]) -> str:
        files = {p: c for p, c in sorted(tree.items()) if any(fnmatch.fnmatch(p, s) for s in scope)}
        if not files:
            raise AgentError("no files inside the permitted scope")
        payload = {"instructions": INSTRUCTIONS, "task": task_text, "permitted_scope": list(scope), "files": files}
        response = self.outbound.call(self.role, EgressPurpose.MODEL_CALL, payload)
        if response.stop_reason == "refusal":
            raise AgentError("the model declined the task")
        content = response.content
        diff = content.get("diff") if isinstance(content, Mapping) else content
        if not isinstance(diff, str) or "@@" not in diff:
            raise AgentError("the model did not return a unified diff")
        return diff
