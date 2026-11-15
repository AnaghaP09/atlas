"""First-run setup for the ATLAS customer view. Safe to run again.

    python -m deploy.init [--org demo] [--env-file .env] [--reset-passwords]

What it does:
1. Creates ``.env`` from ``.env.example`` if it is missing, and turns on built-in sign-in.
2. Copies the example projects to your private data directory (if not there yet).
3. Creates one demo user per role, with generated passwords, and stores only password hashes.
   The passwords are printed once. Keep them somewhere safe; they are not stored anywhere else.
4. Creates a local certificate authority and the server certificate with OpenSSL.
5. Prints the two one-time steps for your operating system (hosts file and certificate trust).
"""

from __future__ import annotations

import argparse
import re
import secrets
import shutil
import sys
import tomllib
from pathlib import Path

from atlas.identity import hash_password, write_users_file
from deploy.certs import CertPaths, CertificateError, ensure_ca, ensure_server_cert, find_openssl
from deploy.envfile import ensure_env_file, read_env, set_env_values
from deploy.steps import one_time_steps

ROLES = [
    ("pm", "Product manager (demo)"),
    ("em", "Engineering manager (demo)"),
    ("architect", "Solution architect (demo)"),
    ("security", "Security engineer (demo)"),
    ("oncall", "On-call engineer (demo)"),
    ("audit", "Auditor (demo)"),
]
HERE = Path(__file__).resolve().parent


def host_names(org: str, domain: str = "internal") -> list[str]:
    return [f"atlas.{org}.{domain}", f"scenario.{org}.{domain}"]


def private_paths(data_dir: Path) -> dict[str, Path]:
    local = data_dir / "local"
    return {"local": local, "projects": local / "projects.toml", "users": local / "users.toml"}


def _section_header(project: str) -> str:
    key = project if re.fullmatch(r"[A-Za-z0-9_-]+", project) and "-" not in project else f'"{project}"'
    return f"[projects.{key}.export]"


def grant_publish_target(projects_file: Path, project: str, target: str) -> bool:
    """Give ``project`` an export grant to publish to ``target`` when it has none yet.

    Only an empty ``publish_targets`` list in that project's export section is filled; a list the
    owner already edited is left as it is. Returns True when the file changed."""
    if not target or "<" in target or "/" not in target:
        return False
    text = projects_file.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    current = (((data.get("projects") or {}).get(project) or {}).get("export") or {}).get("publish_targets")
    if current:
        return False
    header = _section_header(project)
    lines = text.splitlines()
    out, in_section, done = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_section and not done:
                out.append(f'publish_targets = ["{target}"]')
                done = True
            in_section = stripped == header
        if in_section and not done and re.fullmatch(r"publish_targets\s*=\s*\[\s*\]", stripped):
            out.append(f'publish_targets = ["{target}"]')
            done = True
            continue
        out.append(line)
    if not done:
        if not in_section:
            out += ["", header]
        out.append(f'publish_targets = ["{target}"]')
    projects_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    tomllib.loads(projects_file.read_text(encoding="utf-8"))  # still valid TOML
    return True


def project_ids(projects_file: Path) -> list[str]:
    data = tomllib.loads(projects_file.read_text(encoding="utf-8"))
    return sorted((data.get("projects") or {}).keys())


def create_users(users_file: Path, projects: list[str]) -> list[tuple[str, str, str]]:
    """Write one demo user per role. Returns (user, display name, password) to show once."""
    shown: list[tuple[str, str, str]] = []
    entries = []
    for role, display in ROLES:
        password = secrets.token_urlsafe(12)
        entries.append({"name": role, "display_name": display, "password_hash": hash_password(password),
                        "roles": {p: role for p in projects}})
        shown.append((role, display, password))
    write_users_file(users_file, entries)
    return shown


def run(argv: list[str] | None = None, *, out=sys.stdout, system: str | None = None,
        repo_root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m deploy.init", description="First-run setup for ATLAS.")
    parser.add_argument("--org", help="organisation slug used in host names (default: from .env, else 'demo')")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--reset-passwords", action="store_true", help="create new demo passwords")
    parser.add_argument("--renew-certificate", action="store_true", help="issue a new server certificate")
    args = parser.parse_args(argv)

    root = repo_root or Path.cwd()
    env_path = (root / args.env_file) if not Path(args.env_file).is_absolute() else Path(args.env_file)
    created_env = ensure_env_file(env_path, root / ".env.example")
    env = read_env(env_path)

    org = (args.org or env.get("ATLAS_ORG_SLUG") or "demo").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", org):
        print("The organisation slug must use lowercase letters, digits and hyphens.", file=out)
        return 2
    domain = (env.get("ATLAS_SITE_DOMAIN") or "internal").lower()
    data_dir = Path(env.get("ATLAS_DATA_DIR") or "data")
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    paths = private_paths(data_dir)
    paths["local"].mkdir(parents=True, exist_ok=True)

    print(f"Setting up ATLAS for '{org}'.", file=out)
    if created_env:
        print(f"- Created {env_path.name} from .env.example.", file=out)

    if not paths["projects"].is_file():
        shutil.copyfile(HERE / "projects.example.toml", paths["projects"])
        print("- Copied the example projects to your data directory. Edit them to use your own projects.", file=out)
    projects = project_ids(paths["projects"])
    if not projects:
        print("No projects are defined. Add at least one project, then run setup again.", file=out)
        return 2
    target = (env.get("ATLAS_PUBLISH_TARGET") or "").strip()
    if grant_publish_target(paths["projects"], projects[0], target):
        print(f"- Let project '{projects[0]}' publish to {target} (from ATLAS_PUBLISH_TARGET).", file=out)

    updates = {"ATLAS_IDENTITY": "local", "ATLAS_ORG_SLUG": org, "ATLAS_DEFAULT_PROJECT": projects[0]}
    set_env_values(env_path, {k: v for k, v in updates.items() if k != "ATLAS_ORG_SLUG"}, overwrite=False)
    set_env_values(env_path, {"ATLAS_ORG_SLUG": org}, overwrite=bool(args.org))
    set_env_values(env_path, {"ATLAS_IDENTITY": "local"}, overwrite=True)

    if args.reset_passwords or not paths["users"].is_file():
        shown = create_users(paths["users"], projects)
        print("\nDemo users (shown once - copy the passwords now):\n", file=out)
        for user, display, password in shown:
            print(f"    {user:<10} {display:<28} {password}", file=out)
        print("", file=out)
    else:
        print("- Demo users already exist. Use --reset-passwords to make new passwords.", file=out)

    openssl = find_openssl(env.get("ATLAS_OPENSSL_BIN") or None)
    if openssl is None:
        print("OpenSSL was not found. Install it, or set ATLAS_OPENSSL_BIN in .env, then run setup again.", file=out)
        return 3
    certs = CertPaths.under(data_dir)
    try:
        if ensure_ca(certs, openssl, org):
            print("- Created a local certificate authority. Its private key stays in your data directory.", file=out)
        if ensure_server_cert(certs, openssl, host_names(org, domain), renew=args.renew_certificate):
            print("- Created the server certificate.", file=out)
    except CertificateError as exc:
        print(f"Could not create certificates: {exc}", file=out)
        return 3

    print("", file=out)
    print(one_time_steps(host_names(org, domain), certs.public_ca, system=system), file=out)
    print("Then start ATLAS with:  python -m deploy.up", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
