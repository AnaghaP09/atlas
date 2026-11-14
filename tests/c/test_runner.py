from dataclasses import asdict

import pytest

from atlas.execution import diffs
from atlas.execution.images import ImageCache
from atlas.execution.runner import (
    IsolationSpec,
    IsolationViolation,
    LocalDockerRunner,
    NoExecutionRunner,
    RemoteRunner,
    SandboxBundle,
    SandboxLimits,
    SandboxRunner,
    assert_contract,
    select_runner,
    spec_from_docker_flags,
)
from atlas.execution.sandbox import ContainerSandbox, NonExecutingSandbox
from tests.c.fakes import FIX_DIFF, TARGET

GOOD = IsolationSpec("none", True, True, True, True, 256, 1 << 30)


@pytest.mark.parametrize("change", [
    {"network": "bridge"}, {"read_only_root": False}, {"non_root": False}, {"cap_drop_all": False},
    {"no_new_privileges": False}, {"pids_limit": 0}, {"pids_limit": 10_000}, {"memory_bytes": 0},
    {"memory_bytes": 8 << 30}, {"secrets": ("GITHUB_TOKEN",)}, {"host_mounts": ("C:/Users:/host",)},
])
def test_contract_rejects_each_weakening(change):
    with pytest.raises(IsolationViolation):
        assert_contract(IsolationSpec(**{**asdict(GOOD), **change}))
    assert_contract(GOOD)


def test_local_docker_flags_satisfy_contract_and_tampering_is_caught(tmp_path):
    sandbox = ContainerSandbox(ImageCache(tmp_path))
    flags = sandbox.isolation_flags("n", tmp_path)
    assert_contract(spec_from_docker_flags(flags))
    runner = LocalDockerRunner(sandbox)
    original = runner.sandbox.isolation_flags
    runner.sandbox.memory = "16g"
    with pytest.raises(IsolationViolation, match="memory"):
        original("n", tmp_path)


def test_bundle_paths_cannot_escape():
    with pytest.raises(IsolationViolation):
        NoExecutionRunner(NonExecutingSandbox(gofmt=None)).prepare(None, SandboxBundle({"../x": "y"}), SandboxLimits())


def test_remote_slot_is_staged_without_backend():
    runner = RemoteRunner()
    result = runner.prepare(None, SandboxBundle({"go.mod": "module x\n"}), SandboxLimits())
    assert runner.data_mode == "STAGED"
    assert set(result.checks.values()) == {"unavailable"} and "not part of the demo" in result.log


class FakeRemoteBackend:
    """Enforces the contract it is given and reports what it applied."""

    def __init__(self, weaken: dict | None = None) -> None:
        self.jobs = []
        self.weaken = weaken or {}

    def submit(self, job):
        self.jobs.append(job)
        applied = {**job["isolation"], **self.weaken}
        assert_contract(IsolationSpec(**{**applied, "secrets": tuple(applied["secrets"]),
                                         "host_mounts": tuple(applied["host_mounts"])})) if not self.weaken else None
        return {"applied": applied, "log": "remote ok",
                "checks": {"gofmt": "passed", "go_build": "passed", "go_vet": "passed", "behavioural_tests": "passed"}}


def test_remote_runner_against_fake_enforcing_same_contract():
    backend = FakeRemoteBackend()
    result = RemoteRunner(backend).prepare(None, SandboxBundle({"go.mod": "module x\n"}), SandboxLimits())
    assert result.checks["container_sandbox"] == "passed" and result.checks["behavioural_tests"] == "passed"
    assert backend.jobs[0]["isolation"]["network"] == "none" and "secrets" in backend.jobs[0]["isolation"]


def test_remote_runner_refuses_backend_that_applied_weaker_isolation():
    with pytest.raises(IsolationViolation):
        RemoteRunner(FakeRemoteBackend(weaken={"network": "bridge"})).run_checks(SandboxBundle({}), SandboxLimits())


def test_select_runner_from_setting():
    assert isinstance(select_runner({}), LocalDockerRunner)
    assert isinstance(select_runner({"ATLAS_SANDBOX_RUNNER": "remote"}), RemoteRunner)
    assert isinstance(select_runner({"ATLAS_SANDBOX_RUNNER": "none"}), NoExecutionRunner)
    with pytest.raises(ValueError):
        select_runner({"ATLAS_SANDBOX_RUNNER": "laptop"})
    for r in (LocalDockerRunner(), RemoteRunner(), NoExecutionRunner()):
        assert isinstance(r, SandboxRunner)


def _ready() -> bool:
    try:
        return ImageCache().ensure("go") is not None
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _ready(), reason="container engine or cached sandbox image not available")
def test_local_docker_runner_end_to_end():
    base = {p.relative_to(TARGET).as_posix(): p.read_text(encoding="utf-8") for p in TARGET.rglob("*") if p.is_file()}
    runner = LocalDockerRunner()
    fixed = runner.prepare(None, SandboxBundle(diffs.apply(base, diffs.parse(FIX_DIFF))), SandboxLimits())
    assert fixed.checks["behavioural_tests"] == "passed" and fixed.checks["container_sandbox"] == "passed", fixed.log
    assert set(runner.probe_isolation().values()) == {"blocked"}
