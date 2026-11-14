"""Pinned sandbox images and the local image cache.

The sandbox never pulls an image at run time. Images are referenced only by their pinned digest,
fetched ahead of time by ``python -m prep.images.fetch_images`` into a local tar cache, and loaded
from that cache when the container engine does not already hold them. A cached tar whose checksum
or loaded image id differs from the pinned digest is refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

PREP_COMMAND = "python -m prep.images.fetch_images"


@dataclass(frozen=True)
class PinnedImage:
    name: str
    repository: str
    tag: str
    digest: str  # linux/amd64 manifest digest; equals the image id in the container engine

    @property
    def pull_ref(self) -> str:
        return f"{self.repository}@{self.digest}"


IMAGES: dict[str, PinnedImage] = {
    "go": PinnedImage("go", "golang", "1.27.4-alpine",
                      "sha256:ea30e25680a5470bac02b08bb0e0a735fb75bd0ab7d431b9b95ee0abef362586"),
    "alpine": PinnedImage("alpine", "alpine", "3.20",
                          "sha256:c64c687cbea9300178b30c95835354e34c4e4febc4badfe27102879de0483b5e"),
}


class ImageMissing(RuntimeError):
    """The pinned image is neither loaded nor cached; the message names the prep command."""


class DigestMismatch(RuntimeError):
    """A cached or loaded image does not match its pinned digest."""


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
        bin_dir = str(Path(docker_binary()).parent)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    except FileNotFoundError:
        pass
    return env


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(args: Sequence[str], timeout: float = 600) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([docker_binary(), *args], capture_output=True, text=True, timeout=timeout, env=docker_env())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


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

    def loaded_id(self, digest: str) -> str | None:
        result = self.run(["image", "inspect", "--format", "{{.Id}}", digest])
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def ensure(self, name: str) -> str:
        """Return the run reference (the pinned digest) for an image, loading it from the cache
        if needed. Never pulls."""
        image = IMAGES[name]
        loaded = self.loaded_id(image.digest)
        if loaded is not None:
            if loaded != image.digest:
                raise DigestMismatch(f"{name}: engine holds {loaded}, pinned {image.digest}")
            return image.digest
        tar = self.tar_path(image)
        if not tar.exists():
            raise ImageMissing(
                f"sandbox image '{name}' ({image.repository}:{image.tag}, {image.digest}) is not available "
                f"locally; run `{PREP_COMMAND}` once while connected, then retry"
            )
        recorded = self.manifest().get(name, {})
        if recorded.get("digest") != image.digest or recorded.get("tar_sha256") != sha256_file(tar):
            raise DigestMismatch(f"{name}: cached tar does not match the pinned digest; re-run `{PREP_COMMAND}`")
        result = self.run(["load", "-i", str(tar)])
        if result.returncode != 0:
            raise DigestMismatch(f"{name}: loading the cached image failed: {result.stderr.strip()[:200]}")
        loaded = self.loaded_id(image.digest)
        if loaded != image.digest:
            raise DigestMismatch(f"{name}: loaded image id {loaded} does not match pinned {image.digest}")
        return image.digest
