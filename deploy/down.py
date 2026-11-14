"""Stop the ATLAS customer view started by ``python -m deploy.up``.

    python -m deploy.down
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from deploy.envfile import read_env


def stop(run_dir: Path, *, out=sys.stdout) -> int:
    stopped = 0
    for pid_file in sorted(run_dir.glob("*.pid")):
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            pid_file.unlink(missing_ok=True)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
            print(f"  stopped {pid_file.stem} (process {pid})", file=out)
        except OSError:
            print(f"  {pid_file.stem} was not running", file=out)
        pid_file.unlink(missing_ok=True)
    if stopped == 0:
        print("  nothing to stop", file=out)
    return 0


def run(argv: list[str] | None = None, *, out=sys.stdout, root: Path | None = None) -> int:
    root = root or Path.cwd()
    env = read_env(root / ".env")
    data_dir = Path(env.get("ATLAS_DATA_DIR") or "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    return stop(data_dir / "local" / "run", out=out)


if __name__ == "__main__":
    raise SystemExit(run())
