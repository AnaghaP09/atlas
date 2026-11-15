"""Show exactly what a prepared task would publish, without contacting the target repository.

    python -m atlas.publish_check [--task TASK_ID] [--env-file .env]

Prints the destination, the branch, the commit message, the changed files, and the draft pull
request title and description as frozen at preparation (after scope filtering). It makes no
network request and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from atlas.config import load_settings
from atlas.contracts import TaskState
from atlas.execution.db import OpsDB
from atlas.tasks import TaskStore

READY = (TaskState.PREPARED, TaskState.APPROVED_B)


def describe(task) -> dict:
    frozen = dict(task.artefact or {})
    files = frozen.get("changed_files") or {}
    return {
        "task_id": task.task_id,
        "state": task.state.value,
        "destination_repository": task.destination_repo,
        "base_branch": task.base_ref,
        "branch_to_create": task.publication_ref,
        "commit_message": frozen.get("commit_message"),
        "files": [{"path": path, "change": "delete" if content is None else "write",
                   "bytes": 0 if content is None else len(content.encode("utf-8"))}
                  for path, content in sorted(files.items())],
        "pull_request": {"draft": True, "title": frozen.get("title"), "description": frozen.get("body")},
        "change_sha256": frozen.get("diff_sha256"),
        "tree_sha256": frozen.get("tree_sha256"),
        "withheld_items": frozen.get("withheld"),
        "diff": frozen.get("diff"),
        "network_requests_made": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas.publish_check")
    parser.add_argument("--task")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--environment", default="normal")
    args = parser.parse_args(argv)
    settings = load_settings(env_file=args.env_file)
    path = Path(settings.data_dir) / "local" / f"{args.environment}-ops.sqlite"
    if not path.exists():
        print(f"No task store found at {path}. Start ATLAS and prepare a task first.")
        return 1
    tasks = TaskStore(OpsDB(str(path)))
    if args.task:
        chosen = [tasks.get(args.task)]
    else:
        chosen = [t for t in tasks.all() if t.state in READY and t.artefact]
    if not chosen:
        print("No prepared task is waiting to be published.")
        return 1
    for task in chosen:
        if not task.artefact:
            print(f"Task {task.task_id} is {task.state.value} and has no prepared change yet.")
            continue
        json.dump(describe(task), sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
