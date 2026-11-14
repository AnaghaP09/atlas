"""Aliasing for the contamination-control variant of every snapshot.

An identifier inventory is extracted from the ordinary export with typed extractors (repository
and organisation names, issue and pull-request numbers, commit hashes, CI job names, build ids,
test ids, component paths, external ticket keys, hosts, people). One bijective map replaces every
occurrence in every representation: content text, source ids, record ids, content references,
lineage and file names. The map is written only to the evaluator zone; the aliased export carries
no original identifier and no original URL.

Usage: python -m prep.alias.alias   (aliases every ordinary export under data/local/export)
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from atlas.snapshots import normalized_sha256
from prep.reference import paths, reference

EXPORT = paths()["export"]

# Typed extractors. Order of application in the transform: longest and most specific first.
RE_URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
RE_ISSUE_NUM = re.compile(r"(?:#|/issues/|/pull/|/pulls/|issue/|pr/|\"number\": |PR |pull request |issue )(\d{3,6})\b")
RE_PROW_PR = re.compile(r"/pull/[A-Za-z0-9_]+/(\d{3,6})/")
RE_SHA = re.compile(r"\b[0-9a-f]{40}\b")
RE_BUILD_ID = re.compile(r"\b\d{17,20}\b")
RE_JOB = reference().job_pattern or re.compile(r"(?!)")
RE_TEST_ID = re.compile(r"\[(?:test_id|rfe_id):(\d+)\]")
RE_TICKET = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{2,7}\b")
RE_COMPONENT = re.compile(r"\b(?:pkg|cmd|tests|staging)/[A-Za-z0-9_.-]+")
RE_PERSON = re.compile(r"\bperson-[0-9a-f]{8}\b")
PROJECT_WORDS = reference().alias_words
KNOWN_TICKET_PREFIXES = reference().ticket_prefixes


@dataclass
class AliasMap:
    """Bijective maps per identifier type."""

    seed: int = 0
    maps: dict[str, dict[str, str]] = field(default_factory=dict)

    def _rng(self, kind: str, original: str) -> random.Random:
        h = hashlib.sha256(f"{self.seed}|{kind}|{original}".encode()).hexdigest()
        return random.Random(int(h[:16], 16))

    def alias(self, kind: str, original: str) -> str:
        table = self.maps.setdefault(kind, {})
        if original in table:
            return table[original]
        taken = set(table.values())
        rng = self._rng(kind, original)
        for _ in range(1000):
            candidate = self._make(kind, original, rng)
            if candidate not in taken and candidate != original:
                table[original] = candidate
                return candidate
        raise RuntimeError(f"could not allocate an alias for {kind}")

    @staticmethod
    def _make(kind: str, original: str, rng: random.Random) -> str:
        if kind == "number":
            return str(rng.randint(30000, 99999))
        if kind == "sha":
            return "".join(rng.choice("0123456789abcdef") for _ in range(40))
        if kind == "build_id":
            return str(rng.randint(10 ** (len(original) - 1), 10 ** len(original) - 1))
        if kind == "job":
            return f"job-{rng.randint(1000, 9999)}"
        if kind == "test_id":
            return str(rng.randint(90000, 99999))
        if kind == "ticket":
            return f"TICKET-{rng.randint(100000, 999999)}"
        if kind == "component":
            top = original.split("/", 1)[0]
            return f"{top}/component-{rng.randint(100, 999)}"
        if kind == "host":
            return f"host-{rng.randint(100, 999)}.example"
        if kind == "person":
            return "person-" + "".join(rng.choice("0123456789abcdef") for _ in range(8))
        raise ValueError(kind)

    def reverse(self, kind: str) -> dict[str, str]:
        return {v: k for k, v in self.maps.get(kind, {}).items()}

    def to_json(self) -> dict[str, Any]:
        return {"seed": self.seed, "maps": {k: dict(sorted(v.items())) for k, v in sorted(self.maps.items())}}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "AliasMap":
        return cls(seed=data["seed"], maps={k: dict(v) for k, v in data["maps"].items()})


def inventory(texts: Iterable[str]) -> dict[str, set[str]]:
    """Independent typed extraction of identifiers from text."""
    inv: dict[str, set[str]] = {k: set() for k in ("host", "number", "sha", "build_id", "job", "test_id", "ticket", "component", "person", "project")}
    for text in texts:
        inv["host"].update(m.group(1).lower() for m in RE_URL_HOST.finditer(text))
        inv["number"].update(RE_ISSUE_NUM.findall(text))
        inv["number"].update(RE_PROW_PR.findall(text))
        inv["sha"].update(RE_SHA.findall(text))
        inv["build_id"].update(RE_BUILD_ID.findall(text))
        inv["job"].update(RE_JOB.findall(text))
        inv["test_id"].update(RE_TEST_ID.findall(text))
        inv["ticket"].update(t for t in RE_TICKET.findall(text) if t.startswith(KNOWN_TICKET_PREFIXES))
        inv["component"].update(RE_COMPONENT.findall(text))
        inv["person"].update(RE_PERSON.findall(text))
        for word, _ in PROJECT_WORDS:
            if word in text:
                inv["project"].add(word)
    return inv


def prepare(amap: AliasMap, inv: dict[str, set[str]]) -> None:
    """Allocate aliases for every inventory item before any text is transformed."""
    for kind in ("host", "number", "sha", "build_id", "job", "test_id", "ticket", "component", "person"):
        for item in sorted(inv.get(kind, ())):
            amap.alias(kind, item)


def transform(text: str, amap: AliasMap) -> str:
    """Apply one consistent transformation to a piece of text."""
    prepare(amap, inventory([text]))
    text = RE_URL_HOST.sub(lambda m: m.group(0).replace(m.group(1), amap.alias("host", m.group(1).lower())), text)
    hosts = amap.maps.get("host", {})
    if hosts:
        bare = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(h) for h in sorted(hosts, key=len, reverse=True)) + r")(?![\w-])", re.I)
        text = bare.sub(lambda m: hosts[m.group(1).lower()], text)
    shas = amap.maps.get("sha", {})
    text = RE_SHA.sub(lambda m: amap.alias("sha", m.group(0)), text)
    if shas:
        by_prefix = {o[:7]: a for o, a in shas.items()}
        pattern = re.compile(r"\b(" + "|".join(sorted(by_prefix)) + r")[0-9a-f]{0,33}\b")
        text = pattern.sub(lambda m: by_prefix[m.group(1)][: len(m.group(0))], text)
    text = RE_BUILD_ID.sub(lambda m: amap.alias("build_id", m.group(0)), text)
    text = RE_JOB.sub(lambda m: amap.alias("job", m.group(0)), text)
    text = RE_TEST_ID.sub(lambda m: m.group(0).replace(m.group(1), amap.alias("test_id", m.group(1))), text)
    text = RE_TICKET.sub(lambda m: amap.alias("ticket", m.group(0)) if m.group(0).startswith(KNOWN_TICKET_PREFIXES) else m.group(0), text)
    text = RE_COMPONENT.sub(lambda m: amap.alias("component", m.group(0)), text)
    text = RE_PERSON.sub(lambda m: amap.alias("person", m.group(0)), text)
    known = amap.maps.setdefault("number", {})
    if known:
        pattern = re.compile(r"(?<![\w.-])(" + "|".join(sorted(known, key=len, reverse=True)) + r")(?![\w-])")
        text = pattern.sub(lambda m: known[m.group(1)], text)
    for word, replacement in PROJECT_WORDS:
        text = text.replace(word, replacement)
    return text


def alias_rows(rows: list[dict[str, Any]], amap: AliasMap) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        blob = json.dumps(row, sort_keys=True, ensure_ascii=False)
        out.append(json.loads(transform(blob, amap)))
    return out


def originals_present(text: str, original_inv: dict[str, set[str]]) -> dict[str, set[str]]:
    """Original identifiers still present in ``text`` (completeness check on final output)."""
    found: dict[str, set[str]] = {}
    for kind, items in original_inv.items():
        for item in items:
            if kind == "number":
                hit = re.search(r"(?<![\w.-])" + re.escape(item) + r"(?![\w-])", text)
            elif kind == "test_id":
                hit = re.search(r"\[(?:test_id|rfe_id):" + re.escape(item) + r"\]", text)
            elif kind == "project":
                hit = re.search(re.escape(item), text)
            elif kind == "host":
                hit = re.search(r"(?<![\w.-])" + re.escape(item) + r"(?![\w-])", text, re.I)
            else:
                hit = re.search(r"(?<![\w])" + re.escape(item) + r"(?![\w])", text)
            if hit:
                found.setdefault(kind, set()).add(item)
    return found


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def alias_export(source_dir: Path, target_root: Path, amap: AliasMap) -> tuple[Path, dict[str, set[str]]]:
    """Write the aliased variant of one ordinary export. Returns (target, leftovers)."""
    rows = [json.loads(l) for l in (source_dir / "records.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    conflicts = [json.loads(l) for l in (source_dir / "conflicts.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    manifest = json.loads((source_dir / "snapshot.json").read_text(encoding="utf-8"))
    original_text = (source_dir / "records.jsonl").read_text(encoding="utf-8")
    original_inv = inventory([original_text])
    prepare(amap, original_inv)
    aliased_text = transform(original_text, amap)
    aliased_rows = [json.loads(l) for l in aliased_text.splitlines() if l.strip()]
    aliased_conflicts = alias_rows(conflicts, amap)
    snapshot_id = f"{source_dir.name}-aliased"
    target = target_root / snapshot_id
    target.mkdir(parents=True, exist_ok=True)
    (target / "records.jsonl").write_bytes("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in aliased_rows).encode("utf-8"))
    (target / "conflicts.jsonl").write_bytes("".join(json.dumps(c, sort_keys=True) + "\n" for c in aliased_conflicts).encode("utf-8"))
    aliased_manifest = {
        "snapshot_id": snapshot_id,
        "release_or_case": "aliased",
        "alias_variant": "aliased",
        "cutoff": manifest["cutoff"],
        "counts": manifest.get("counts"),
        "notes": ["Identifiers are aliased; provenance holds opaque references only."],
        "data_modes": manifest.get("data_modes"),
        "records": len(aliased_rows),
        "records_sha256": normalized_sha256(target / "records.jsonl"),
    }
    (target / "snapshot.json").write_bytes((json.dumps(aliased_manifest, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    final_text = (target / "records.jsonl").read_text(encoding="utf-8") + (target / "conflicts.jsonl").read_text(encoding="utf-8")
    final_text += "\n".join(_strings(json.loads((target / "snapshot.json").read_text(encoding="utf-8"))))
    leftovers = originals_present(final_text, original_inv)
    return target, leftovers


def main(argv: list[str]) -> int:
    map_path = EXPORT / "evaluator" / "alias_map.json"
    amap = AliasMap.from_json(json.loads(map_path.read_text(encoding="utf-8"))) if map_path.exists() else AliasMap(seed=random.SystemRandom().randint(1, 2**31))
    problems = 0
    for kind in ("cases", "releases"):
        src_root = EXPORT / "snapshots" / kind
        if not src_root.exists():
            continue
        for src in sorted(p for p in src_root.iterdir() if p.is_dir()):
            target, leftovers = alias_export(src, EXPORT / "snapshots" / f"{kind}-aliased", amap)
            status = "ok" if not leftovers else f"LEFTOVERS {({k: sorted(v)[:5] for k, v in leftovers.items()})}"
            problems += bool(leftovers)
            print(src.name, "->", target.name, status)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    map_path.write_bytes((json.dumps(amap.to_json(), indent=1, sort_keys=True) + "\n").encode("utf-8"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
