"""Capture external portable/v1 input once; keep existing commits by reference."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue

from avalanche.file import File
from avalanche.references import LocalReferenceBinder, LocalWorkspaceStore
from avalanche.workspace import Workspace

from .state.runs import RunRequest

if TYPE_CHECKING:
    from .operator import Operator


def capture_keyed_input(op: Operator, request: RunRequest) -> RunRequest:
    store = LocalWorkspaceStore(op.input_files.root.parent / "workspaces")
    return request.model_copy(update={"input": capture_input(request.input, store)})


def capture_input(value: JsonValue, store: LocalWorkspaceStore) -> JsonValue:
    """No restaging: v2 artifacts are immutable, already-published objects."""
    if isinstance(value, dict):
        if value.get("kind") == "file" and "ref" in value:
            file = File.model_validate(value)
            if file.ref.workspace.startswith("local-ws:v2:"):
                return value
            root = LocalReferenceBinder().bind_workspace(
                Workspace(reference=file.ref.workspace)
            )
            committed = store.commit(root)
            return {**value, "ref": {"workspace": committed.reference, "path": file.ref.path}}
        if value.get("kind") == "workspace":
            return store.ingest(Workspace.model_validate(value)).manifest()
        # Portable workspaces have no kind tag for wire compatibility.
        if set(value) == {"version", "entries"}:
            return store.ingest(Workspace.model_validate(value)).manifest()
        return {name: capture_input(child, store) for name, child in value.items()}
    if isinstance(value, list):
        return [capture_input(child, store) for child in value]
    return value
