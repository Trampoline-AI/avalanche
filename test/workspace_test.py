from __future__ import annotations

import base64
import hashlib
import json
import pickle
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import avalanche as ava
import avalanche.workspace as workspace_module
from avalanche.references import (
    LOCAL_WORKSPACE_ROOT_ENV,
    LocalReferenceBinder,
    read_stored_trees,
)
from avalanche.results import EncodedWorkflowResult
from avalanche.workspace import refuses_value_workspace
from runtime.operator.client import _json_payload
from runtime.operator.results import decode_workflow_result, encode_workflow_result


@pytest.fixture
def workspace_root(tmp_path, monkeypatch) -> Path:
    """A host workspace folder that lasts only for this test."""
    root = tmp_path / "host-workspaces"
    root.mkdir()
    monkeypatch.setenv(LOCAL_WORKSPACE_ROOT_ENV, str(root))
    return root


def _inline_tree(files: dict[str, bytes]) -> dict:
    """The wire form earlier releases used to carry a tree's bytes."""
    parents = {str(parent) for name in files for parent in Path(name).parents}
    directories = sorted(parents - {"."})
    return {
        "version": 1,
        "entries": [
            *({"kind": "directory", "path": path} for path in directories),
            *(
                {
                    "kind": "file",
                    "path": path,
                    "content": base64.b64encode(content).decode(),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for path, content in files.items()
            ),
        ],
    }


def test_run_workspace_placeholder_carries_no_tree_and_needs_a_run():
    workspace = ava.Workspace()
    for value in (workspace, pickle.loads(pickle.dumps(workspace))):
        assert value.reference is None
        assert not value.is_reference
        assert value.model_dump() == {"version": 1, "entries": []}
        assert ava.Workspace.model_validate(value.model_dump()).reference is None
        with pytest.raises(RuntimeError):
            _ = value.path


def test_flow_steps_share_one_run_workspace_in_place(workspace_root):
    class Snapshot(BaseModel, frozen=True):
        files: int
        checkout: ava.Workspace

    seen: dict[str, Path] = {}

    @ava.source
    def fill(checkout: ava.Workspace) -> Snapshot:
        seen["fill"] = checkout.path
        for index in range(3):
            (checkout.path / f"part-{index}.bin").write_bytes(b"x" * 2 * 1024 * 1024)
        return Snapshot(files=3, checkout=checkout)

    @ava.step
    def measure(snapshot: Snapshot, other: ava.Workspace) -> Snapshot:
        seen["measure"] = snapshot.checkout.path
        # A second Workspace() in the same run is the same folder.
        seen["other"] = other.path
        sizes = [path.stat().st_size for path in snapshot.checkout.path.iterdir()]
        assert sum(sizes) == 6 * 1024 * 1024
        return snapshot

    @ava.workflow
    def flow():
        snapshot = fill(checkout=ava.Workspace())
        return measure(snapshot, ava.Workspace())

    result = flow().run(executor=ava.LocalExecutor()).result()

    assert seen["fill"] == seen["measure"] == seen["other"]
    assert seen["fill"].parent == workspace_root / "runs"
    # The step's files are still where it wrote them: nothing was copied or removed.
    assert len(list(seen["fill"].iterdir())) == 3
    encoded = encode_workflow_result(result)
    assert len(encoded.value_json) < 1024
    assert result.checkout.reference.startswith("local-ws:v1:")
    restored = decode_workflow_result(encoded)["checkout"]
    assert restored.path == seen["fill"]


def test_workspace_created_inside_a_step_is_returned_by_reference(workspace_root):
    @ava.source
    def write():
        workspace = ava.Workspace()
        workspace.path.joinpath("made.txt").write_text("made")
        return {"workspace": workspace}

    @ava.workflow
    def flow():
        return write()

    workspace = flow().run(executor=ava.LocalExecutor()).result()["workspace"]

    assert workspace.reference.startswith("local-ws:v1:")
    assert workspace.path.joinpath("made.txt").read_text() == "made"
    assert LocalReferenceBinder().bind_workspace(workspace) == workspace.path


def test_from_path_copies_the_directory_once_and_refers_to_the_copy(workspace_root, tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "kept.txt").write_text("kept")
    workspace = ava.Workspace.from_path(source)
    (source / "later.txt").write_text("written after")
    workspace.path.joinpath("edit.txt").write_text("edited")

    assert workspace.path.parent == workspace_root / "captures"
    assert workspace.path.joinpath("nested", "kept.txt").read_text() == "kept"
    assert not workspace.path.joinpath("later.txt").exists()
    assert not (source / "edit.txt").exists()
    assert str(tmp_path) not in workspace.model_dump_json()
    with pytest.raises(ValueError):
        ava.Workspace.from_path(source / "later.txt")


def test_workspace_refuses_an_inline_file_tree():
    tree = _inline_tree({"a.txt": b"a"})
    for value in (tree, {"entries": tree["entries"]}):
        with pytest.raises(ValidationError) as refused:
            ava.Workspace.model_validate(value)
        assert refuses_value_workspace(refused.value)
    with pytest.raises(ValidationError) as other:
        ava.Workspace.model_validate({"version": 2, "entries": []})
    assert not refuses_value_workspace(other.value)


def test_stored_inline_tree_result_is_read_once_as_a_reference(workspace_root):
    tree = _inline_tree({"docs/a.txt": b"alpha", "b.bin": b"\x00\x01"})
    stored = EncodedWorkflowResult(
        value_json=json.dumps(
            {
                "version": 1,
                "value": {
                    "kind": "dict",
                    "items": [["checkout", {"kind": "workspace", "manifest": tree}]],
                },
            }
        )
    )

    first = decode_workflow_result(stored)["checkout"]
    second = decode_workflow_result(stored)["checkout"]

    assert first.reference == second.reference
    assert first.path == second.path
    assert first.path.parent == workspace_root / "stored-trees"
    assert first.path.joinpath("docs/a.txt").read_bytes() == b"alpha"
    assert first.path.joinpath("b.bin").read_bytes() == b"\x00\x01"
    assert read_stored_trees({"checkout": tree})["checkout"] == first.manifest()


def test_each_run_started_from_a_stored_inline_tree_edits_its_own_copy(workspace_root):
    tree = _inline_tree({"notes.txt": b"stored"})
    binder = LocalReferenceBinder()

    def start() -> Path:
        manifest = read_stored_trees({"checkout": tree}, own_copy=True)["checkout"]
        return binder.bind_workspace(ava.Workspace.model_validate(manifest))

    first, second = start(), start()
    (first / "notes.txt").write_text("edited by the first run")
    (first / "extra.txt").write_text("left behind")

    assert first != second
    assert first.parent == second.parent == workspace_root / "captures"
    assert (second / "notes.txt").read_text() == "stored"
    assert not (second / "extra.txt").exists()
    assert (start() / "notes.txt").read_text() == "stored"


@pytest.mark.parametrize("corruption", ["escape", "sha256", "missing_parent", "duplicate"])
def test_stored_inline_tree_is_refused_when_unsafe_or_corrupt(workspace_root, corruption):
    tree = _inline_tree({"docs/a.txt": b"alpha"})
    if corruption == "escape":
        tree["entries"][1]["path"] = "../a.txt"
    elif corruption == "sha256":
        tree["entries"][1]["sha256"] = "0" * 64
    elif corruption == "missing_parent":
        del tree["entries"][0]
    else:
        tree["entries"].append(dict(tree["entries"][1]))

    with pytest.raises(ValueError):
        read_stored_trees(tree)
    assert not any((workspace_root / "stored-trees").glob("[0-9a-f]*"))
    assert not (workspace_root.parent / "a.txt").exists()


def test_directory_workspace_uses_host_path_without_capture(tmp_path, monkeypatch):
    (tmp_path / "keep.txt").write_text("host-owned")
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    path = alias / ".." / tmp_path.name

    def unexpected_capture(*args, **kwargs):
        raise AssertionError("reference workspace attempted to capture or materialize")

    monkeypatch.setattr(ava.Workspace, "from_path", unexpected_capture)
    workspace = ava.Workspace.from_directory(path, reference="opaque: host / data")
    assert workspace.path == path
    assert workspace.reference == "opaque: host / data"
    assert workspace.is_reference
    assert workspace.snapshot() is workspace
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
    manifest = {"kind": "workspace", "version": 1, "reference": reference}

    def reference_serializer(value):
        return value.manifest()

    for mode in ("python", "json"):
        assert workspace.model_dump(mode=mode) == manifest
        assert (
            workspace.model_dump(
                mode=mode,
                context={
                    workspace_module._WORKSPACE_SERIALIZER_CONTEXT_KEY: reference_serializer
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


def test_reference_workspace_rejects_entries():
    with pytest.raises(ValueError):
        ava.Workspace.model_validate({"version": 1, "reference": "ref", "entries": []})


def test_reference_workspace_transport_emits_no_attachments(tmp_path):
    class Request(ava.BaseInput):
        workspace: ava.Workspace

    workspace = ava.Workspace.from_directory(tmp_path, reference="host-managed:example")
    manifest = {"kind": "workspace", "version": 1, "reference": workspace.reference}
    request = Request(workspace=workspace)
    assert json.loads(_json_payload(request))["workspace"] == manifest
    for value in (workspace, request, {"workspace": workspace}):
        encoded = encode_workflow_result(value)
        assert encoded.files == ()
        assert str(tmp_path) not in encoded.value_json
        assert "host-managed:example" in encoded.value_json
    for value in (workspace, request, {"workspace": workspace}):
        restored = decode_workflow_result(encode_workflow_result(value))
        actual = restored if isinstance(restored, ava.Workspace) else restored["workspace"]
        assert isinstance(actual, ava.Workspace)
        assert actual.manifest() == manifest
    with pytest.warns(DeprecationWarning):
        assert (
            ava.Workspace.from_manifest({"version": 1, "reference": "legacy"}).reference
            == "legacy"
        )
    for kind in ("file", "object", "other"):
        with pytest.raises(ValueError):
            ava.Workspace.from_manifest({**manifest, "kind": kind})


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


@pytest.mark.parametrize("fault", ["escape", "unfinished", "interleaved", "orphan"])
def test_a_refused_workspace_stream_leaves_nothing_in_the_store(tmp_path, fault):
    from runtime.operator.run_workspaces import (
        StagedWorkspaceError,
        WorkspaceChunk,
        stage_input_workspace,
    )

    def piece(path, content=b"x", end=True):
        return WorkspaceChunk(path=path, directory=False, content=content, end_of_file=end)

    folder = WorkspaceChunk(path="docs", directory=True, content=b"", end_of_file=False)
    chunks = {
        "escape": [folder, piece("../outside.txt")],
        "unfinished": [folder, piece("docs/a.txt", end=False)],
        "interleaved": [piece("a.txt", end=False), piece("b.txt")],
        "orphan": [piece("missing/a.txt")],
    }[fault]

    with pytest.raises(StagedWorkspaceError):
        stage_input_workspace(tmp_path / "store", iter(chunks))

    assert list((tmp_path / "store" / "captures").iterdir()) == []
    assert not (tmp_path / "outside.txt").exists()
