"""Pinned sandbox images and the local image cache.

The sandbox never pulls an image at run time. Each image is pinned by its registry manifest
digest. ``python -m prep.images.fetch_images`` pulls it once by that digest, verifies the pulled
reference carries the digest (``RepoDigests``), records the local image id (which is not the same
as the manifest digest on every image store) and saves the image to a tar whose SHA256 is
recorded. At run time the sandbox uses the recorded image id: if the engine holds it, it is used;
otherwise the tar is checked against its recorded SHA256, loaded, and the loaded id must equal the
recorded id. Anything else is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

PREP_COMMAND = "python -m prep.images.fetch_images"
LOADED_RE = re.compile(r"Loaded image(?: ID)?:\s*(\S+)")


@dataclass(frozen=True)
class PinnedImage:
    name: str
    repository: str
    tag: str
    manifest_digest: str  # registry manifest digest (linux/amd64)

    @property
    def pull_ref(self) -> str:
        return f"{self.repository}@{self.manifest_digest}"

    def matches_repo_digest(self, repo_digests: Sequence[str]) -> bool:
        names = {self.repository, f"docker.io/library/{self.repository}", f"library/{self.repository}"}
        return any(d.rsplit("@", 1)[-1] == self.manifest_digest and d.rsplit("@", 1)[0] in names for d in repo_digests)


IMAGES: dict[str, PinnedImage] = {
    "go": PinnedImage("go", "golang", "1.27.4-alpine",
                      "sha256:ea30e25680a5470bac02b08bb0e0a735fb75bd0ab7d431b9b95ee0abef362586"),
    "alpine": PinnedImage("alpine", "alpine", "3.20",
                          "sha256:c64c687cbea9300178b30c95835354e34c4e4febc4badfe27102879de0483b5e"),
}


class ImageMissing(RuntimeError):
    """The pinned image is neither loaded nor cached; the message names the prep command."""


class DigestMismatch(RuntimeError):
    """A cached or loaded image does not match what was recorded for its pinned digest."""


def default_cache_dir() -> Path:
    return Path(os.environ.get("ATLAS_DATA_DIR", "data")) / "local" / "images"


def docker_binary() -> str:
    found = shutil.which("docker")
    if found:
        return found
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe"
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError("docker is not installed or not on PATH")


def docker_env() -> dict[str, str]:
    """Environment for docker subprocesses: the engine's helper binaries sit next to docker."""
    env = dict(os.environ)
    try:
        env["PATH"] = str(Path(docker_binary()).parent) + os.pathsep + env.get("PATH", "")
    except FileNotFoundError:
        pass
    return env


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def default_runner(args: Sequence[str], timeout: float = 600) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([docker_binary(), *args], capture_output=True, text=True, timeout=timeout, env=docker_env())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class Inspected:
    image_id: str
    repo_digests: tuple[str, ...]


class ImageCache:
    """Makes pinned images available to the sandbox without ever contacting a registry."""

    def __init__(self, cache_dir: Path | None = None, runner: Runner = default_runner) -> None:
        self.cache_dir = cache_dir or default_cache_dir()
        self.run = runner

    @property
    def manifest_path(self) -> Path:
        return self.cache_dir / "manifest.json"

    def manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def tar_path(self, image: PinnedImage) -> Path:
        return self.cache_dir / f"{image.name}.tar"

    def inspect(self, reference: str) -> Inspected | None:
        result = self.run(["image", "inspect", "--format", "{{json .Id}} {{json .RepoDigests}}", reference])
        if result.returncode != 0 or not result.stdout.strip():
            return None
        id_part, _, digests_part = result.stdout.strip().partition(" ")
        try:
            image_id = json.loads(id_part)
            digests = tuple(json.loads(digests_part) or ())
        except json.JSONDecodeError:
            return None
        return Inspected(image_id, digests)

    def ensure(self, name: str) -> str:
        """Return the local image id to run for a pinned image, loading it from the cache if
        needed. Never pulls."""
        image = IMAGES[name]
        recorded = self.manifest().get(name)
        if recorded and recorded.get("manifest_digest") != image.manifest_digest:
            raise DigestMismatch(f"{name}: cache was prepared for another digest; re-run `{PREP_COMMAND}`")
        expected_id = recorded.get("image_id") if recorded else None

        if expected_id:
            held = self.inspect(expected_id)
            if held is not None:
                if held.image_id != expected_id:
                    raise DigestMismatch(f"{name}: engine reports {held.image_id} for {expected_id}")
                return expected_id
        else:
            held = self.inspect(image.pull_ref)
            if held is not None:
                if not image.matches_repo_digest(held.repo_digests):
                    raise DigestMismatch(f"{name}: engine image does not carry the pinned digest")
                return held.image_id

        tar = self.tar_path(image)
        if not recorded or not tar.exists():
            raise ImageMissing(
                f"sandbox image '{name}' ({image.repository}:{image.tag}, {image.manifest_digest}) is not available "
                f"locally; run `{PREP_COMMAND}` once while connected, then retry"
            )
        if recorded.get("tar_sha256") != sha256_file(tar):
            raise DigestMismatch(f"{name}: cached tar does not match its recorded checksum; re-run `{PREP_COMMAND}`")
        result = self.run(["load", "-i", str(tar)])
        if result.returncode != 0:
            raise DigestMismatch(f"{name}: loading the cached image failed: {result.stderr.strip()[:200]}")
        held = self.inspect(expected_id)
        if held is None or held.image_id != expected_id:
            raise DigestMismatch(f"{name}: loaded image does not have the recorded id {expected_id}")
        return expected_id
