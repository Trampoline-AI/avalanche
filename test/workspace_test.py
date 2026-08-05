from __future__ import annotations

import json
import os
import pickle
from pathlib import Path

import pytest
from pydantic import BaseModel

import avalanche as ava
import avalanche.workspace as workspace_module
from avalanche.workspace import WorkspaceEntry
from runtime.operator.client import _json_payload
from runtime.operator.results import decode_workflow_result, encode_workflow_result


def _workspace_files(workspace: ava.Workspace) -> dict[str, bytes]:
    return {
        entry.path: entry.content
        for entry in workspace.entries
        if entry.kind == "file" and entry.content is not None
    }


def test_workspace_captures_a_deterministic_tree_and_rejects_unsafe_entries(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "report.txt").write_text("hello")
    (tmp_path / "empty").mkdir()

    workspace = ava.Workspace.from_path(tmp_path)

    assert [entry.path for entry in workspace.entries] == [
        "empty",
        "nested",
        "nested/report.txt",
    ]
    assert ava.Workspace.from_manifest(workspace.manifest()).manifest() == workspace.manifest()
    pickled = pickle.dumps(workspace)
    restored = pickle.loads(pickled)
    assert restored.manifest() == workspace.manifest()

    def unexpected_materialization(*args, **kwargs):
        raise AssertionError("terminal Workspace.path allocated a temporary tree")

    monkeypatch.setattr("avalanche.workspace.tempfile.mkdtemp", unexpected_materialization)
    with pytest.raises(RuntimeError, match="only during Avalanche node execution"):
        _ = workspace.path
    with pytest.raises(RuntimeError, match="only during Avalanche node execution"):
        _ = restored.path

    with pytest.raises(ValueError, match="unsafe"):
        ava.Workspace.from_manifest(
            {"version": 1, "entries": [{"kind": "directory", "path": "../escape"}]}
        )


def test_workspace_rejects_source_and_child_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")

    root_link = tmp_path / "root-link"
    root_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="source must be a directory"):
        ava.Workspace.from_path(root_link)

    (source / "file-link").symlink_to(outside / "secret.txt")
    with pytest.raises(ValueError, match="unsupported entry 'file-link'"):
        ava.Workspace.from_path(source)

    (source / "file-link").unlink()
    (source / "directory-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="unsupported entry 'directory-link'"):
        ava.Workspace.from_path(source)


@pytest.mark.parametrize("replacement_kind", ["file", "directory"])
def test_workspace_rejects_entry_replaced_with_symlink_before_open(
    tmp_path,
    monkeypatch,
    replacement_kind,
):
    source = tmp_path / "source"
    source.mkdir()
    selected = source / "selected"
    outside = tmp_path / "outside"
    if replacement_kind == "file":
        selected.write_text("selected")
        outside.write_text("outside secret")
    else:
        selected.mkdir()
        (selected / "inside.txt").write_text("selected")
        outside.mkdir()
        (outside / "secret.txt").write_text("outside secret")

    real_open = os.open
    replaced = False

    def replace_before_open(path, flags, *, dir_fd=None):
        nonlocal replaced
        if path == "selected" and dir_fd is not None and not replaced:
            replaced = True
            selected.rename(source / "selected-original")
            selected.symlink_to(
                outside,
                target_is_directory=replacement_kind == "directory",
            )
        if dir_fd is None:
            return real_open(path, flags)
        return real_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(workspace_module.os, "open", replace_before_open)

    with pytest.raises(ValueError, match="entry 'selected' changed while being captured"):
        ava.Workspace.from_path(source)
    assert replaced
    assert (
        outside if replacement_kind == "file" else outside / "secret.txt"
    ).read_text() == "outside secret"


def test_workspace_rejects_root_replacement_without_following_it(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "selected.txt").write_text("selected")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret")
    source_identity = (source.stat().st_dev, source.stat().st_ino)
    real_listdir = os.listdir
    replaced = False

    def replace_root_after_listing(descriptor):
        nonlocal replaced
        names = real_listdir(descriptor)
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == source_identity and not replaced:
            replaced = True
            source.rename(tmp_path / "source-original")
            source.symlink_to(outside, target_is_directory=True)
        return names

    monkeypatch.setattr(workspace_module.os, "listdir", replace_root_after_listing)

    with pytest.raises(ValueError, match="root directory changed while being captured"):
        ava.Workspace.from_path(source)
    assert replaced
    assert (outside / "secret.txt").read_text() == "outside secret"


def test_workspace_rejects_directory_mutation_after_listing(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "selected.txt").write_text("selected")
    source_identity = (source.stat().st_dev, source.stat().st_ino)
    real_listdir = os.listdir
    mutated = False

    def mutate_root_after_listing(descriptor):
        nonlocal mutated
        names = real_listdir(descriptor)
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == source_identity and not mutated:
            mutated = True
            (source / "concurrent.txt").write_text("concurrent")
        return names

    monkeypatch.setattr(workspace_module.os, "listdir", mutate_root_after_listing)

    with pytest.raises(ValueError, match="root directory changed while being captured"):
        ava.Workspace.from_path(source)
    assert mutated


def test_workspace_rejects_file_mutation_after_open(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    selected = source / "selected.txt"
    selected.write_text("selected")
    selected_identity = (selected.stat().st_dev, selected.stat().st_ino)
    real_read = os.read
    mutated = False

    def mutate_file_before_read(descriptor, size):
        nonlocal mutated
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == selected_identity and not mutated:
            mutated = True
            selected.write_text("concurrent mutation")
        return real_read(descriptor, size)

    monkeypatch.setattr(workspace_module.os, "read", mutate_file_before_read)

    with pytest.raises(ValueError, match="entry 'selected.txt' changed while being captured"):
        ava.Workspace.from_path(source)
    assert mutated


def test_workspace_capture_accepts_materializable_depth_with_bounded_descriptors(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    current = source
    parts = []
    for index in range(workspace_module._MAX_WORKSPACE_CAPTURE_DEPTH - 1):
        part = f"level-{index}"
        parts.append(part)
        current /= part
        current.mkdir()
    current.joinpath("value.txt").write_text("value")

    real_open = os.open
    real_close = os.close
    live_descriptors: set[int] = set()
    maximum_open = 0

    def tracked_open(path, flags, *args, **kwargs):
        nonlocal maximum_open
        descriptor = real_open(path, flags, *args, **kwargs)
        live_descriptors.add(descriptor)
        maximum_open = max(maximum_open, len(live_descriptors))
        return descriptor

    def tracked_close(descriptor):
        live_descriptors.discard(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(workspace_module.os, "open", tracked_open)
    monkeypatch.setattr(workspace_module.os, "close", tracked_close)

    workspace = ava.Workspace.from_path(source)

    assert _workspace_files(workspace) == {"/".join([*parts, "value.txt"]): b"value"}
    assert maximum_open <= workspace_module._MAX_WORKSPACE_CAPTURE_DEPTH + 1
    assert live_descriptors == set()


def test_workspace_capture_rejects_excess_depth_before_open_and_closes_ancestors(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    current = source
    for index in range(workspace_module._MAX_WORKSPACE_CAPTURE_DEPTH):
        current /= f"level-{index}"
        current.mkdir()

    real_open = os.open
    real_close = os.close
    live_descriptors: set[int] = set()

    def limited_open(path, flags, *args, **kwargs):
        if len(live_descriptors) >= workspace_module._MAX_WORKSPACE_CAPTURE_DEPTH:
            raise AssertionError("capture attempted to open beyond its descriptor bound")
        descriptor = real_open(path, flags, *args, **kwargs)
        live_descriptors.add(descriptor)
        return descriptor

    def tracked_close(descriptor):
        live_descriptors.discard(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(workspace_module.os, "open", limited_open)
    monkeypatch.setattr(workspace_module.os, "close", tracked_close)

    with pytest.raises(ValueError, match="capture depth limit of 8"):
        ava.Workspace.from_path(source)

    assert live_descriptors == set()


def test_pickled_portable_workspace_can_materialize_during_later_execution(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "before.txt").write_text("before")
    workspace = pickle.loads(pickle.dumps(ava.Workspace.from_path(source)))
    materialized_paths: list[Path] = []

    class Request(ava.BaseInput):
        workspace: ava.Workspace

    @ava.source
    def read(request: Request):
        path = request.workspace.path
        materialized_paths.append(path)
        return path.joinpath("before.txt").read_text()

    @ava.workflow(input=Request)
    def flow():
        return read(ava.input)

    assert (
        flow().run(input=Request(workspace=workspace), executor=ava.LocalExecutor()).result()
        == "before"
    )
    assert materialized_paths
    assert all(not path.exists() for path in materialized_paths)
    with pytest.raises(RuntimeError, match="only during Avalanche node execution"):
        _ = workspace.path


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (
            {"version": 1, "entries": [{"kind": "directory", "path": "."}]},
            "root pseudo-path",
        ),
        (
            {"version": True, "entries": []},
            "Unsupported workspace manifest",
        ),
        (
            {
                "version": 1,
                "entries": [
                    {
                        "kind": "file",
                        "path": "missing/value.txt",
                        "content": "dmFsdWU=",
                        "sha256": (
                            "cd42404d52ad55ccfa9aca4adc828aa5800ad9d385a0671fbcbf7"
                            "24118320619"
                        ),
                    }
                ],
            },
            "missing directory",
        ),
        (
            {
                "version": 1,
                "entries": [
                    {
                        "kind": "file",
                        "path": "parent",
                        "content": "dmFsdWU=",
                        "sha256": (
                            "cd42404d52ad55ccfa9aca4adc828aa5800ad9d385a0671fbcbf7"
                            "24118320619"
                        ),
                    },
                    {
                        "kind": "file",
                        "path": "parent/child",
                        "content": "dmFsdWU=",
                        "sha256": (
                            "cd42404d52ad55ccfa9aca4adc828aa5800ad9d385a0671fbcbf7"
                            "24118320619"
                        ),
                    },
                ],
            },
            "collides",
        ),
        (
            {
                "version": 1,
                "entries": [
                    {"kind": "directory", "path": "same"},
                    {"kind": "directory", "path": "same"},
                ],
            },
            "Duplicate",
        ),
        (
            {
                "version": 1,
                "entries": [
                    {
                        "kind": "file",
                        "path": "value.txt",
                        "content": "dmFsdWU=",
                        "sha256": "0" * 64,
                    }
                ],
            },
            "sha256",
        ),
        (
            {
                "version": 1,
                "entries": [
                    {
                        "kind": "file",
                        "path": "value.txt",
                        "content": "not-base64!",
                        "sha256": "0" * 64,
                    }
                ],
            },
            "base64",
        ),
        (
            {
                "version": 1,
                "entries": [
                    {
                        "kind": "file",
                        "path": "value.txt",
                        "content": b"value",
                        "sha256": (
                            "cd42404d52ad55ccfa9aca4adc828aa5800ad9d385a0671fbcbf7"
                            "24118320619"
                        ),
                    }
                ],
            },
            "base64 string",
        ),
    ],
)
def test_workspace_rejects_malformed_manifests(manifest, message):
    with pytest.raises(ValueError, match=message):
        ava.Workspace.from_manifest(manifest)


def test_workspace_direct_typed_construction_remains_available():
    workspace = ava.Workspace(
        entries=(WorkspaceEntry(path="empty", kind="directory"),),
    )

    assert workspace.entries == (WorkspaceEntry(path="empty", kind="directory"),)
    with pytest.raises(ValueError, match="Unsupported workspace manifest"):
        ava.Workspace.from_manifest({"entries": []})


def test_workspace_rejects_corrupt_constructed_tree_before_materialization(monkeypatch):
    corrupt = ava.Workspace.model_construct(
        entries=(
            WorkspaceEntry.model_construct(
                path=".",
                kind="directory",
                content=None,
                sha256=None,
            ),
        )
    )

    def unexpected_materialization(*args, **kwargs):
        raise AssertionError("temporary tree was allocated before validation")

    monkeypatch.setattr("avalanche.workspace.tempfile.mkdtemp", unexpected_materialization)

    from avalanche.workspace import run_workspace_invocation

    with pytest.raises(ValueError, match="root pseudo-path"):
        run_workspace_invocation(lambda workspace: workspace.path, corrupt)


def test_local_sibling_workspace_inputs_are_isolated_and_cleaned(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "before.txt").write_text("before")
    materialized_paths: list[Path] = []

    class Request(ava.BaseInput):
        workspace: ava.Workspace

    @ava.source
    def left(request: Request):
        materialized_paths.append(request.workspace.path)
        request.workspace.path.joinpath("left.txt").write_text("left")
        return request.workspace

    @ava.source
    def right(request: Request):
        materialized_paths.append(request.workspace.path)
        assert not request.workspace.path.joinpath("left.txt").exists()
        request.workspace.path.joinpath("right.txt").write_text("right")
        return request.workspace

    @ava.workflow(input=Request)
    def flow():
        return left(ava.input), right(ava.input)

    left_result, right_result = (
        flow()
        .run(
            input=Request(workspace=ava.Workspace.from_path(source)),
            executor=ava.LocalExecutor(),
        )
        .result()
    )

    assert len(materialized_paths) == 2
    assert materialized_paths[0] != materialized_paths[1]
    assert all(not path.exists() for path in materialized_paths)
    assert _workspace_files(left_result) == {
        "before.txt": b"before",
        "left.txt": b"left",
    }
    assert _workspace_files(right_result) == {
        "before.txt": b"before",
        "right.txt": b"right",
    }
    with pytest.raises(RuntimeError, match="only during Avalanche node execution"):
        _ = left_result.path
    with pytest.raises(RuntimeError, match="only during Avalanche node execution"):
        _ = right_result.path
    assert (source / "before.txt").read_text() == "before"
    assert sorted(path.name for path in source.iterdir()) == ["before.txt"]


def test_workspace_propagates_only_through_returned_interstep_values(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "before.txt").write_text("before")

    class Request(ava.BaseInput):
        workspace: ava.Workspace

    @ava.source
    def first(request: Request):
        request.workspace.path.joinpath("first.txt").write_text("first")
        return request.workspace

    @ava.step
    def second(workspace: ava.Workspace):
        assert workspace.path.joinpath("first.txt").read_text() == "first"
        workspace.path.joinpath("second.txt").write_text("second")
        return workspace

    @ava.workflow(input=Request)
    def flow():
        return second(first(ava.input))

    result = (
        flow()
        .run(
            input=Request(workspace=ava.Workspace.from_path(source)),
            executor=ava.LocalExecutor(),
        )
        .result()
    )

    assert _workspace_files(result) == {
        "before.txt": b"before",
        "first.txt": b"first",
        "second.txt": b"second",
    }


def test_workspace_invocation_cleanup_runs_after_failure(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("caller-owned")
    materialized_paths: list[Path] = []

    class Request(ava.BaseInput):
        workspace: ava.Workspace

    @ava.source
    def fail(request: Request):
        materialized_paths.append(request.workspace.path)
        request.workspace.path.joinpath("transient.txt").write_text("transient")
        raise RuntimeError("node failed")

    @ava.workflow(input=Request)
    def flow():
        return fail(ava.input)

    with pytest.raises(RuntimeError, match="node failed"):
        flow().run(
            input=Request(workspace=ava.Workspace.from_path(source)),
            executor=ava.LocalExecutor(),
        ).result()

    assert len(materialized_paths) == 1
    assert not materialized_paths[0].exists()
    assert (source / "keep.txt").read_text() == "caller-owned"
    assert not (source / "transient.txt").exists()


def test_workspace_result_codec_round_trips_nested_values(tmp_path):
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "data.bin").write_bytes(b"contents")
    value = {"items": [ava.Workspace.from_path(tmp_path)]}

    restored = decode_workflow_result(encode_workflow_result(value))

    assert isinstance(restored["items"][0], ava.Workspace)
    assert _workspace_files(restored["items"][0]) == {"dir/data.bin": b"contents"}


def test_nested_pydantic_workspace_result_transport_preserves_manifest(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "before.txt").write_text("before")

    class NestedResult(BaseModel):
        workspaces: list[ava.Workspace]

    workspace = ava.Workspace.from_path(source)

    restored = decode_workflow_result(
        encode_workflow_result({"nested": NestedResult(workspaces=[workspace])})
    )

    restored_workspace = restored["nested"]["workspaces"][0]
    assert isinstance(restored_workspace, ava.Workspace)
    assert restored_workspace.manifest() == workspace.manifest()


def test_operator_input_json_roundtrip_preserves_workspace_manifest(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "before.txt").write_text("before")

    class Request(ava.BaseInput):
        workspace: ava.Workspace

    workspace = ava.Workspace.from_path(source)

    restored = Request.model_validate(json.loads(_json_payload(Request(workspace=workspace))))

    assert restored.workspace.manifest() == workspace.manifest()


def test_workspace_pickle_inside_node_captures_changes_and_cleans(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "before.txt").write_text("before")
    materialized_paths: list[Path] = []

    class Request(ava.BaseInput):
        workspace: ava.Workspace

    @ava.source
    def roundtrip(request: Request):
        first_path = request.workspace.path
        materialized_paths.append(first_path)
        first_path.joinpath("after.txt").write_text("after")
        restored = pickle.loads(pickle.dumps(request.workspace))
        assert not first_path.exists()
        second_path = restored.path
        materialized_paths.append(second_path)
        assert second_path.joinpath("after.txt").read_text() == "after"
        return restored

    @ava.workflow(input=Request)
    def flow():
        return roundtrip(ava.input)

    result = (
        flow()
        .run(
            input=Request(workspace=ava.Workspace.from_path(source)),
            executor=ava.LocalExecutor(),
        )
        .result()
    )

    assert all(not path.exists() for path in materialized_paths)
    assert _workspace_files(result) == {
        "after.txt": b"after",
        "before.txt": b"before",
    }
    with pytest.raises(RuntimeError, match="only during Avalanche node execution"):
        _ = result.path


@pytest.mark.ray
def test_real_ray_workspace_input_interstep_and_result_roundtrip(tmp_path):
    pytest.importorskip("ray")
    import ray

    if ray.is_initialized():
        ray.shutdown()
    source = tmp_path / "source"
    source.mkdir()
    (source / "before.txt").write_text("before")

    class Request(ava.BaseInput):
        workspace: ava.Workspace

    @ava.source
    def first(request: Request):
        request.workspace.path.joinpath("first.txt").write_text("first")
        return request.workspace

    @ava.dest
    def second(workspace: ava.Workspace):
        assert workspace.path.joinpath("first.txt").read_text() == "first"
        workspace.path.joinpath("second.txt").write_text("second")
        return workspace

    @ava.workflow(input=Request)
    def flow():
        return second(first(ava.input))

    ray.init(
        address="local",
        num_cpus=2,
        include_dashboard=False,
    )
    try:
        result = (
            flow()
            .run(
                input=Request(workspace=ava.Workspace.from_path(source)),
                executor=ava.RayExecutor(),
            )
            .result()
        )
        assert _workspace_files(result) == {
            "before.txt": b"before",
            "first.txt": b"first",
            "second.txt": b"second",
        }
    finally:
        ray.shutdown()


@pytest.mark.ray
def test_real_ray_sibling_workspaces_are_isolated_and_cleaned(tmp_path):
    pytest.importorskip("ray")
    import ray

    if ray.is_initialized():
        ray.shutdown()
    source = tmp_path / "source"
    source.mkdir()
    (source / "before.txt").write_text("before")

    class Request(ava.BaseInput):
        workspace: ava.Workspace

    @ava.source
    def left(request: Request):
        path = request.workspace.path
        assert not path.joinpath("right.txt").exists()
        path.joinpath("left.txt").write_text("left")
        return {"workspace": request.workspace, "materialized_path": str(path)}

    @ava.source
    def right(request: Request):
        path = request.workspace.path
        assert not path.joinpath("left.txt").exists()
        path.joinpath("right.txt").write_text("right")
        return {"workspace": request.workspace, "materialized_path": str(path)}

    @ava.workflow(input=Request)
    def flow():
        return left(ava.input), right(ava.input)

    ray.init(
        address="local",
        num_cpus=2,
        include_dashboard=False,
    )
    try:
        left_result, right_result = (
            flow()
            .run(
                input=Request(workspace=ava.Workspace.from_path(source)),
                executor=ava.RayExecutor(),
            )
            .result()
        )
        left_path = Path(left_result["materialized_path"])
        right_path = Path(right_result["materialized_path"])
        assert left_path != right_path
        assert not left_path.exists()
        assert not right_path.exists()
        assert _workspace_files(left_result["workspace"]) == {
            "before.txt": b"before",
            "left.txt": b"left",
        }
        assert _workspace_files(right_result["workspace"]) == {
            "before.txt": b"before",
            "right.txt": b"right",
        }
    finally:
        ray.shutdown()
