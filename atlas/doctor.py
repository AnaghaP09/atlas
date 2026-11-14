"""Check that this copy of ATLAS is ready to run, and say what to fix.

    python -m atlas.doctor

Each line starts with OK, WARN or FAIL. FAIL means ATLAS cannot start or a core feature cannot
work; WARN means an optional feature is off. The exit code is 1 if anything failed.
Secret values are never printed; only whether they are set.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    message: str


def _tool(env: Mapping[str, str], setting: str, name: str) -> str | None:
    configured = env.get(setting, "").strip()
    if configured:
        return configured if Path(configured).exists() else None
    return shutil.which(name)


def check_python() -> Check:
    v = sys.version_info
    if v < (3, 12):
        return Check(FAIL, "python", f"Python {v.major}.{v.minor} is too old; install Python 3.14.")
    if v < (3, 14):
        return Check(WARN, "python", f"Python {v.major}.{v.minor} works for most parts; the locked packages target 3.14.")
    return Check(OK, "python", f"Python {v.major}.{v.minor}.{v.micro}")


def check_packages() -> Check:
    missing = [m for m in ("anthropic", "fastapi", "uvicorn", "httpx", "pydantic") if importlib.util.find_spec(m) is None]
    if missing:
        return Check(FAIL, "packages", "missing " + ", ".join(missing)
                     + "; run: python -m pip install --require-hashes -r requirements.txt")
    return Check(OK, "packages", "the required packages are installed")


def check_settings(root: Path, env_file: str) -> tuple[list[Check], dict[str, str], object | None]:
    from atlas.config import ConfigError, load_settings, parse_env_file

    path = root / env_file
    if not path.exists():
        return [Check(FAIL, "settings", f"{env_file} not found; run: python -m atlas.setup")], {}, None
    env = parse_env_file(path)
    env.update({k: v for k, v in os.environ.items() if k.startswith(("ATLAS_", "ANTHROPIC_"))})
    checks: list[Check] = []
    try:
        settings = load_settings(path)
    except ConfigError as exc:
        return [Check(FAIL, "settings", f"{env_file} has problems: {exc}")], env, None
    checks.append(Check(OK, "settings", f"{env_file} loads without errors"))
    for warning in getattr(settings, "warnings", ()) or ():
        checks.append(Check(WARN, "settings", str(warning)))
    provider = env.get("ATLAS_MODEL_PROVIDER", "anthropic") or "anthropic"
    if provider == "anthropic" and not env.get("ANTHROPIC_API_KEY"):
        checks.append(Check(WARN, "model", "ANTHROPIC_API_KEY is not set; Scout and Sentinel cannot call a model"))
    else:
        checks.append(Check(OK, "model", f"model provider '{provider}' is configured (key set: "
                            f"{'yes' if env.get('ANTHROPIC_API_KEY') or provider != 'anthropic' else 'no'})"))
    target = env.get("ATLAS_PUBLISH_TARGET", "")
    if not target or "<" in target:
        checks.append(Check(WARN, "publication", "ATLAS_PUBLISH_TARGET is not set; the coding agent cannot publish"))
    else:
        checks.append(Check(OK, "publication", f"publication target: {target}"))
    return checks, env, settings


def check_docker(env: Mapping[str, str], run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Check:
    runner = env.get("ATLAS_SANDBOX_RUNNER", "local_docker") or "local_docker"
    if runner != "local_docker":
        return Check(OK, "docker", f"sandbox runner is '{runner}'; Docker is not needed")
    docker = _tool(env, "ATLAS_DOCKER_BIN", "docker")
    if docker is None:
        return Check(WARN, "docker", "Docker was not found; install Docker or set ATLAS_DOCKER_BIN. "
                     "Without it, tasks can be prepared but never approved for publication")
    try:
        result = run([docker, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return Check(WARN, "docker", "Docker is installed but did not answer; start Docker and try again")
    if result.returncode != 0:
        return Check(WARN, "docker", "Docker is installed but its engine is not running; start Docker")
    return Check(OK, "docker", f"Docker engine {result.stdout.strip()} is running")


def check_image_cache(root: Path, env: Mapping[str, str]) -> Check:
    cache = Path(env.get("ATLAS_IMAGE_CACHE") or "data/local/images")
    cache = cache if cache.is_absolute() else root / cache
    manifest = cache / "manifest.json"
    if not manifest.exists():
        return Check(WARN, "images", "the sandbox images are not prepared; run: python -m prep.images.fetch_images")
    try:
        names = sorted(json.loads(manifest.read_text(encoding="utf-8")))
    except ValueError:
        return Check(WARN, "images", "the sandbox image list is unreadable; run: python -m prep.images.fetch_images")
    missing = [n for n in names if not (cache / f"{n}.tar").exists()]
    if missing:
        return Check(WARN, "images", "missing image files: " + ", ".join(missing) + "; run: python -m prep.images.fetch_images")
    return Check(OK, "images", "sandbox images ready: " + ", ".join(names))


def check_optional_tool(env: Mapping[str, str], setting: str, name: str, purpose: str) -> Check:
    found = _tool(env, setting, name)
    if found is None:
        return Check(WARN, name, f"{name} was not found ({purpose}); install it or set {setting}")
    return Check(OK, name, f"{name} found")


def check_port(host: str, port: int) -> Check:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        in_use = s.connect_ex((host, port)) == 0
    if in_use:
        return Check(WARN, "port", f"something is already listening on {host}:{port} (ATLAS may already be running)")
    return Check(OK, "port", f"{host}:{port} is free")


def check_data_dir(root: Path, env: Mapping[str, str]) -> Check:
    data = Path(env.get("ATLAS_DATA_DIR") or "data")
    data = data if data.is_absolute() else root / data
    try:
        (data / "local").mkdir(parents=True, exist_ok=True)
        probe = data / "local" / ".write-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return Check(FAIL, "data", f"cannot write to {data}; choose another ATLAS_DATA_DIR")
    return Check(OK, "data", f"data folder is writable: {data}")


def check_ui(root: Path) -> Check:
    if (root / "ui" / "dist" / "index.html").exists():
        return Check(OK, "ui", "the UI is built and will be served at /")
    return Check(WARN, "ui", "the UI is not built; the API still works at /api/docs")


def run_checks(root: Path, env_file: str = ".env") -> list[Check]:
    checks = [check_python(), check_packages()]
    settings_checks, env, settings = check_settings(root, env_file)
    checks += settings_checks
    checks += [check_data_dir(root, env), check_docker(env), check_image_cache(root, env),
               check_optional_tool(env, "ATLAS_GO_BIN", "go", "used for Go checks outside the sandbox"),
               check_optional_tool(env, "ATLAS_OPENSSL_BIN", "openssl", "used to create local certificates for the HTTPS view"),
               check_ui(root)]
    if settings is not None:
        checks.append(check_port(getattr(settings, "host", "127.0.0.1"), int(getattr(settings, "port", 8000))))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas.doctor", description="Check this copy of ATLAS.")
    parser.add_argument("--root", default=".", help="the ATLAS folder (default: current folder)")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)
    checks = run_checks(Path(args.root).resolve(), args.env_file)
    for c in checks:
        print(f"{c.status:<4}  {c.name:<12} {c.message}")
    failed = [c for c in checks if c.status == FAIL]
    print("")
    print("Ready to start: python -m atlas.serve" if not failed else f"{len(failed)} problem(s) to fix before starting.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
