"""The graph endpoint gives every client the same shape: kind and label on nodes, source and
target on edges, while keeping the stored field names."""

from __future__ import annotations

from atlas.api.routes_runs import graph_view


def test_graph_view_adds_drawing_fields_and_keeps_stored_ones() -> None:
    g = graph_view({"nodes": [{"id": "component:pkg/util", "type": "component"}],
                    "edges": [{"from": "component:cmd/a", "to": "component:pkg/util", "kind": "depends_on"}]})
    node, edge = g["nodes"][0], g["edges"][0]
    assert node["kind"] == "component" and node["type"] == "component" and node["label"] == "pkg/util"
    assert edge["source"] == "component:cmd/a" and edge["target"] == "component:pkg/util"
    assert edge["from"] == "component:cmd/a" and edge["to"] == "component:pkg/util" and edge["kind"] == "depends_on"


def test_graph_view_handles_empty_and_already_normal_graphs() -> None:
    assert graph_view(None) == {"nodes": [], "edges": []}
    g = graph_view({"nodes": [{"id": "n", "kind": "issue", "label": "n"}], "edges": [{"source": "n", "target": "n"}]})
    assert g["nodes"][0]["type"] == "issue" and g["edges"][0]["kind"] == "link"
