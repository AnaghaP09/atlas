"""Start the ATLAS customer view.

    python -m deploy.up [--scenarios] [--env-file .env]

Checks first, and stops with a plain explanation if something is missing:
configuration, certificates, the hosts entry, certificate trust (Windows only), the sandbox image
cache and Docker. Then it starts the server (and the scenario server with --scenarios) and waits
until it answers over HTTPS. Nothing is downloaded at this point.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from deploy.certs import CertPaths, find_openssl, fingerprint_sha1
from deploy.envfile import read_env

SERVER_MODULE = "atlas.serve"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def resolves_to_loopback(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return any(info[4][0] in ("127.0.0.1", "::1") for info in infos)


def windows_trusts(openssl: Path, ca: Path) -> bool | None:
    """Is the local CA in the current user's Windows trust store? None when it cannot be checked."""
    if platform.system().lower() != "windows":
        return None
    try:
        thumb = fingerprint_sha1(openssl, ca)
        result = subprocess.run(["certutil", "-user", "-store", "Root", thumb],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode == 0


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) != 0


def run_checks(root: Path, env: dict[str, str], *, scenarios: bool,
               resolver: Callable[[str], bool] = resolves_to_loopback,
               trust_check: Callable[[Path, Path], bool | None] = windows_trusts,
               ports: Callable[[str, int], bool] = port_free) -> list[Check]:
    checks: list[Check] = []
    org = (env.get("ATLAS_ORG_SLUG") or "demo").lower()
    data_dir = Path(env.get("ATLAS_DATA_DIR") or "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    certs = CertPaths.under(data_dir)

    checks.append(Check("sign-in mode", env.get("ATLAS_IDENTITY") == "local",
                        "ATLAS_IDENTITY must be 'local' (run `python -m deploy.init`)"))
    checks.append(Check("users", (data_dir / "local" / "users.toml").is_file(),
                        "no sign-in users yet (run `python -m deploy.init`)"))
    checks.append(Check("certificates", certs.server_cert.is_file() and certs.server_key.is_file(),
                        "no server certificate yet (run `python -m deploy.init`)"))
    hosts = [f"atlas.{org}.internal"] + ([f"scenario.{org}.internal"] if scenarios else [])
    for host in hosts:
        checks.append(Check(f"hosts entry for {host}", resolver(host),
                            f"{host} does not point to 127.0.0.1; add it to your hosts file (see setup output)"))
    openssl = find_openssl(env.get("ATLAS_OPENSSL_BIN") or None)
    if openssl and certs.ca_cert.is_file():
        trusted = trust_check(openssl, certs.ca_cert)
        if trusted is not None:
            checks.append(Check("certificate trust", trusted,
                                "the local certificate authority is not trusted yet; run the certutil step from setup"))
    cache = Path(env.get("ATLAS_IMAGE_CACHE") or (data_dir / "local" / "images"))
    if not cache.is_absolute():
        cache = root / cache
    runner = (env.get("ATLAS_SANDBOX_RUNNER") or "local_docker").lower()
    if runner == "local_docker":
        checks.append(Check("sandbox image cache", (cache / "manifest.json").is_file(),
                            "the sandbox images are not cached; run `python -m prep.images.fetch_images` once while online"))
        docker = env.get("ATLAS_DOCKER_BIN") or None
        from shutil import which
        checks.append(Check("Docker", bool(docker or which("docker")),
                            "Docker was not found; start Docker Desktop or set ATLAS_DOCKER_BIN"))
    for port in [443] + ([8443] if scenarios else []):
        checks.append(Check(f"port {port}", ports("127.0.0.1", port), f"something is already using port {port}"))
    return checks


def wait_healthy(url: str, ca: Path, timeout: float = 60.0) -> bool:
    context = ssl.create_default_context(cafile=str(ca))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, context=context, timeout=3) as response:
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(1)
    return False


def start_server(root: Path, data_dir: Path, scenario: bool) -> int:
    run_dir = data_dir / "local" / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    log = open(run_dir / ("scenario.log" if scenario else "atlas.log"), "ab")
    args = [sys.executable, "-m", SERVER_MODULE, "--site"] + (["--scenario"] if scenario else [])
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(args, cwd=root, stdout=log, stderr=log, creationflags=flags)
    (run_dir / ("scenario.pid" if scenario else "atlas.pid")).write_text(str(process.pid), encoding="utf-8")
    return process.pid


def run(argv: list[str] | None = None, *, out=sys.stdout, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m deploy.up", description="Start the ATLAS customer view.")
    parser.add_argument("--scenarios", action="store_true", help="also start the scenario server")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    root = root or Path.cwd()
    env = read_env(root / args.env_file)
    env.update({k: v for k, v in os.environ.items() if k.startswith("ATLAS_")})

    checks = run_checks(root, env, scenarios=args.scenarios)
    failed = [c for c in checks if not c.ok]
    for check in checks:
        print(f"  [{'ok' if check.ok else 'missing'}] {check.name}" + ("" if check.ok else f": {check.detail}"), file=out)
    if failed:
        print("\nATLAS was not started. Fix the items above, then run this again.", file=out)
        return 1
    if args.check_only:
        return 0

    org = (env.get("ATLAS_ORG_SLUG") or "demo").lower()
    data_dir = Path(env.get("ATLAS_DATA_DIR") or "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    certs = CertPaths.under(data_dir)
    start_server(root, data_dir, scenario=False)
    if args.scenarios:
        start_server(root, data_dir, scenario=True)
    url = f"https://atlas.{org}.internal/api/health"
    if not wait_healthy(url, certs.ca_cert):
        print(f"ATLAS did not answer at {url}. See {data_dir / 'local' / 'run' / 'atlas.log'}.", file=out)
        return 1
    print(f"\nATLAS is running: https://atlas.{org}.internal", file=out)
    if args.scenarios:
        print(f"Scenario server:  https://scenario.{org}.internal:8443", file=out)
    print("Stop it with:     python -m deploy.down", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
