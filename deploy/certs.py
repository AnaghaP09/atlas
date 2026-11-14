"""Local certificates for the HTTPS site, made with OpenSSL.

- A local certificate authority (CA) is created once. Its private key stays in the private data
  directory and never leaves this machine.
- One server certificate covers both host names (main and scenario) and 127.0.0.1.
- Only the CA's public certificate is exported, so you can trust it once in your browser's store.

OpenSSL is found from ``ATLAS_OPENSSL_BIN``, then on PATH, then next to Git (Git for Windows ships
OpenSSL).
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

CA_DAYS = 1825
SERVER_DAYS = 397


class CertificateError(RuntimeError):
    pass


def find_openssl(configured: str | Path | None = None) -> Path | None:
    if configured:
        path = Path(configured)
        return path if path.is_file() else None
    found = shutil.which("openssl")
    if found:
        return Path(found)
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parent.parent  # <git>/cmd/git.exe or <git>/bin/git
        for candidate in ("usr/bin/openssl", "usr/bin/openssl.exe", "mingw64/bin/openssl.exe", "bin/openssl"):
            path = root / candidate
            if path.is_file():
                return path
    return None


def _run(openssl: Path, *args: str, cwd: Path) -> str:
    env = dict(os.environ)
    env["MSYS_NO_PATHCONV"] = "1"  # keep "/CN=..." as written when OpenSSL comes from Git for Windows
    env["MSYS2_ARG_CONV_EXCL"] = "*"
    result = subprocess.run([str(openssl), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise CertificateError(f"openssl {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def _private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@dataclass(frozen=True)
class CertPaths:
    ca_dir: Path
    tls_dir: Path

    @property
    def ca_key(self) -> Path:
        return self.ca_dir / "ca.key"

    @property
    def ca_cert(self) -> Path:
        return self.ca_dir / "ca.crt"

    @property
    def public_ca(self) -> Path:
        return self.tls_dir / "atlas-local-ca.crt"

    @property
    def server_key(self) -> Path:
        return self.tls_dir / "server.key"

    @property
    def server_cert(self) -> Path:
        return self.tls_dir / "server.crt"

    @classmethod
    def under(cls, data_dir: Path) -> "CertPaths":
        return cls(data_dir / "local" / "ca", data_dir / "local" / "tls")


def ensure_ca(paths: CertPaths, openssl: Path, label: str) -> bool:
    """Create the local CA if it does not exist yet. Returns True when it was created."""
    if paths.ca_key.is_file() and paths.ca_cert.is_file():
        return False
    paths.ca_dir.mkdir(parents=True, exist_ok=True)
    _run(openssl, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
         "-out", paths.ca_key.name, cwd=paths.ca_dir)
    _private(paths.ca_key)
    _run(openssl, "req", "-x509", "-new", "-key", paths.ca_key.name, "-sha256", "-days", str(CA_DAYS),
         "-subj", f"/CN=ATLAS local CA ({label})",
         "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign",
         "-out", paths.ca_cert.name, cwd=paths.ca_dir)
    return True


def ensure_server_cert(paths: CertPaths, openssl: Path, host_names: list[str], *, renew: bool = False) -> bool:
    """Create the server certificate for the given host names. Returns True when it was created."""
    if paths.server_cert.is_file() and paths.server_key.is_file() and not renew:
        return False
    paths.tls_dir.mkdir(parents=True, exist_ok=True)
    work = paths.tls_dir
    _run(openssl, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
         "-out", paths.server_key.name, cwd=work)
    _private(paths.server_key)
    csr = work / "server.csr"
    ext = work / "server.ext"
    san = ",".join([f"DNS:{h}" for h in host_names] + ["IP:127.0.0.1"])
    ext.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature\n"
        "extendedKeyUsage=serverAuth\n"
        f"subjectAltName={san}\n",
        encoding="utf-8",
    )
    try:
        _run(openssl, "req", "-new", "-key", paths.server_key.name, "-subj", f"/CN={host_names[0]}",
             "-out", csr.name, cwd=work)
        _run(openssl, "x509", "-req", "-in", csr.name, "-CA", str(paths.ca_cert.resolve()),
             "-CAkey", str(paths.ca_key.resolve()), "-set_serial", str(secrets.randbits(63)),
             "-days", str(SERVER_DAYS), "-sha256", "-extfile", ext.name, "-out", paths.server_cert.name, cwd=work)
    finally:
        csr.unlink(missing_ok=True)
        ext.unlink(missing_ok=True)
    shutil.copyfile(paths.ca_cert, paths.public_ca)  # public part only
    return True


def fingerprint_sha1(openssl: Path, cert: Path) -> str:
    out = _run(openssl, "x509", "-noout", "-fingerprint", "-sha1", "-in", str(cert.resolve()), cwd=cert.parent)
    return out.strip().split("=", 1)[-1].replace(":", "").upper()


def describe(openssl: Path, cert: Path) -> str:
    return _run(openssl, "x509", "-noout", "-text", "-in", str(cert.resolve()), cwd=cert.parent)
