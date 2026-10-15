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


def test_directory_workspace_uses_host_path_without_capture(tmp_path, monkeypatch):
    (tmp_path / "keep.txt").write_text("host-owned")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    path = alias / ".." / tmp_path.name

    def unexpected_capture(*args, **kwargs):
        raise AssertionError("reference workspace attempted to capture or materialize")

    monkeypatch.setattr(ava.Workspace, "from_path", unexpected_capture)
    monkeypatch.setattr(workspace_module.tempfile, "mkdtemp", unexpected_capture)
    monkeypatch.setattr(workspace_module._MaterializationOwner, "register", unexpected_capture)
    workspace = ava.Workspace.from_directory(path, reference="opaque: host / data")
    assert workspace.path == path
    assert workspace.reference == "opaque: host / data"
    assert workspace.is_reference
    assert workspace.entries == ()
    assert workspace.snapshot().path == path
    assert workspace.snapshot().reference == workspace.reference
    assert (
        workspace_module.run_workspace_invocation(lambda value: value, workspace) is workspace
    )
    assert workspace.path.joinpath("keep.txt").read_text() == "host-owned"
    assert ava.Workspace().reference is None
    assert not ava.Workspace().is_reference


@pytest.mark.parametrize("kind", ["relative", "missing", "file"])
def test_directory_workspace_requires_existing_absolute_directory(tmp_path, kind):
    path = tmp_path / kind
    if kind == "relative":
        path = Path(".")
    elif kind == "file":
        path.write_text("file")
    with pytest.raises(ValueError):
        ava.Workspace.from_directory(path, reference="valid")


@pytest.mark.parametrize("reference", ["", "x" * 1025, "null\x00byte", None, 1, b"bytes"])
def test_directory_workspace_rejects_invalid_reference(tmp_path, reference):
    with pytest.raises(ValueError):
        ava.Workspace.from_directory(tmp_path, reference=reference)
    with pytest.raises(ValueError):
        ava.Workspace.model_validate({"version": 1, "reference": reference})


def test_reference_workspace_serialization_is_path_free_and_unbound(tmp_path):
    reference = "\u00e9" * 1024
    workspace = ava.Workspace.from_directory(str(tmp_path), reference=reference)
    manifest = {"version": 1, "reference": reference}

    def unexpected_serializer(value):
        raise AssertionError("reference workspace called the value serializer hook")

    for mode in ("python", "json"):
        assert workspace.model_dump(mode=mode) == manifest
        assert (
            workspace.model_dump(
                mode=mode,
                context={
                    workspace_module._WORKSPACE_SERIALIZER_CONTEXT_KEY: unexpected_serializer
                },
            )
            == manifest
        )
    assert workspace.manifest() == manifest
    assert workspace.__getstate__() == manifest
    assert json.loads(workspace.model_dump_json()) == manifest
    assert str(tmp_path).encode() not in pickle.dumps(workspace)
    for restored in (
        ava.Workspace.model_validate(manifest),
        ava.Workspace.model_validate_json(workspace.model_dump_json()),
        ava.Workspace.from_manifest(manifest),
        pickle.loads(pickle.dumps(workspace)),
    ):
        assert restored.reference == reference
        assert restored.is_reference
        assert restored.entries == ()
        with pytest.raises(
            RuntimeError, match="^workspace reference is not bound to a directory$"
        ):
            _ = restored.path
        with pytest.raises(
            RuntimeError, match="^workspace reference is not bound to a directory$"
        ):
            restored.snapshot()
    assert workspace.path == tmp_path
    assert tmp_path.is_dir()


@pytest.mark.parametrize("entries", [(), (WorkspaceEntry(path="empty", kind="directory"),)])
def test_reference_workspace_rejects_entries(entries):
    with pytest.raises(ValueError):
        ava.Workspace(entries=entries, reference="ref")
    with pytest.raises(ValueError):
        ava.Workspace.model_validate({"version": 1, "reference": "ref", "entries": []})


def test_reference_workspace_transport_emits_no_attachments(tmp_path):
    class Request(ava.BaseInput):
        workspace: ava.Workspace

    workspace = ava.Workspace.from_directory(tmp_path, reference="host-managed:example")
    manifest = {"version": 1, "reference": workspace.reference}
    request = Request(workspace=workspace)
    assert json.loads(_json_payload(request))["workspace"] == manifest
    for value in (workspace, request, {"workspace": workspace}):
        encoded = encode_workflow_result(value)
        assert encoded.files == ()
        assert str(tmp_path) not in encoded.value_json
        assert "host-managed:example" in encoded.value_json
    restored = decode_workflow_result(encode_workflow_result({"workspace": workspace}))
    assert restored["workspace"].manifest() == manifest


@pytest.mark.parametrize("fail", [False, True])
def test_reference_workspace_local_workflow_preserves_host_directory(tmp_path, fail):
    workspace = ava.Workspace.from_directory(tmp_path, reference="host-managed:workflow")

    class Result(BaseModel):
        workspaces: list[ava.Workspace]

    @ava.source
    def write():
        workspace.path.joinpath("created.txt").write_text("created")
        return {"result": Result(workspaces=[workspace])}

    @ava.step
    def read(result):
        received = result["result"].workspaces[0]
        assert received is workspace
        assert received.path.joinpath("created.txt").read_text() == "created"
        if fail:
            raise RuntimeError("node failed")
        return received

    @ava.workflow
    def flow():
        return read(write())

    if fail:
        with pytest.raises(RuntimeError, match="node failed"):
            flow().run(executor=ava.LocalExecutor()).result()
    else:
        assert flow().run(executor=ava.LocalExecutor()).result() is workspace
    assert workspace.path == tmp_path
    assert tmp_path.joinpath("created.txt").read_text() == "created"


def test_unbound_reference_passes_return_capture_without_binding():
    from runtime.executor import _normalize_distributed_result

    workspace = ava.Workspace.model_validate({"version": 1, "reference": "remote:example"})
    assert (
        workspace_module.run_workspace_invocation(lambda value: value, workspace) is workspace
    )
    assert _normalize_distributed_result(workspace) is workspace


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


@pytest.mark.parametrize("excess_depth", [False, True])
def test_workspace_depth_boundary_closes_capture_descriptors(
    tmp_path, monkeypatch, excess_depth
):
    current = tmp_path
    parts = []
    for index in range(workspace_module._MAX_WORKSPACE_CAPTURE_DEPTH - 1 + excess_depth):
        part = f"level-{index}"
        parts.append(part)
        current /= part
        current.mkdir()
    current.joinpath("value.txt").write_text("value")
    real_open, real_close = os.open, os.close
    live = set()

    def tracked_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        live.add(descriptor)
        return descriptor

    def tracked_close(descriptor):
        live.remove(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(workspace_module.os, "open", tracked_open)
    monkeypatch.setattr(workspace_module.os, "close", tracked_close)
    if excess_depth:
        with pytest.raises(ValueError, match="depth limit"):
            ava.Workspace.from_path(tmp_path)
    else:
        assert _workspace_files(ava.Workspace.from_path(tmp_path)) == {
            "/".join([*parts, "value.txt"]): b"value"
        }
    assert live == set()


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", ".", "missing/child"],
)
def test_workspace_manifest_rejects_unsafe_or_incomplete_tree(tmp_path, path):
    (tmp_path / "value.txt").write_bytes(b"contents")
    manifest = ava.Workspace.from_path(tmp_path).manifest()
    manifest["entries"][0]["path"] = path
    with pytest.raises(ValueError):
        ava.Workspace.from_manifest(manifest)


@pytest.mark.parametrize("corruption", ["content", "sha256", "duplicate", "collision"])
def test_workspace_manifest_rejects_corrupt_content_and_colliding_paths(tmp_path, corruption):
    (tmp_path / "value.txt").write_bytes(b"contents")
    manifest = ava.Workspace.from_path(tmp_path).manifest()
    entry = manifest["entries"][0]
    if corruption == "content":
        entry["content"] = "not-base64!"
    elif corruption == "sha256":
        entry["sha256"] = "0" * 64
    elif corruption == "duplicate":
        manifest["entries"].append(dict(entry))
    else:
        manifest["entries"].append({**entry, "path": "value.txt/child"})
    with pytest.raises(ValueError):
        ava.Workspace.from_manifest(manifest)


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


@pytest.fixture(params=["local", pytest.param("ray", marks=pytest.mark.ray)])
def executor(request):
    if request.param == "local":
        yield ava.LocalExecutor()
        return
    ray = pytest.importorskip("ray")
    ray.init(address="local", num_cpus=2, include_dashboard=False)
    try:
        yield ava.RayExecutor()
    finally:
        ray.shutdown()


def test_workspace_transport_isolates_siblings_propagates_returns_and_cleans(
    tmp_path, executor
):
    (tmp_path / "before.txt").write_bytes(b"\x00before\xff")
    (tmp_path / "empty").mkdir()

    class Request(ava.BaseInput):
        workspace: ava.Workspace

    class NestedResult(BaseModel):
        workspaces: list[ava.Workspace]

    workspace = pickle.loads(pickle.dumps(ava.Workspace.from_path(tmp_path)))
    request = Request.model_validate(json.loads(_json_payload(Request(workspace=workspace))))

    @ava.source
    def left(request: Request):
        path = request.workspace.path
        assert path.joinpath("empty").is_dir()
        path.joinpath("left.txt").write_text("left")
        restored = pickle.loads(pickle.dumps(request.workspace))
        assert not path.exists()
        return {"workspace": restored, "paths": [str(path)]}

    @ava.step
    def next_step(result):
        workspace = result["workspace"]
        path = workspace.path
        assert path.joinpath("left.txt").read_text() == "left"
        path.joinpath("after.txt").write_text("after")
        return {"workspace": workspace, "paths": [*result["paths"], str(path)]}

    @ava.source
    def right(request: Request):
        path = request.workspace.path
        assert not path.joinpath("left.txt").exists()
        path.joinpath("right.txt").write_text("right")
        return {"workspace": request.workspace, "paths": [str(path)]}

    @ava.workflow(input=Request)
    def flow():
        return next_step(left(ava.input)), right(ava.input)

    left_result, right_result = flow().run(input=request, executor=executor).result(timeout=30)
    assert _workspace_files(left_result["workspace"]) == {
        "before.txt": b"\x00before\xff",
        "left.txt": b"left",
        "after.txt": b"after",
    }
    assert _workspace_files(right_result["workspace"]) == {
        "before.txt": b"\x00before\xff",
        "right.txt": b"right",
    }
    paths = left_result["paths"] + right_result["paths"]
    assert len(set(paths)) == 3
    assert all(not Path(path).exists() for path in paths)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["before.txt", "empty"]
    restored = decode_workflow_result(
        encode_workflow_result({"nested": NestedResult(workspaces=[left_result["workspace"]])})
    )["nested"]["workspaces"][0]
    assert restored.manifest() == left_result["workspace"].manifest()
    with pytest.raises(RuntimeError):
        _ = restored.path


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
