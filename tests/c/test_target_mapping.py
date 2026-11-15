from pathlib import Path

import pytest

from atlas.execution.target import CAUSE_HEADING, CHANGE_HEADING, NoTargetMapping, TargetManifest, change_section

MANIFEST = Path(__file__).resolve().parents[2] / "target" / "demo_target.toml"


def test_manifest_maps_an_ipv6_test_defect_to_the_sample_package():
    manifest = TargetManifest.load(MANIFEST)
    mapped = manifest.map("test_defect", ["The IPv6 lane tests were not adapted to the new image"])
    assert mapped.mapping_id == "ipv6-host-port"
    assert mapped.scope == ("netaddr/*.go",)
    assert mapped.data_mode == "SYNTHETIC"
    assert mapped.task_text.startswith("Demo target (sample code modelled on the real issue)")
    assert CHANGE_HEADING in mapped.task_text and CAUSE_HEADING in mapped.task_text
    assert "JoinHostPort" in change_section(mapped.task_text)
    assert "new image" not in change_section(mapped.task_text)


def test_a_task_the_sample_code_cannot_answer_is_refused():
    manifest = TargetManifest.load(MANIFEST)
    with pytest.raises(NoTargetMapping):
        manifest.map("dependency", ["passt uses 100% CPU after the image update"])
    with pytest.raises(NoTargetMapping):
        manifest.map("test_defect", ["a timeout in the storage migration test"])


def test_missing_manifest_means_no_mapping(tmp_path):
    assert TargetManifest.load(tmp_path / "none.toml") is None


def test_change_section_of_unmapped_text_is_the_text():
    assert change_section("Fix the cause") == "Fix the cause"


def test_a_malformed_change_from_the_coding_agent_is_asked_for_once_more():
    from types import SimpleNamespace

    from atlas.execution.prepare import PreparationRefused, Preparer
    from tests.c.fakes import FIX_DIFF

    base = {"netaddr/netaddr.go": (Path(__file__).resolve().parents[2] / "target" / "repo" / "netaddr" / "netaddr.go").read_text(encoding="utf-8")}
    task = SimpleNamespace(task_text="fix", scope=("netaddr/*.go",))

    class Agent:
        def __init__(self, answers):
            self.answers = list(answers)
            self.calls = 0

        def propose(self, text, scope, tree):
            self.calls += 1
            return self.answers.pop(0)

    bad = "--- a/netaddr/netaddr.go\n+++ b/netaddr/netaddr.go\n@@ -1,9 +1,2 @@\n package netaddr\n"
    preparer = Preparer.__new__(Preparer)
    preparer.agent = Agent([bad, FIX_DIFF])
    assert preparer.propose_valid(task, base) == FIX_DIFF and preparer.agent.calls == 2
    preparer.agent = Agent([bad, bad])
    with pytest.raises(PreparationRefused):
        preparer.propose_valid(task, base)


def test_mapped_task_has_a_short_title_for_commits_and_pull_requests():
    from atlas.execution.target import summary_line

    mapped = TargetManifest.load(MANIFEST).map("test_defect", ["IPv6 lane only"])
    assert summary_line(mapped.task_text) == "Format IPv6 hosts correctly in netaddr.JoinHostPort"
    assert len(summary_line("x" * 200)) <= 72
