"""First-run setup for a new copy of ATLAS.

    python -m atlas.setup                       # asks a few questions
    python -m atlas.setup --non-interactive \\
        --publish-target <owner>/<repo> --org demo --port 8000

It creates ``.env`` from ``.env.example`` (it never overwrites an existing ``.env`` unless you pass
``--force``), creates the data directory, and prints the next steps for your operating system.
Secrets are typed without being shown and are never printed back.
"""

from __future__ import annotations

import argparse
import getpass
import platform
import sys
from pathlib import Path
from typing import Callable, Mapping

ASKED = (
    ("ATLAS_ORG_SLUG", "Short name of your organisation, used in web addresses", "demo"),
    ("ATLAS_DEFAULT_PROJECT", "Id of the project to open by default (from deploy/projects.yaml)", ""),
    ("ATLAS_PUBLISH_TARGET", "Repository the coding agent may write to, as owner/repo (leave empty to turn publishing off)", ""),
    ("ATLAS_PORT", "Port for the local server", "8000"),
    ("ATLAS_DATA_DIR", "Folder for ATLAS data", "data"),
)
SECRETS = (("ANTHROPIC_API_KEY", "Anthropic API key (leave empty to add it later)"),)


def fill_env(template: str, values: Mapping[str, str]) -> str:
    """Return ``template`` with ``KEY=`` lines set to the given values; other lines are kept."""
    out, seen = [], set()
    for line in template.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in values.items():
        if key not in seen:
            out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def next_steps(system: str, port: str) -> list[str]:
    venv_python = r".venv\Scripts\python" if system == "Windows" else ".venv/bin/python"
    steps = [
        f"1. Create a virtual environment and install the locked packages:",
        f"     python -m venv .venv",
        f"     {venv_python} -m pip install --require-hashes -r requirements.txt",
        "2. Check your setup:",
        f"     {venv_python} -m atlas.doctor",
        "3. Optional, for the coding-agent sandbox (needs Docker running), prepare the sandbox images once:",
        f"     {venv_python} -m prep.images.fetch_images",
        "4. Start ATLAS:",
        f"     {venv_python} -m atlas.serve",
        f"   Then open http://127.0.0.1:{port}/api/docs in your browser.",
    ]
    hosts = r"C:\Windows\System32\drivers\etc\hosts" if system == "Windows" else "/etc/hosts"
    steps += [
        "",
        "Only for the HTTPS customer view with sign-in (ATLAS_IDENTITY=local), one-time steps:",
        f"   - Add a line for atlas.<org>.internal and scenario.<org>.internal pointing to 127.0.0.1 in {hosts}"
        + (" (needs an administrator account)." if system == "Windows" else " (needs sudo)."),
        "   - Trust the local certificate authority that ATLAS creates: "
        + {"Windows": "certutil -user -addstore Root <path to ca.crt>",
           "Darwin": "open Keychain Access and add <path to ca.crt> to the login keychain as trusted",
           }.get(system, "copy <path to ca.crt> to /usr/local/share/ca-certificates/ and run sudo update-ca-certificates"),
    ]
    return steps


def run_setup(root: Path, *, answers: Mapping[str, str], force: bool = False,
              system: str | None = None, out: Callable[[str], None] = print) -> int:
    template_path = root / ".env.example"
    env_path = root / ".env"
    if not template_path.exists():
        out(f"Cannot find {template_path.name} in {root}. Run this command from the ATLAS folder.")
        return 2
    if env_path.exists() and not force:
        out(f"{env_path.name} already exists, so it was left unchanged. Use --force to replace it.")
    else:
        text = fill_env(template_path.read_text(encoding="utf-8"), {k: v for k, v in answers.items() if v is not None})
        env_path.write_text(text, encoding="utf-8")
        out(f"Wrote {env_path.name}. It is ignored by git; keep it private.")
    data_dir = Path(answers.get("ATLAS_DATA_DIR") or "data")
    data_dir = data_dir if data_dir.is_absolute() else root / data_dir
    (data_dir / "local").mkdir(parents=True, exist_ok=True)
    out(f"Data folder: {data_dir}")
    out("")
    out("Next steps:")
    for line in next_steps(system or platform.system(), answers.get("ATLAS_PORT") or "8000"):
        out(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m atlas.setup", description="Set up a new copy of ATLAS.")
    parser.add_argument("--root", default=".", help="the ATLAS folder (default: current folder)")
    parser.add_argument("--non-interactive", action="store_true", help="do not ask questions; use the flags given")
    parser.add_argument("--force", action="store_true", help="replace an existing .env")
    parser.add_argument("--org", dest="ATLAS_ORG_SLUG")
    parser.add_argument("--project", dest="ATLAS_DEFAULT_PROJECT")
    parser.add_argument("--publish-target", dest="ATLAS_PUBLISH_TARGET")
    parser.add_argument("--port", dest="ATLAS_PORT")
    parser.add_argument("--data-dir", dest="ATLAS_DATA_DIR")
    args = parser.parse_args(argv)
    answers: dict[str, str] = {}
    for key, question, default in ASKED:
        given = getattr(args, key, None)
        if given is not None:
            answers[key] = given
        elif args.non_interactive:
            answers[key] = default
        else:
            reply = input(f"{question} [{default}]: ").strip()
            answers[key] = reply or default
    if not args.non_interactive:
        for key, question in SECRETS:
            secret = getpass.getpass(f"{question}: ").strip()
            if secret:
                answers[key] = secret
    return run_setup(Path(args.root).resolve(), answers=answers, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
