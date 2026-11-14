import json

import pytest

from atlas.contracts import RunMode
from atlas.snapshots import SnapshotError, SnapshotRegistry, UnsafePath, resolve_under
from atlas.store import LIVE_SNAPSHOT_ID, EvidenceDB, ReadOnlyStore, file_sha256
from tests.a.helpers import T0, ctx, rec


@pytest.fixture
def registry(tmp_path):
    reg = SnapshotRegistry(tmp_path / "data")
    yield reg
    reg.close()


def test_freeze_isolates_later_live_writes(registry):
    store = registry.store()
    store.put(rec(version="v1", revision=1), "connector.issues")
    info = registry.freeze(frozen_at=T0)
    store.put(rec(version="v2", revision=2), "connector.issues")
    frozen = ctx(info.snapshot_id)
    assert store.get(frozen, "issues", "issue/1").content_version_id == "v1"
    live = ctx(LIVE_SNAPSHOT_ID, mode=RunMode.LIVE)
    assert store.get(live, "issues", "issue/1").content_version_id == "v2"
    assert file_sha256(info.path) == info.sha256


def test_frozen_snapshot_is_read_only(registry):
    registry.store().put(rec(), "connector.issues")
    info = registry.freeze(frozen_at=T0)
    db = registry.open(ctx(info.snapshot_id))
    with pytest.raises(ReadOnlyStore):
        db.put(rec(version="x"), "connector.issues")


def test_replay_cannot_read_live_store(registry):
    with pytest.raises(SnapshotError):
        registry.open(ctx(LIVE_SNAPSHOT_ID))


def test_invalid_and_unknown_snapshot_ids(registry):
    with pytest.raises(SnapshotError):
        registry.open(ctx("../etc"))
    with pytest.raises(SnapshotError):
        registry.open(ctx("does-not-exist"))


def _write_export(root, snapshot_id, rows, kind="cases"):
    d = root / "snapshots" / kind / snapshot_id
    d.mkdir(parents=True)
    lines = [json.dumps(r, sort_keys=True) + "\n" for r in rows]
    (d / "records.jsonl").write_text("".join(lines), encoding="utf-8")
    sha = file_sha256(d / "records.jsonl")
    (d / "snapshot.json").write_text(json.dumps({"snapshot_id": snapshot_id, "cutoff": T0.isoformat(), "records_sha256": sha}), encoding="utf-8")
    return d


def test_prepared_snapshot_builds_from_export_and_detects_tamper(tmp_path):
    source = EvidenceDB(tmp_path / "src.sqlite")
    source.put(rec(), "connector.issues", content={"title": "t"})
    rows = source.export_rows()
    source.close()
    data = tmp_path / "data"
    export = _write_export(data, "case-g01", rows)
    reg = SnapshotRegistry(data)
    r = reg.store().get(ctx("case-g01"), "issues", "issue/1")
    assert r is not None
    reg.close()
    # tamper with the export: build refuses
    (export / "records.jsonl").write_text("{}\n", encoding="utf-8")
    reg2 = SnapshotRegistry(data)
    with pytest.raises(SnapshotError):
        reg2.open(ctx("case-g01"))
    reg2.close()


def test_two_snapshots_never_mix(tmp_path):
    data = tmp_path / "data"
    for sid, title in (("case-a", "A"), ("case-b", "B")):
        src = EvidenceDB(tmp_path / f"{sid}.sqlite")
        src.put(rec(), "connector.issues", content={"title": title})
        _write_export(data, sid, src.export_rows())
        src.close()
    reg = SnapshotRegistry(data)
    store = reg.store()
    for _ in range(2):
        for sid, title in (("case-a", "A"), ("case-b", "B")):
            c = ctx(sid)
            assert store.content(c, store.get(c, "issues", "issue/1").record_id)["title"] == title
    reg.close()


@pytest.mark.parametrize(
    "ref",
    [
        "file:../evaluator/alias_map.json",
        "file:cases/../../evaluator/x",
        "file:/etc/passwd",
        "file:C:/Windows/win.ini",
        "file:cases\\..\\..\\evaluator",
        "file:cases/%2e%2e/evaluator",
        "file:cases/%2Fetc",
        "file:~/x",
        "file:",
        "",
        "file:./cases/x",
    ],
)
def test_resolver_rejects_traversal(tmp_path, ref):
    root = tmp_path / "snapshots"
    (root / "cases").mkdir(parents=True)
    with pytest.raises(UnsafePath):
        resolve_under(root, ref)


def test_resolver_rejects_symlink(tmp_path):
    root = tmp_path / "snapshots"
    (root / "cases").mkdir(parents=True)
    outside = tmp_path / "evaluator"
    outside.mkdir()
    link = root / "cases" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this system")
    with pytest.raises(UnsafePath):
        resolve_under(root, "file:cases/link/x")


def test_resolver_accepts_normal_path(tmp_path):
    root = tmp_path / "snapshots"
    (root / "cases" / "g01").mkdir(parents=True)
    (root / "cases" / "g01" / "log.txt").write_text("x")
    assert resolve_under(root, "file:cases/g01/log.txt").read_text() == "x"
