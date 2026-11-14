"""Fetch the pinned sandbox images once and save them to the local image cache.

Run while connected: ``python -m prep.images.fetch_images``. Each image is pulled by its manifest
digest with retries and back-off (some local security software intercepts registry traffic and
makes the first attempt fail). The pulled reference must carry the pinned digest in its
``RepoDigests``; its local image id (not always equal to the manifest digest) and the SHA256 of the
saved tar are recorded. The sandbox later loads from this cache by image id and never pulls.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from atlas.execution.images import IMAGES, ImageCache, PinnedImage, Runner, default_runner, sha256_file


class FetchError(RuntimeError):
    pass


def pull_with_retries(image: PinnedImage, run: Runner, attempts: int = 5,
                      sleep: Callable[[float], None] = time.sleep) -> None:
    delay = 2.0
    last = ""
    for attempt in range(1, attempts + 1):
        result = run(["pull", "-q", image.pull_ref])
        if result.returncode == 0:
            return
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        last = lines[-1] if lines else ""
        if attempt < attempts:
            sleep(delay)
            delay *= 2
    raise FetchError(f"could not pull {image.pull_ref} after {attempts} attempts: {last}")


def fetch(names: Sequence[str], cache: ImageCache, sleep: Callable[[float], None] = time.sleep) -> dict:
    cache.cache_dir.mkdir(parents=True, exist_ok=True)
    manifest = cache.manifest()
    for name in names:
        image = IMAGES[name]
        held = cache.inspect(image.pull_ref)
        if held is None or not image.matches_repo_digest(held.repo_digests):
            pull_with_retries(image, cache.run, sleep=sleep)
            held = cache.inspect(image.pull_ref)
        if held is None or not image.matches_repo_digest(held.repo_digests):
            raise FetchError(f"{name}: pulled image does not carry the pinned manifest digest {image.manifest_digest}")
        tar = cache.tar_path(image)
        result = cache.run(["save", "-o", str(tar), image.pull_ref])
        if result.returncode != 0:
            raise FetchError(f"{name}: save failed: {result.stderr.strip()[:200]}")
        manifest[name] = {
            "repository": image.repository,
            "tag": image.tag,
            "manifest_digest": image.manifest_digest,
            "image_id": held.image_id,
            "tar_sha256": sha256_file(tar),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        cache.manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        print(f"{name}: cached {image.repository}:{image.tag} manifest {image.manifest_digest} image id {held.image_id}")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", default=sorted(IMAGES), help="images to fetch (default: all)")
    parser.add_argument("--cache-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        fetch(args.names, ImageCache(args.cache_dir, default_runner))
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
