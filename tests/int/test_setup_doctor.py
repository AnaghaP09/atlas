"""First-run setup and the doctor, in a clean temporary folder."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from atlas import doctor
from atlas.setup import fill_env, next_steps, run_setup

ROOT = Path(__file__).resolve().parents[2]


def _fresh(tmp_path: Path) -> Path:
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    return tmp_path


def test_setup_writes_env_from_example_and_keeps_it_private(tmp_path) -> None:
    root = _fresh(tmp_path)
    lines: list[str] = []
    code = run_setup(root, answers={"ATLAS_ORG_SLUG": "acme", "ATLAS_PUBLISH_TARGET": "acme/sandbox",
                                    "ATLAS_PORT": "8100", "ATLAS_DATA_DIR": "atlas-data"}, out=lines.append)
    assert code == 0
    env = (root / ".env").read_text(encoding="utf-8")
    assert "ATLAS_ORG_SLUG=acme" in env and "ATLAS_PUBLISH_TARGET=acme/sandbox" in env and "ATLAS_PORT=8100" in env
    assert (root / "atlas-data" / "local").is_dir()
    assert any("python -m atlas.serve" in line for line in lines)


def test_setup_never_overwrites_without_force(tmp_path) -> None:
    root = _fresh(tmp_path)
    (root / ".env").write_text("ANTHROPIC_API_KEY=keep-me\n", encoding="utf-8")
    run_setup(root, answers={"ATLAS_ORG_SLUG": "acme"}, out=lambda _: None)
    assert (root / ".env").read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=keep-me\n"
    run_setup(root, answers={"ATLAS_ORG_SLUG": "acme"}, force=True, out=lambda _: None)
    assert "ATLAS_ORG_SLUG=acme" in (root / ".env").read_text(encoding="utf-8")


def test_setup_does_not_print_secrets(tmp_path) -> None:
    root = _fresh(tmp_path)
    lines: list[str] = []
    run_setup(root, answers={"ANTHROPIC_API_KEY": "sk-test-not-real"}, out=lines.append)
    assert not any("sk-test-not-real" in line for line in lines)


def test_fill_env_keeps_comments_and_unknown_lines() -> None:
    out = fill_env("# comment\nA=\nB=2\n", {"A": "1", "C": "3"})
    assert out.splitlines() == ["# comment", "A=1", "B=2", "C=3"]


def test_next_steps_are_specific_to_each_system() -> None:
    win = "\n".join(next_steps("Windows", "8000"))
    linux = "\n".join(next_steps("Linux", "8000"))
    mac = "\n".join(next_steps("Darwin", "8000"))
    assert r".venv\Scripts\python" in win and "certutil -user" in win
    assert ".venv/bin/python" in linux and "update-ca-certificates" in linux
    assert "Keychain" in mac


def test_doctor_without_env_says_to_run_setup(tmp_path) -> None:
    checks = doctor.run_checks(_fresh(tmp_path))
    settings = next(c for c in checks if c.name == "settings")
    assert settings.status == doctor.FAIL and "atlas.setup" in settings.message


def test_doctor_after_setup_has_no_failures_without_docker(tmp_path) -> None:
    root = _fresh(tmp_path)
    run_setup(root, answers={"ATLAS_PORT": "8199"}, out=lambda _: None)
    checks = doctor.run_checks(root)
    assert not [c for c in checks if c.status == doctor.FAIL], [c for c in checks if c.status == doctor.FAIL]
    assert any(c.name == "publication" and c.status == doctor.WARN for c in checks)


def test_doctor_reports_a_stopped_docker_engine_plainly() -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "cannot connect")

    check = doctor.check_docker({"ATLAS_DOCKER_BIN": __file__}, run=fake_run)
    assert check.status == doctor.WARN and "not running" in check.message


def test_doctor_skips_docker_for_other_runners() -> None:
    assert doctor.check_docker({"ATLAS_SANDBOX_RUNNER": "none"}).status == doctor.OK
