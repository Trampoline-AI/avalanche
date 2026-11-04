from __future__ import annotations

from runtime.operator import WorkflowRegistry


def test_durable_waits_admission_survives_subprocess_and_cache(tmp_path, monkeypatch):
    """Read the frozen contract back from disk, then invalidate an old schema."""
    import json
    import shutil
    from pathlib import Path

    from runtime.operator.discovery import configure_roots
    from runtime.operator.discovery_cache import DiscoveryCache

    monkeypatch.chdir(tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "durable_waits_flow.py"
    target = tmp_path / "durable_waits_flow.py"
    shutil.copyfile(fixture, target)
    registry = WorkflowRegistry()
    registry.scan([str(target)])
    assert registry.view.diagnostics == ()
    (info,) = registry.list_workflows()
    assert info.node_policies["draft_1"].retries == 3
    assert info.node_policies["draft_1"].on_uncertain == "hold"
    assert info.node_policies["publish_1"].retries == 0
    assert info.node_policies["publish_1"].on_uncertain == "fail"
    assert info.node_types["decision_1"] == "wait"
    review = info.node_waits["decision_1"]
    assert review.kind == "decision"
    assert review.approvers == ("reviewer",)
    assert review.show_node_id == "draft_1"
    assert review.on_timeout == {"kind": "reject", "note": ""}
    event = info.node_waits["wait_for_event_1"]
    assert event.key == "docusign.signed"
    assert event.schema_["properties"]["envelope_id"]["type"] == "string"
    cache = DiscoveryCache(configure_roots([str(target)]))
    files = cache.load()
    assert files is not None
    (descriptor,) = files[0].descriptors
    assert dict(descriptor.node_policies) == info.node_policies
    assert dict(descriptor.node_waits) == info.node_waits
    fresh = WorkflowRegistry()
    fresh.scan([str(target)])
    (cached_info,) = fresh.list_workflows()
    assert cached_info.node_waits == info.node_waits
    stale = json.loads(cache.path.read_text())
    stale["schema_version"] = 12
    for entry in stale["files"]:
        for item in entry["descriptors"]:
            item.pop("node_policies")
            item.pop("node_waits")
    cache.path.write_text(json.dumps(stale))
    rebuilt = WorkflowRegistry()
    rebuilt.scan([str(target)])
    (rebuilt_info,) = rebuilt.list_workflows()
    assert rebuilt_info.node_waits == info.node_waits
    assert cache.load() is not None

    invalid_target = tmp_path / "invalid_flow.py"
    invalid_target.write_text(
        fixture.read_text().replace('on_uncertain="hold"', 'on_uncertain="once"')
    )
    invalid = WorkflowRegistry()
    invalid.scan([str(invalid_target)])
    assert invalid.list_workflows() == []
    assert invalid.view.diagnostics


def test_directory_scan_ignores_excluded_directories(tmp_path):
    visible_flow = tmp_path / "flow.py"
    visible_flow.write_text(
        """
from avalanche import source, workflow


@source
def generate() -> int:
    return 1


@workflow
def visible():
    return generate()
"""
    )
    for directory_name in (".venv", "venv", "build", "node_modules"):
        excluded_flow = tmp_path / directory_name / "dependency.py"
        excluded_flow.parent.mkdir(parents=True)
        excluded_flow.write_text(
            'raise RuntimeError("must not import excluded directory code")'
        )

    registry = WorkflowRegistry()
    registry.scan([str(tmp_path)])

    assert [workflow.name for workflow in registry.list_workflows()] == ["visible"]
    assert registry.view.diagnostics == ()
