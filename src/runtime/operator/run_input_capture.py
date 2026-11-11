"""Capture external portable/v1 input once; keep existing commits by reference."""

from __future__ import annotations

from collections.abc import Callable
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
    with op._state_store.transaction() as connection:
        captured = capture_input(
            request.input,
            store,
            lambda reference: (
                connection.execute(
                    "SELECT 1 FROM workspace_commits WHERE reference=?", (reference,)
                ).fetchone()
                is not None
            ),
        )
    return request.model_copy(update={"input": captured})


def _import_external_commit(
    store: LocalWorkspaceStore, reference: str, known: Callable[[str], bool] | None
) -> None:
    # Initial local-OS ingress materializes a durable host-owned commit once.
    # References passed from runs are already pinned by the grant boundary.
    if known is None or known(reference):
        return
    source = LocalWorkspaceStore.resolve(reference)
    if source.is_relative_to(store.root / "commits"):
        return
    committed = store.commit(source)
    if committed.reference != reference:
        raise ValueError("Workspace content differs from its immutable commit")


def capture_input(
    value: JsonValue, store: LocalWorkspaceStore, known: Callable[[str], bool] | None = None
) -> JsonValue:
    """No restaging: v2 artifacts are immutable, already-published objects."""
    if isinstance(value, dict):
        if value.get("kind") == "file" and "ref" in value:
            file = File.model_validate(value)
            if file.ref.workspace.startswith("local-ws:v2:"):
                _import_external_commit(store, file.ref.workspace, known)
                return value
            root = LocalReferenceBinder().bind_workspace(
                Workspace(reference=file.ref.workspace)
            )
            committed = store.commit(root)
            return {**value, "ref": {"workspace": committed.reference, "path": file.ref.path}}
        if value.get("kind") == "workspace":
            workspace = Workspace.model_validate(value)
            if workspace.reference is not None and workspace.reference.startswith(
                "local-ws:v2:"
            ):
                _import_external_commit(store, workspace.reference, known)
            return store.ingest(workspace).manifest()
        # Portable workspaces have no kind tag for wire compatibility.
        if set(value) == {"version", "entries"}:
            return store.ingest(Workspace.model_validate(value)).manifest()
        return {name: capture_input(child, store, known) for name, child in value.items()}
    if isinstance(value, list):
        return [capture_input(child, store, known) for child in value]
    return value
