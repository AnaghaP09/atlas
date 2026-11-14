"""The one-time steps a person does by hand, per operating system.

ATLAS never edits the hosts file or the certificate store itself. These two steps need an
administrator account on most systems, so setup prints them instead.
"""

from __future__ import annotations

import platform
from pathlib import Path


def one_time_steps(host_names: list[str], public_ca: Path, system: str | None = None) -> str:
    system = (system or platform.system()).lower()
    line = "127.0.0.1 " + " ".join(host_names)
    ca = str(public_ca.resolve())
    if system == "windows":
        hosts_step = (
            "1. Add the host names (needs an administrator account).\n"
            "   Open Notepad as administrator, open C:\\Windows\\System32\\drivers\\etc\\hosts,\n"
            f"   add this line at the end and save:\n\n       {line}\n"
        )
        trust_step = (
            "2. Trust the local certificate authority for your own user (no administrator needed).\n"
            "   In PowerShell:\n\n"
            f'       certutil -user -addstore Root "{ca}"\n\n'
            "   Accept the security prompt. Then restart your browser.\n"
        )
    elif system == "darwin":
        hosts_step = (
            "1. Add the host names (needs an administrator password).\n\n"
            f"       echo '{line}' | sudo tee -a /etc/hosts\n"
        )
        trust_step = (
            "2. Trust the local certificate authority for your user:\n\n"
            f'       security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db "{ca}"\n\n'
            "   Then restart your browser.\n"
        )
    else:
        hosts_step = (
            "1. Add the host names (needs root):\n\n"
            f"       echo '{line}' | sudo tee -a /etc/hosts\n"
        )
        trust_step = (
            "2. Trust the local certificate authority. Either for the whole system:\n\n"
            f'       sudo cp "{ca}" /usr/local/share/ca-certificates/atlas-local-ca.crt\n'
            "       sudo update-ca-certificates\n\n"
            "   or import the file into your browser's certificate settings as an authority.\n"
            "   Then restart your browser.\n"
        )
    return (
        "One-time steps (do these yourself, once per machine):\n\n"
        + hosts_step + "\n" + trust_step
        + "\nATLAS never changes these settings for you.\n"
    )
