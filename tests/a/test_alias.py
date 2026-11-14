import json

from atlas.snapshots import SnapshotRegistry
from prep.alias.alias import AliasMap, alias_export, inventory, originals_present, transform
from tests.a.helpers import ctx

SHA = "9e58ac0fa2" + "1" * 30
TEXT = (
    "Upgrade test [test_id:3145] fails since #17540 merged (commit 9e58ac0fa2, full " + SHA + "). "
    "See https://prow.ci.kubevirt.io/view/gs/kubevirt-prow/pr-logs/pull/kubevirt_kubevirt/18355/"
    "pull-kubevirt-e2e-k8s-1.36-sig-operator/2074418471822692352 and CNV-89396. "
    "Touches pkg/virt-operator and tests/upgrade. Reported by @person-0a1b2c3d. KubeVirt issue 18465."
)


def test_transform_removes_every_inventory_item_and_is_consistent():
    amap = AliasMap(seed=7)
    inv = inventory([TEXT])
    assert {"17540", "18355", "18465"} <= inv["number"]
    assert inv["sha"] == {SHA} and "2074418471822692352" in inv["build_id"]
    out = transform(TEXT, amap)
    leftovers = originals_present(out, inv)
    assert leftovers == {}, leftovers
    assert transform(TEXT, amap) == out  # deterministic for the same map
    alias_sha = amap.maps["sha"][SHA]
    assert alias_sha[:10] in out  # abbreviation keeps its prefix relation


def test_alias_map_is_a_bijection_and_round_trips():
    amap = AliasMap(seed=3)
    for n in range(10000, 10500):
        amap.alias("number", str(n))
    forward = amap.maps["number"]
    assert len(set(forward.values())) == len(forward)
    reverse = amap.reverse("number")
    assert all(reverse[forward[k]] == k for k in forward)
    restored = AliasMap.from_json(json.loads(json.dumps(amap.to_json())))
    assert restored.maps == amap.maps


def test_independent_reextraction_finds_no_originals():
    amap = AliasMap(seed=11)
    out = transform(TEXT, amap)
    again = inventory([out])
    original = inventory([TEXT])
    for kind in ("number", "sha", "build_id", "job", "ticket", "component", "person"):
        assert not (again[kind] & original[kind]), kind
    assert "kubevirt" not in out.lower()


def test_aliased_export_builds_and_resolves(tmp_path):
    from atlas.store import EvidenceDB
    from tests.a.helpers import rec

    data = tmp_path / "data"
    src_dir = data / "snapshots" / "cases" / "case-x"
    src_dir.mkdir(parents=True)
    db = EvidenceDB(tmp_path / "s.sqlite")
    r = rec("issues", "issue/18465", "v1", record_id="issues:issue/18465@abc")
    db.put(r, "connector.issues", content={"number": 18465, "body": TEXT, "url": "https://github.com/kubevirt/kubevirt/issues/18465"})
    rows = db.export_rows()
    db.close()
    (src_dir / "records.jsonl").write_bytes("".join(json.dumps(x, sort_keys=True) + "\n" for x in rows).encode())
    (src_dir / "conflicts.jsonl").write_bytes(b"")
    (src_dir / "snapshot.json").write_text(json.dumps({"cutoff": "2026-07-15T10:05:44+00:00"}))
    amap = AliasMap(seed=5)
    target, leftovers = alias_export(src_dir, data / "snapshots" / "cases-aliased", amap)
    assert leftovers == {}
    text = (target / "records.jsonl").read_text(encoding="utf-8")
    assert "18465" not in text and "github.com" not in text and "kubevirt" not in text.lower()
    reg = SnapshotRegistry(data)
    aliased_number = amap.maps["number"]["18465"]
    record = reg.store().get(ctx("case-x-aliased"), "issues", f"issue/{aliased_number}")
    assert record is not None
    # Evaluator round trip restores the structured reference exactly.
    assert amap.reverse("number")[record.source_id.split("/")[1]] == "18465"
    reg.close()
