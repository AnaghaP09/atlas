"""Small, careful edits to a ``.env`` file. Existing values are kept unless told otherwise."""

from __future__ import annotations

import shutil
from pathlib import Path


def ensure_env_file(env_path: Path, example: Path) -> bool:
    """Create ``.env`` from ``.env.example`` if it does not exist. Returns True when created."""
    if env_path.exists():
        return False
    if example.is_file():
        shutil.copyfile(example, env_path)
    else:
        env_path.write_text("", encoding="utf-8")
    return True


def read_env(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def set_env_values(env_path: Path, updates: dict[str, str], *, overwrite: bool = False) -> list[str]:
    """Set keys in place. Empty or missing keys are filled; set keys change only with overwrite."""
    lines = env_path.read_text(encoding="utf-8-sig").splitlines() if env_path.is_file() else []
    changed: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in updates:
            seen.add(key)
            if overwrite or value.strip() == "":
                lines[i] = f"{key}={updates[key]}"
                changed.append(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
            changed.append(key)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed
