"""Sandbox runners: the swap point between the demo and a pilot deployment.

Every runner enforces the same isolation contract (no network, read-only root filesystem, no
secrets, non-root user, all capabilities dropped, no privilege escalation, process, memory and CPU
limits) and asserts it itself before running anything: callers never pass isolation settings.
``local_docker`` runs a container on this machine (the demo). ``remote`` hands the same job to a
dedicated runner (for example a cluster job); it is not available in the demo and reports its
checks as unavailable. ``none`` runs no repository code at all.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from atlas.contracts import IsolationSpec, SandboxBundle, SandboxLimits, SandboxRunner
from atlas.execution.sandbox import (
    EXECUTABLE_CHECKS,
    ContainerSandbox,
    NonExecutingSandbox,
    SandboxResult,
)

RUNNER_SETTING = "ATLAS_SANDBOX_RUNNER"
RUNNER_NAMES = ("local_docker", "remote", "none")
MAX_PIDS = 256
MAX_MEMORY_BYTES = 1 << 30


class IsolationViolation(RuntimeError):
    """A runner was about to run code without the full isolation contract."""


def _memory_bytes(text: str) -> int:
    units = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}
    text = text.strip().lower()
    return int(float(text[:-1]) * units[text[-1]]) if text[-1:] in units else int(text)


def assert_contract(spec: IsolationSpec) -> None:
    problems = []
    if spec.network != "none":
        problems.append("network must be none")
    if not spec.read_only_root:
        problems.append("root filesystem must be read-only")
    if not spec.non_root:
        problems.append("must run as a non-root user")
    if not spec.cap_drop_all:
        problems.append("all capabilities must be dropped")
    if not spec.no_new_privileges:
        problems.append("privilege escalation must be disabled")
    if not 0 < spec.pids_limit <= MAX_PIDS:
        problems.append("process limit missing or too high")
    if not 0 < spec.memory_bytes <= MAX_MEMORY_BYTES:
        problems.append("memory limit missing or too high")
    if spec.secrets:
        problems.append("no secrets may be passed to the sandbox")
    if any(not m.endswith(":/src:ro") for m in spec.host_mounts) or len(spec.host_mounts) > 1:
        problems.append("only the read-only copy of the tree may be mounted")
    if problems:
        raise IsolationViolation("; ".join(problems))


def spec_from_docker_flags(flags: Sequence[str]) -> IsolationSpec:
    def value(flag: str) -> str | None:
        return flags[flags.index(flag) + 1] if flag in flags else None

    user = value("--user") or ""
    envs = [flags[i + 1] for i, f in enumerate(flags) if f == "-e"]
    secret_env = tuple(e.split("=", 1)[0] for e in envs
                       if any(w in e.split("=", 1)[0].upper() for w in ("TOKEN", "SECRET", "KEY", "PASSWORD")))
    return IsolationSpec(
        network=value("--network") or "default",
        read_only_root="--read-only" in flags,
        non_root=bool(user) and not user.startswith("0"),
        cap_drop_all=value("--cap-drop") == "ALL",
        no_new_privileges=value("--security-opt") == "no-new-privileges",
        pids_limit=int(value("--pids-limit") or 0),
        memory_bytes=_memory_bytes(value("--memory") or "0"),
        secrets=secret_env,
        host_mounts=tuple(flags[i + 1] for i, f in enumerate(flags) if f == "-v"),
    )


def _check_bundle(task: Any, bundle: SandboxBundle) -> None:
    for path in bundle.tree:
        if path.startswith(("/", "..")) or ".." in path.split("/"):
            raise IsolationViolation(f"bundle path escapes the work directory: {path}")


class LocalDockerRunner:
    """The demo runner: a fresh, isolated container per run on this machine."""

    name = "local_docker"
    data_mode = "REAL"

    def __init__(self, sandbox: ContainerSandbox | None = None) -> None:
        self.sandbox = sandbox or ContainerSandbox()
        original = self.sandbox.isolation_flags

        def checked_flags(name, src):
            flags = original(name, src)
            assert_contract(spec_from_docker_flags(flags))
            return flags

        self.sandbox.isolation_flags = checked_flags  # every container run asserts the contract

    def run_checks(self, bundle: SandboxBundle, limits: SandboxLimits) -> SandboxResult:
        self.sandbox.timeout = limits.timeout_seconds
        self.sandbox.memory = limits.memory
        self.sandbox.cpus = limits.cpus
        self.sandbox.pids = limits.pids
        self.sandbox.behaviour_package = bundle.behaviour_package
        return self.sandbox.run_checks(bundle.tree)

    def prepare(self, task: Any, bundle: SandboxBundle, limits: SandboxLimits) -> SandboxResult:
        _check_bundle(task, bundle)
        return self.run_checks(bundle, limits)

    def probe_isolation(self) -> dict[str, str]:
        return self.sandbox.probe_isolation()


class RemoteBackend(Protocol):
    def submit(self, job: Mapping[str, Any]) -> Mapping[str, Any]:
        """Run a job; returns ``{"applied": <isolation spec actually applied>, "checks": {...}, "log": str}``."""


class RemoteRunner:
    """Pilot slot: the same job on a dedicated runner. Not available in the demo (STAGED)."""

    name = "remote"
    data_mode = "STAGED"
    reason = "a remote sandbox runner is not part of the demo; checks that run code are unavailable"

    def __init__(self, backend: RemoteBackend | None = None) -> None:
        self.backend = backend

    def job_spec(self, bundle: SandboxBundle, limits: SandboxLimits) -> dict[str, Any]:
        spec = IsolationSpec("none", True, True, True, True, limits.pids, _memory_bytes(limits.memory))
        assert_contract(spec)
        return {"isolation": asdict(spec), "tree": dict(bundle.tree), "behaviour_package": bundle.behaviour_package,
                "timeout_seconds": limits.timeout_seconds, "cpus": limits.cpus}

    def run_checks(self, bundle: SandboxBundle, limits: SandboxLimits) -> SandboxResult:
        if self.backend is None:
            checks = {c: "unavailable" for c in EXECUTABLE_CHECKS}
            checks.update({"container_sandbox": "unavailable", "gofmt": "unavailable"})
            return SandboxResult(checks, self.reason)
        reply = self.backend.submit(self.job_spec(bundle, limits))
        applied = dict(reply.get("applied") or {})
        applied["secrets"] = tuple(applied.get("secrets") or ())
        applied["host_mounts"] = tuple(applied.get("host_mounts") or ())
        assert_contract(IsolationSpec(**applied))  # the runner verifies what was actually enforced
        checks = {c: "failed" for c in ("gofmt",) + EXECUTABLE_CHECKS}
        checks.update({k: v for k, v in dict(reply.get("checks") or {}).items() if k in checks})
        checks["container_sandbox"] = "passed"
        return SandboxResult(checks, str(reply.get("log", "")), True)

    def prepare(self, task: Any, bundle: SandboxBundle, limits: SandboxLimits) -> SandboxResult:
        _check_bundle(task, bundle)
        return self.run_checks(bundle, limits)


class NoExecutionRunner:
    """No container engine: trusted, non-executing checks only."""

    name = "none"
    data_mode = "STAGED"

    def __init__(self, sandbox: NonExecutingSandbox | None = None) -> None:
        self.sandbox = sandbox or NonExecutingSandbox()

    def run_checks(self, bundle: SandboxBundle, limits: SandboxLimits) -> SandboxResult:
        return self.sandbox.run_checks(bundle.tree)

    def prepare(self, task: Any, bundle: SandboxBundle, limits: SandboxLimits) -> SandboxResult:
        _check_bundle(task, bundle)
        return self.run_checks(bundle, limits)


def select_runner(env: Mapping[str, str] | None = None, *, remote_backend: RemoteBackend | None = None) -> SandboxRunner:
    name = ((env if env is not None else os.environ).get(RUNNER_SETTING) or "local_docker").strip().lower()
    if name not in RUNNER_NAMES:
        raise ValueError(f"{RUNNER_SETTING} must be one of {', '.join(RUNNER_NAMES)}")
    if name == "local_docker":
        return LocalDockerRunner()
    if name == "remote":
        return RemoteRunner(remote_backend)
    return NoExecutionRunner()
