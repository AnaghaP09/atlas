from __future__ import annotations

import io
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from atlas.identity import UserDirectory, verify_password
from deploy import down, init, up
from deploy.certs import CertPaths, describe, find_openssl
from deploy.envfile import read_env, set_env_values
from deploy.steps import one_time_steps

REPO = Path(__file__).resolve().parents[2]
OPENSSL = find_openssl()


def test_env_edits_keep_existing_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nATLAS_IDENTITY=header\nATLAS_ORG_SLUG=\nOTHER=1\n")
    changed = set_env_values(env, {"ATLAS_IDENTITY": "local", "ATLAS_ORG_SLUG": "demo", "NEW": "x"})
    values = read_env(env)
    assert values["ATLAS_IDENTITY"] == "header"  # kept without overwrite
    assert values["ATLAS_ORG_SLUG"] == "demo" and values["NEW"] == "x" and values["OTHER"] == "1"
    assert "# comment" in env.read_text()
    set_env_values(env, {"ATLAS_IDENTITY": "local"}, overwrite=True)
    assert read_env(env)["ATLAS_IDENTITY"] == "local"
    assert set(changed) == {"ATLAS_ORG_SLUG", "NEW"}


@pytest.mark.parametrize("system,hosts_hint,trust_hint", [
    ("Windows", r"C:\Windows\System32\drivers\etc\hosts", "certutil -user -addstore Root"),
    ("Darwin", "/etc/hosts", "security add-trusted-cert"),
    ("Linux", "/etc/hosts", "update-ca-certificates"),
])
def test_one_time_steps_per_system(tmp_path, system, hosts_hint, trust_hint):
    text = one_time_steps(["atlas.demo.internal", "scenario.demo.internal"], tmp_path / "ca.crt", system=system)
    assert "127.0.0.1 atlas.demo.internal scenario.demo.internal" in text
    assert hosts_hint in text and trust_hint in text
    assert "never changes these settings" in text


def test_example_projects_are_examples_with_evaluation_only_on_kubevirt():
    data = tomllib.loads((REPO / "deploy" / "projects.example.toml").read_text(encoding="utf-8"))
    projects = data["projects"]
    assert set(projects) == {"kubevirt", "argo-cd"}
    assert projects["kubevirt"]["evaluation"] is True and projects["argo-cd"]["evaluation"] is False
    assert projects["argo-cd"]["ci"] == "github_actions"
    assert all(not p["export"]["publish_targets"] for p in projects.values())


@pytest.mark.skipif(OPENSSL is None, reason="OpenSSL not found on PATH or next to Git")
def test_init_from_a_clean_directory(tmp_path, monkeypatch):
    (tmp_path / ".env.example").write_text((REPO / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    assert init.run(["--org", "acme"], out=out, system="Windows", repo_root=tmp_path) == 0
    text = out.getvalue()

    env = read_env(tmp_path / ".env")
    assert env["ATLAS_IDENTITY"] == "local" and env["ATLAS_ORG_SLUG"] == "acme"
    assert env["ATLAS_DEFAULT_PROJECT"] == "argo-cd"  # first project id in sorted order

    local = tmp_path / "data" / "local"
    users = UserDirectory.load(local / "users.toml")
    assert set(users.users) == {"pm", "em", "architect", "security", "oncall", "audit"}
    assert users.role_in("em", "kubevirt").value == "em"
    shown = {line.split()[0]: line.split()[-1] for line in text.splitlines() if line.startswith("    ")}
    assert verify_password(shown["pm"], users.get("pm").password_hash)
    assert shown["pm"] not in (local / "users.toml").read_text()

    certs = CertPaths.under(tmp_path / "data")
    assert certs.ca_key.is_file() and certs.ca_key.resolve().is_relative_to((tmp_path / "data").resolve())
    assert certs.public_ca.is_file() and "PRIVATE KEY" not in certs.public_ca.read_text()
    details = describe(OPENSSL, certs.server_cert)
    assert "atlas.acme.internal" in details and "scenario.acme.internal" in details and "127.0.0.1" in details
    assert "certutil -user -addstore Root" in text

    # Running again changes nothing and shows no new passwords.
    ca_before = certs.ca_cert.read_bytes()
    users_before = (local / "users.toml").read_bytes()
    out2 = io.StringIO()
    assert init.run([], out=out2, system="Linux", repo_root=tmp_path) == 0
    assert certs.ca_cert.read_bytes() == ca_before and (local / "users.toml").read_bytes() == users_before
    assert "already exist" in out2.getvalue()
    assert read_env(tmp_path / ".env")["ATLAS_ORG_SLUG"] == "acme"


def test_init_rejects_a_bad_org_slug(tmp_path):
    out = io.StringIO()
    assert init.run(["--org", "Bad Org!"], out=out, repo_root=tmp_path) == 2


def test_up_checks_explain_what_is_missing(tmp_path):
    env = {"ATLAS_IDENTITY": "header", "ATLAS_ORG_SLUG": "demo", "ATLAS_SANDBOX_RUNNER": "none"}
    checks = up.run_checks(tmp_path, env, scenarios=True, resolver=lambda h: False,
                           trust_check=lambda o, c: None, ports=lambda h, p: True)
    failed = {c.name: c.detail for c in checks if not c.ok}
    assert "sign-in mode" in failed and "users" in failed and "certificates" in failed
    assert "hosts entry for atlas.demo.internal" in failed and "hosts entry for scenario.demo.internal" in failed
    assert all("python -m deploy.init" in failed[k] for k in ("sign-in mode", "users", "certificates"))


def test_up_refuses_to_start_when_checks_fail(tmp_path):
    out = io.StringIO()
    (tmp_path / ".env").write_text("ATLAS_IDENTITY=header\n")
    assert up.run(["--check-only"], out=out, root=tmp_path) == 1
    assert "was not started" in out.getvalue()


def test_up_requires_the_image_cache_without_pulling(tmp_path):
    env = {"ATLAS_IDENTITY": "local", "ATLAS_SANDBOX_RUNNER": "local_docker"}
    checks = up.run_checks(tmp_path, env, scenarios=False, resolver=lambda h: True,
                           trust_check=lambda o, c: True, ports=lambda h, p: True)
    cache = [c for c in checks if c.name == "sandbox image cache"][0]
    assert not cache.ok and "fetch_images" in cache.detail


def test_down_stops_recorded_processes(tmp_path):
    run_dir = tmp_path / "data" / "local" / "run"
    run_dir.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (run_dir / "atlas.pid").write_text(str(child.pid))
        out = io.StringIO()
        assert down.run([], out=out, root=tmp_path) == 0
        child.wait(timeout=10)
        assert "stopped atlas" in out.getvalue()
        assert not (run_dir / "atlas.pid").exists()
    finally:
        if child.poll() is None:
            child.kill()
