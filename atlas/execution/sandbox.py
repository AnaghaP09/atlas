"""Sandboxes for running checks on a prepared change.

``ContainerSandbox`` is the only path that runs repository code: a fresh container per run from a
pinned image (never pulled at run time), no network, read-only root filesystem, a tmpfs work
directory holding a copy of the tree, a read-only mount of that copy only, no secrets, a non-root
user, and CPU, memory, process and time limits. ``NonExecutingSandbox`` is used when no container
engine is available: it runs only trusted checks that never execute repository code and reports
every executable check as ``unavailable``, so such a task can never be approved for publication.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from atlas.execution.images import ImageCache, default_runner

EXECUTABLE_CHECKS = ("go_build", "go_vet", "behavioural_tests")
GO_ENV = {
    "HOME": "/work", "GOCACHE": "/work/cache", "GOPATH": "/work/gopath", "GOFLAGS": "-mod=mod",
    "GOPROXY": "off", "GOTOOLCHAIN": "local", "CGO_ENABLED": "0", "GOTELEMETRY": "off",
}
CHECK_SCRIPT = r"""
set -u
cp -r /src /work/repo || { echo "CHECK copy failed"; exit 90; }
cd /work/repo
if [ -d .atlas/behaviour ] && [ -n "${BEHAVIOUR_PACKAGE:-}" ]; then cp .atlas/behaviour/*_test.go "$BEHAVIOUR_PACKAGE"/ 2>/dev/null; fi
if [ -z "$(gofmt -l .)" ]; then echo "CHECK gofmt passed"; else echo "CHECK gofmt failed"; fi
if go build ./... ; then echo "CHECK go_build passed"; else echo "CHECK go_build failed"; fi
if go vet ./... ; then echo "CHECK go_vet passed"; else echo "CHECK go_vet failed"; fi
if go test -count=1 ./... ; then echo "CHECK behavioural_tests passed"; else echo "CHECK behavioural_tests failed"; fi
"""


@dataclass
class SandboxResult:
    checks: dict[str, str]
    log: str = ""
    container: bool = False
    isolation: dict[str, str] = field(default_factory=dict)


def write_tree(tree: Mapping[str, str], root: Path) -> None:
    for rel, content in tree.items():
        target = (root / rel).resolve()
        if root.resolve() not in target.parents:
            raise ValueError(f"path escapes the work directory: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


class ContainerSandbox:
    """Runs build, vet and tests in an isolated, short-lived container."""

    container = True

    def __init__(self, images: ImageCache | None = None, runner: Callable = default_runner, *,
                 timeout: float = 300, memory: str = "1g", cpus: str = "2", pids: int = 256,
                 behaviour_package: str = "netaddr") -> None:
        self.images = images or ImageCache(runner=runner)
        self.run = runner
        self.timeout = timeout
        self.memory = memory
        self.cpus = cpus
        self.pids = pids
        self.behaviour_package = behaviour_package

    def isolation_flags(self, name: str, src: Path) -> list[str]:
        flags = [
            "run", "--rm", "--pull=never", "--name", name,
            "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--pids-limit", str(self.pids),
            "--memory", self.memory, "--cpus", self.cpus, "--user", "65534:65534",
            "--tmpfs", "/work:rw,exec,size=512m",
            "-v", f"{src}:/src:ro",
        ]
        for k, v in GO_ENV.items():
            flags += ["-e", f"{k}={v}"]
        flags += ["-e", f"BEHAVIOUR_PACKAGE={self.behaviour_package}"]
        return flags

    def run_checks(self, tree: Mapping[str, str]) -> SandboxResult:
        image = self.images.ensure("go")
        name = f"atlas-sbx-{uuid.uuid4().hex[:10]}"
        with tempfile.TemporaryDirectory(prefix="atlas-sbx-") as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            write_tree(tree, src)
            args = self.isolation_flags(name, src) + [image, "sh", "-c", CHECK_SCRIPT]
            try:
                result = self.run(args, timeout=self.timeout) if _accepts_timeout(self.run) else self.run(args)
            except subprocess.TimeoutExpired:
                self.run(["kill", name])
                return SandboxResult({c: "failed" for c in ("gofmt",) + EXECUTABLE_CHECKS},
                                     "timed out", True, {"container_sandbox": "passed"})
        checks = {c: "failed" for c in ("gofmt",) + EXECUTABLE_CHECKS}
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] == "CHECK" and parts[1] in checks:
                checks[parts[1]] = "passed" if parts[2] == "passed" else "failed"
        checks["container_sandbox"] = "passed"
        return SandboxResult(checks, (result.stdout or "") + (result.stderr or ""), True)

    def probe_isolation(self) -> dict[str, str]:
        """Try to escape: reach the network, write the root filesystem, write the source mount,
        read secrets from the environment. Every probe must report ``blocked``."""
        image = self.images.ensure("go")
        script = r"""
(wget -q -T 3 -O- http://example.com >/dev/null 2>&1 && echo "PROBE network open") || echo "PROBE network blocked"
(touch /escape 2>/dev/null && echo "PROBE rootfs open") || echo "PROBE rootfs blocked"
(touch /src/escape 2>/dev/null && echo "PROBE source_mount open") || echo "PROBE source_mount blocked"
(env | grep -qiE 'token|secret|api_key|password' && echo "PROBE env_secrets open") || echo "PROBE env_secrets blocked"
([ "$(id -u)" = "0" ] && echo "PROBE root_user open") || echo "PROBE root_user blocked"
(ls /run/desktop/mnt/host >/dev/null 2>&1 && echo "PROBE host_paths open") || echo "PROBE host_paths blocked"
"""
        name = f"atlas-probe-{uuid.uuid4().hex[:10]}"
        with tempfile.TemporaryDirectory(prefix="atlas-probe-") as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "README").write_text("probe\n", encoding="utf-8")
            result = self.run(self.isolation_flags(name, src) + [image, "sh", "-c", script])
        out: dict[str, str] = {}
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[0] == "PROBE":
                out[parts[1]] = parts[2]
        return out


def _accepts_timeout(fn: Callable) -> bool:
    try:
        import inspect

        return "timeout" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def gofmt_binary() -> str | None:
    found = shutil.which("gofmt")
    if found:
        return found
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "go" / "bin" / "gofmt.exe"
    return str(candidate) if candidate.exists() else None


class NonExecutingSandbox:
    """Used without a container engine: trusted checks only, no repository code is executed."""

    container = False

    def __init__(self, gofmt: str | None = None, runner: Callable[[Sequence[str]], subprocess.CompletedProcess] | None = None) -> None:
        self.gofmt = gofmt if gofmt is not None else gofmt_binary()
        self.runner = runner or (lambda args: subprocess.run(list(args), capture_output=True, text=True, timeout=60))
        self.commands: list[list[str]] = []

    def run_checks(self, tree: Mapping[str, str]) -> SandboxResult:
        checks = {c: "unavailable" for c in EXECUTABLE_CHECKS}
        checks["container_sandbox"] = "unavailable"
        if self.gofmt is None:
            checks["gofmt"] = "unavailable"
            return SandboxResult(checks, "gofmt not installed")
        with tempfile.TemporaryDirectory(prefix="atlas-nx-") as tmp:
            root = Path(tmp)
            write_tree(tree, root)
            cmd = [self.gofmt, "-l", str(root)]
            self.commands.append(cmd)
            result = self.runner(cmd)
        checks["gofmt"] = "passed" if result.returncode == 0 and not result.stdout.strip() else "failed"
        return SandboxResult(checks, result.stdout + result.stderr)
