"""Operator-project pins and run grants, journaled with starts/send receipts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import JsonValue

from avalanche.file import File
from avalanche.references import LocalWorkspaceStore, register_run_store
from avalanche.workspace import Workspace


def configure_run(connection: sqlite3.Connection, run_id: str) -> LocalWorkspaceStore:
    database = connection.execute("PRAGMA database_list").fetchone()[2]
    root = (
        Path(database).parent / "workspaces"
        if database
        else LocalWorkspaceStore.for_run().root / "operator-memory"
    )
    register_run_store(run_id, root)
    return LocalWorkspaceStore(root, run_id)


def grant_references(
    connection: sqlite3.Connection,
    value: JsonValue,
    recipient: str,
    *,
    sender: str | None = None,
    ingress: bool = False,
) -> None:
    """Authorize every ref before persisting any accepted receipt.

    A public send is scoped to commits already pinned by this operator. A
    declared operation additionally requires a sender grant or a commit produced
    by that sender's run-owned workspace. Local host access is not a sandbox.
    """
    if isinstance(value, list):
        for child in value:
            grant_references(connection, child, recipient, sender=sender, ingress=ingress)
        return
    if not isinstance(value, dict):
        return
    if value.get("kind") == "file":
        file = File.model_validate(value)
        reference = file.ref.workspace
        root = LocalWorkspaceStore.resolve(reference)
        path = (root / file.ref.path).resolve(strict=True)
        if not path.is_relative_to(root) or path.stat().st_size != file.size:
            raise ValueError("Invalid File commit path or size")
    elif value.get("kind") == "workspace":
        workspace = Workspace.model_validate(value)
        reference = workspace.reference
        if reference is None:
            raise ValueError("Workspace transfer requires a commit reference")
        root = LocalWorkspaceStore.resolve(reference)
    else:
        for child in value.values():
            grant_references(connection, child, recipient, sender=sender, ingress=ingress)
        return
    pinned = connection.execute(
        "SELECT 1 FROM workspace_commits WHERE reference=?", (reference,)
    ).fetchone()
    if sender is not None:
        allowed = connection.execute(
            "SELECT 1 FROM workspace_grants WHERE run_id=? AND reference=?",
            (sender, reference),
        ).fetchone()
        store = LocalWorkspaceStore.for_run(sender)
        produced = store.run_root / "issued" / reference.removeprefix("local-ws:v2:")
        if allowed is None and not produced.is_file():
            raise ValueError("Sender has no grant for workspace commit")
    elif not ingress and pinned is None:
        raise ValueError("Workspace commit is outside this operator project")
    connection.execute(
        "INSERT OR IGNORE INTO workspace_commits VALUES (?, ?)", (reference, str(root))
    )
    connection.execute(
        "INSERT OR IGNORE INTO workspace_grants VALUES (?, ?)", (recipient, reference)
    )
