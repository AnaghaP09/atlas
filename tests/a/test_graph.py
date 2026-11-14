import json
from pathlib import Path

import pytest

from atlas.graph import (
    RUNTIME_LIMITATION,
    ArchitectureGraph,
    ReleaseGraph,
    architecture_records,
    component_of,
    component_of_import,
    dependency_proof_cases,
    extract,
    parse_imports,
)
from atlas.snapshots import SnapshotRegistry
from atlas.store import AUTHORITATIVE_WRITERS
from tests.a.helpers import T0, ctx

MOD = "kubevirt.io/kubevirt"

GO_FILE = '''//go:build linux
// +build linux

/* import "kubevirt.io/kubevirt/pkg/commented-out" */
package handler

import "fmt"

import (
\t"context"
\tvirtv1 "kubevirt.io/api/core/v1"
\tnet "kubevirt.io/kubevirt/pkg/network/setup"
\t_ "kubevirt.io/kubevirt/pkg/storage/hotplug"
\t// "kubevirt.io/kubevirt/pkg/ignored"
)

func main() { s := "import \\"kubevirt.io/kubevirt/pkg/in-string\\"" ; fmt.Println(s) }
'''


def test_parse_imports_static_only():
    assert parse_imports(GO_FILE) == ["context", "kubevirt.io/api/core/v1", "kubevirt.io/kubevirt/pkg/network/setup",
                                      "kubevirt.io/kubevirt/pkg/storage/hotplug", "fmt"]


@pytest.mark.parametrize(
    "path,component",
    [
        ("pkg/virt-handler/vm.go", "pkg/virt-handler"),
        ("pkg/network/setup/x.go", "pkg/network"),
        ("cmd/virt-launcher/main.go", "cmd/virt-launcher"),
        ("tests/network/vmi_test.go", "tests/network"),
        ("tests/vm_test.go", "tests"),
        ("pkg/util.go", "pkg"),
    ],
)
def test_component_mapping(path, component):
    assert component_of(path) == component


def test_component_of_import_internal_only():
    assert component_of_import("kubevirt.io/kubevirt/pkg/network/setup", MOD) == "pkg/network"
    assert component_of_import("kubevirt.io/api/core/v1", MOD) is None


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


def _go(imports: list[str]) -> str:
    body = "\n".join(f'\t"{i}"' for i in imports)
    return f"package x\n\nimport (\n{body}\n)\n"


def test_edge_direction_follows_imports_with_identical_paths_and_owners(tmp_path):
    owners = "labels:\n- sig/compute\n"
    rev1 = _tree(tmp_path / "r1", {
        "pkg/a/a.go": _go(["kubevirt.io/kubevirt/pkg/b"]),
        "pkg/b/b.go": _go([]),
        "pkg/a/OWNERS": owners,
    })
    rev2 = _tree(tmp_path / "r2", {
        "pkg/a/a.go": _go([]),
        "pkg/b/b.go": _go(["kubevirt.io/kubevirt/pkg/a"]),
        "pkg/a/OWNERS": owners,
    })
    g1, g2 = extract(rev1, "r1", module=MOD), extract(rev2, "r2", module=MOD)
    assert g1.depends_on("pkg/a") == {"pkg/b"} and g1.depends_on("pkg/b") == set()
    assert g2.depends_on("pkg/b") == {"pkg/a"} and g2.depends_on("pkg/a") == set()
    assert g1.components["pkg/a"]["owner_team"] == "sig/compute"


def test_test_imports_and_build_tags(tmp_path):
    root = _tree(tmp_path / "r", {
        "pkg/a/a.go": "//go:build linux\n" + _go(["kubevirt.io/kubevirt/pkg/b"]),
        "pkg/a/a_test.go": _go(["kubevirt.io/kubevirt/pkg/c"]),
        "pkg/b/b.go": _go([]),
        "pkg/c/c.go": _go([]),
        "vendor/x/y.go": _go(["kubevirt.io/kubevirt/pkg/z"]),
    })
    g = extract(root, "r", module=MOD)
    assert ("pkg/a", "pkg/b", "depends_on") in g.edges
    assert ("pkg/a", "pkg/c", "test_depends_on") in g.edges
    assert ("pkg/a", "pkg/c", "depends_on") not in g.edges
    assert all(info["runtime_relationships"] == "unknown" for info in g.components.values())


def test_owner_team_never_uses_handles(tmp_path):
    root = _tree(tmp_path / "r", {"pkg/a/a.go": _go([]), "pkg/a/OWNERS": "approvers:\n- somebody\nreviewers:\n- other\n"})
    assert extract(root, "r", module=MOD).components["pkg/a"]["owner_team"] is None


ARCH = Path("data") / "snapshots" / "architecture" / "v1.9.0.json"


@pytest.mark.skipif(not ARCH.exists(), reason="committed architecture graph not present")
def test_dependency_proof_ten_of_ten_on_extracted_edges():
    graph = ArchitectureGraph.from_json(json.loads(ARCH.read_text(encoding="utf-8")))
    cases = dependency_proof_cases(graph, 10)
    assert len(cases) == 10
    found = sum(1 for c in cases if c["cause_component"] in graph.one_hop(c["failing_component"]))
    assert found == 10


def test_release_graph_reads_only_its_snapshot(tmp_path):
    reg = SnapshotRegistry(tmp_path / "data")
    store = reg.store()
    g = ArchitectureGraph("rev")
    g.components = {"pkg/a": {"files": 1}, "pkg/b": {"files": 1}}
    g.add_edge("pkg/a", "pkg/b", "depends_on")
    for record, content, etype in architecture_records(g, T0):
        store.put(record, AUTHORITATIVE_WRITERS["architecture"], content=content, entity_type=etype)
    info = reg.freeze(frozen_at=T0)
    rg = ReleaseGraph(store, ctx(info.snapshot_id))
    impact = rg.trace_dependency_impact("pkg/a")
    assert impact["depends_on"] == ["pkg/b"] and impact["runtime_relationships"] == "unknown"
    assert impact["limitation"] == RUNTIME_LIMITATION
    other = reg.freeze(frozen_at=T0)  # same live content, different snapshot id
    assert ReleaseGraph(store, ctx(other.snapshot_id)).trace_dependency_impact("pkg/a")["depends_on"] == ["pkg/b"]
    reg.close()
