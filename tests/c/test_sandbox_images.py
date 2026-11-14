import json
import subprocess
from pathlib import Path

import pytest

from atlas.execution import diffs
from atlas.execution.agent import AgentError, CodingAgent
from atlas.execution.checks import config_path_check, personal_data_scan, scope_check, secret_scan
from atlas.execution.images import IMAGES, DigestMismatch, ImageCache, ImageMissing, sha256_file
from atlas.execution.sandbox import ContainerSandbox, NonExecutingSandbox
from prep.images.fetch_images import FetchError, fetch, pull_with_retries
from prep.planted.build_planted import PlantedError, build
from tests.c.fakes import FIX_DIFF, TARGET
from tests.fakes import FakeOutbound, RecordedModel


GO = IMAGES["go"]
MANIFEST = GO.manifest_digest
CONFIG_ID = "sha256:" + "c0" * 32  # deliberately different from the manifest digest
REPO_DIGEST = f"golang@{MANIFEST}"


class Recorder:
    """Fake docker runner. ``images`` maps a reference to (image id, repo digests)."""

    def __init__(self, images: dict | None = None, load_gives: dict | None = None, pull_ok: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.images = dict(images or {})
        self.load_gives = dict(load_gives or {})
        self.pull_ok = pull_ok

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        if args[:2] == ["image", "inspect"]:
            ref = args[-1]
            if ref in self.images:
                image_id, digests = self.images[ref]
                return subprocess.CompletedProcess(args, 0, f"{json.dumps(image_id)} {json.dumps(digests)}\n", "")
            return subprocess.CompletedProcess(args, 1, "", "No such image")
        if args[0] == "load":
            self.images.update(self.load_gives)
            return subprocess.CompletedProcess(args, 0, "Loaded image: golang", "")
        if args[0] == "pull":
            if self.pull_ok:
                self.images[GO.pull_ref] = (CONFIG_ID, [REPO_DIGEST])
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 1, "", "server gave HTTP response to HTTPS client")
        if args[0] == "save":
            Path(args[2]).write_bytes(b"saved image " + args[3].encode())
        return subprocess.CompletedProcess(args, 0, "", "")


def _manifest(tmp_path, tar_sha=None, image_id=CONFIG_ID):
    (tmp_path / "manifest.json").write_text(json.dumps({"go": {
        "manifest_digest": MANIFEST, "image_id": image_id,
        "tar_sha256": tar_sha or sha256_file(tmp_path / "go.tar")}}))


def test_missing_image_gives_clear_error_without_network(tmp_path):
    runner = Recorder()
    with pytest.raises(ImageMissing, match="prep.images.fetch_images"):
        ImageCache(tmp_path, runner).ensure("go")
    assert not any(c[0] in ("pull", "login", "buildx") for c in runner.calls)


def test_fetch_records_manifest_digest_and_distinct_image_id(tmp_path):
    runner = Recorder(pull_ok=True)
    cache = ImageCache(tmp_path, runner)
    manifest = fetch(["go"], cache, sleep=lambda s: None)
    assert manifest["go"]["manifest_digest"] == MANIFEST and manifest["go"]["image_id"] == CONFIG_ID
    assert manifest["go"]["tar_sha256"] == sha256_file(tmp_path / "go.tar")
    assert ["save", "-o", str(tmp_path / "go.tar"), GO.pull_ref] in runner.calls


def test_fetch_refuses_pull_without_pinned_repo_digest(tmp_path):
    runner = Recorder(images={GO.pull_ref: (CONFIG_ID, ["golang@sha256:" + "9" * 64])})
    with pytest.raises(FetchError):
        fetch(["go"], ImageCache(tmp_path, runner), sleep=lambda s: None)


def test_load_from_cache_verifies_recorded_image_id(tmp_path):
    (tmp_path / "go.tar").write_bytes(b"image")
    _manifest(tmp_path)
    runner = Recorder(load_gives={CONFIG_ID: (CONFIG_ID, [])})  # a loaded image may carry no repo digest
    assert ImageCache(tmp_path, runner).ensure("go") == CONFIG_ID
    assert not any(c[0] == "pull" for c in runner.calls)


def test_tampered_cache_tar_refused(tmp_path):
    (tmp_path / "go.tar").write_bytes(b"not the image")
    _manifest(tmp_path, tar_sha="0" * 64)
    runner = Recorder()
    with pytest.raises(DigestMismatch):
        ImageCache(tmp_path, runner).ensure("go")
    assert not any(c[0] == "load" for c in runner.calls)


def test_loaded_image_with_other_id_refused(tmp_path):
    (tmp_path / "go.tar").write_bytes(b"image")
    _manifest(tmp_path)
    other = "sha256:" + "f" * 64
    runner = Recorder(load_gives={other: (other, [])})
    with pytest.raises(DigestMismatch):
        ImageCache(tmp_path, runner).ensure("go")


def test_engine_image_without_pinned_digest_refused(tmp_path):
    runner = Recorder(images={GO.pull_ref: (CONFIG_ID, ["golang@sha256:" + "a" * 64])})
    with pytest.raises(DigestMismatch):
        ImageCache(tmp_path, runner).ensure("go")


def test_cache_for_other_digest_refused(tmp_path):
    (tmp_path / "go.tar").write_bytes(b"image")
    (tmp_path / "manifest.json").write_text(json.dumps({"go": {"manifest_digest": "sha256:" + "1" * 64,
                                                               "image_id": CONFIG_ID, "tar_sha256": "x"}}))
    with pytest.raises(DigestMismatch):
        ImageCache(tmp_path, Recorder()).ensure("go")


def test_fetch_retries_with_backoff_then_reports():
    sleeps = []
    with pytest.raises(FetchError, match="after 3 attempts"):
        pull_with_retries(IMAGES["alpine"], Recorder(), attempts=3, sleep=sleeps.append)
    assert sleeps == [2.0, 4.0]


def test_sandbox_runs_by_recorded_image_id_with_isolation_flags(tmp_path):
    (tmp_path / "go.tar").write_bytes(b"image")
    _manifest(tmp_path)
    runner = Recorder(images={CONFIG_ID: (CONFIG_ID, [REPO_DIGEST])})
    sb = ContainerSandbox(ImageCache(tmp_path, runner), runner)
    sb.run_checks({"go.mod": "module x\n"})
    run = next(c for c in runner.calls if c[0] == "run")
    for flag in ("--pull=never", "--read-only", "--cap-drop", "no-new-privileges"):
        assert flag in run
    assert run[run.index("--network") + 1] == "none" and run[run.index("--user") + 1] == "65534:65534"
    assert CONFIG_ID in run
    assert not any(a.startswith("-e") and ("TOKEN" in a or "KEY" in a) for a in run)


def test_non_executing_sandbox_runs_no_repository_code():
    seen = []
    sb = NonExecutingSandbox(gofmt="gofmt", runner=lambda args: seen.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    result = sb.run_checks({"netaddr/netaddr.go": "package netaddr\n"})
    assert all(cmd[0] == "gofmt" for cmd in seen)
    assert result.checks["go_vet"] == result.checks["behavioural_tests"] == result.checks["container_sandbox"] == "unavailable"
    assert result.checks["gofmt"] == "passed"


def _docker_ready() -> bool:
    try:
        return ImageCache().ensure("go") is not None
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _docker_ready(), reason="container engine or cached sandbox image not available")
def test_save_load_round_trip_keeps_recorded_image_id(tmp_path):
    cache = ImageCache()
    recorded = cache.manifest()["go"]
    held = cache.inspect(recorded["image_id"])
    assert held is not None and held.image_id == recorded["image_id"]
    out = tmp_path / "go.tar"
    assert cache.run(["save", "-o", str(out), recorded["image_id"]]).returncode == 0
    assert cache.run(["load", "-i", str(out)]).returncode == 0
    assert cache.inspect(recorded["image_id"]).image_id == recorded["image_id"]


def _base_tree() -> dict[str, str]:
    return {p.relative_to(TARGET).as_posix(): p.read_text(encoding="utf-8") for p in TARGET.rglob("*") if p.is_file()}


@pytest.mark.skipif(not _docker_ready(), reason="container engine or cached sandbox image not available")
def test_container_behavioural_check_fails_at_base_and_passes_with_fix():
    sb = ContainerSandbox()
    base = _base_tree()
    before = sb.run_checks(base)
    assert before.checks["go_build"] == "passed" and before.checks["behavioural_tests"] == "failed", before.log
    after = sb.run_checks(diffs.apply(base, diffs.parse(FIX_DIFF)))
    assert after.checks == {"gofmt": "passed", "go_build": "passed", "go_vet": "passed", "behavioural_tests": "passed",
                            "container_sandbox": "passed"}, after.log


@pytest.mark.skipif(not _docker_ready(), reason="container engine or cached sandbox image not available")
def test_container_escape_probes_all_blocked():
    probes = ContainerSandbox().probe_isolation()
    assert set(probes) == {"network", "rootfs", "source_mount", "env_secrets", "root_user", "host_paths"}
    assert all(v == "blocked" for v in probes.values()), probes


# -- diffs and static checks ------------------------------------------------------------------


def test_fix_diff_applies_and_strict_context():
    base = _base_tree()
    tree = diffs.apply(base, diffs.parse(FIX_DIFF))
    assert 'strings.Contains(host, ":")' in tree["netaddr/netaddr.go"]
    assert diffs.tree_sha256(tree) != diffs.tree_sha256(base)
    with pytest.raises(diffs.DiffError):
        diffs.apply(base, diffs.parse(FIX_DIFF.replace('"strconv"', '"os"', 1)))


def test_static_checks():
    parsed = diffs.parse(FIX_DIFF)
    assert scope_check(parsed, ["netaddr/netaddr.go"]).status == "passed"
    assert scope_check(parsed, ["other/*.go"]).status == "failed"
    ci = diffs.parse("--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n@@ -1,1 +1,1 @@\n-a\n+b\n")
    assert config_path_check(ci).status == "failed"
    beh = diffs.parse("--- a/.atlas/behaviour/x_test.go\n+++ b/.atlas/behaviour/x_test.go\n@@ -1,1 +1,1 @@\n-a\n+b\n")
    assert config_path_check(beh).status == "failed"
    assert secret_scan("key = '" + "AKIA" + "ABCDEFGHIJKLMNOP" + "'").status == "failed"
    assert secret_scan("ordinary code").status == "passed"
    assert personal_data_scan("contact someone@example.org").status == "failed"
    binary = diffs.parse("diff --git a/x.bin b/x.bin\nBinary files a/x.bin and b/x.bin differ\n")
    from atlas.execution.checks import binary_check

    assert binary_check(binary).status == "failed"


def test_agent_refusal_and_bad_output():
    refusing = FakeOutbound(RecordedModel(default={"diff": FIX_DIFF}))
    agent = CodingAgent(refusing)
    assert "@@" in agent.propose("t", ["netaddr/netaddr.go"], _base_tree())
    assert refusing.attempts[-1][1].value == "model_call"

    class Refuses(FakeOutbound):
        def call(self, role, purpose, payload):
            from atlas.contracts import ModelResponse

            return ModelResponse({}, "refusal", 1, 0, "m")

    with pytest.raises(AgentError):
        CodingAgent(Refuses()).propose("t", ["netaddr/netaddr.go"], _base_tree())
    with pytest.raises(AgentError):
        CodingAgent(FakeOutbound(RecordedModel(default={"diff": "no diff"}))).propose("t", ["netaddr/netaddr.go"], _base_tree())
    with pytest.raises(AgentError):
        agent.propose("t", ["nothing/*"], _base_tree())


def test_offline_outbound_refuses_agent():
    with pytest.raises(ConnectionRefusedError):
        CodingAgent(FakeOutbound(offline=True)).propose("t", ["netaddr/netaddr.go"], _base_tree())


# -- planted verifier set ------------------------------------------------------------------------


def _records():
    spec = json.loads((Path(__file__).resolve().parents[2] / "prep" / "planted" / "spec.json").read_text())
    out = []
    for entry in spec["cases"]:
        for side in (entry["true"]["support"], entry["wrong"]["cite"]):
            rid = f"rec-{side['source_id']}-{side['part']}"
            if not any(r["record_id"] == rid for r in out):
                out.append({"record_id": rid, **side})
    return out, spec


def test_planted_set_has_ten_and_ten_with_distinct_citations():
    records, spec = _records()
    result = build(records, spec)
    assert len(result["true"]) == len(result["wrong"]) == 10
    for t, w in zip(result["true"], result["wrong"]):
        assert t["evidence_ids"] != w["evidence_ids"] and w["expected"] == "rejected" and t["expected"] == "supported"


def test_planted_set_refuses_missing_records():
    records, spec = _records()
    with pytest.raises(PlantedError):
        build(records[:-1], spec)
