"""Operator-project pins and run grants, journaled with starts/send receipts."""

from __future__ import annotations

import os
import shutil
import sqlite3
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from avalanche.file import File
from avalanche.references import LocalWorkspaceStore, register_run_store
from avalanche.workspace import Workspace

if TYPE_CHECKING:
    from .store import OperatorStateStore


def configure_run(connection: sqlite3.Connection, run_id: str) -> LocalWorkspaceStore:
    database = connection.execute("PRAGMA database_list").fetchone()[2]
    root = (
        Path(database).parent / "workspaces"
        if database
        else LocalWorkspaceStore.for_run().root / "operator-memory"
    )
    register_run_store(run_id, root)
    store = LocalWorkspaceStore(root, run_id)
    connection.execute(
        "INSERT OR IGNORE INTO workspace_runs VALUES (?, ?)",
        (run_id, store.run_root.name),
    )
    return store


def index_run_commits(connection: sqlite3.Connection, run_id: str) -> None:
    """Index filesystem publications when the owner records a finished visit/run."""
    store = LocalWorkspaceStore.for_run(run_id)
    issued = store.run_root / "issued"
    if issued.exists():
        for marker in issued.iterdir():
            reference = "local-ws:v2:" + marker.name
            path = LocalWorkspaceStore.resolve(reference)
            connection.execute(
                "INSERT OR IGNORE INTO workspace_commits (reference, path) VALUES (?, ?)",
                (reference, str(path)),
            )
            connection.execute(
                "INSERT OR IGNORE INTO workspace_grants VALUES (?, ?)", (run_id, reference)
            )
    heads = store.run_root / "heads"
    if heads.exists():
        connection.executemany(
            "INSERT INTO workspace_heads VALUES (?, ?, ?) ON CONFLICT(run_id, mount) "
            "DO UPDATE SET reference=excluded.reference",
            ((run_id, head.name, head.read_text()) for head in heads.iterdir()),
        )


def restore_references(connection: sqlite3.Connection) -> None:
    """SQLite is authoritative; the temporary cross-process locator is rebuildable."""
    from avalanche.references import register_commit

    for row in connection.execute(
        "SELECT reference, path FROM workspace_commits WHERE ref_count>0"
    ):
        register_commit(row[0], Path(row[1]))
    for row in connection.execute("SELECT run_id FROM runs WHERE read_state='retained'"):
        configure_run(connection, row[0])


def sweep_workspaces(store: OperatorStateStore) -> None:
    """Sweep unpinned operator-owned trees in bounded retention batches.

    Maintenance holds the database lock, so coordinators cannot publish while
    sweeping. Files go first and index rows second: an interrupted batch remains
    retryable. Directory scans also recover publications/deletes interrupted
    before their SQLite commit. Foreign stores remain their owner's concern.
    """
    if store.path is None:
        return
    root = store.path.parent / "workspaces"
    while True:
        with store.transaction() as connection:
            rows = connection.execute(
                "SELECT reference, path FROM workspace_commits WHERE ref_count=0 LIMIT 128"
            ).fetchall()
            if not rows:
                break
            for reference, path in rows:
                tree = Path(path)
                if tree.parent == root / "commits":
                    _remove_tree(tree)
            connection.executemany(
                "DELETE FROM workspace_commits WHERE reference=? AND ref_count=0",
                ((row[0],) for row in rows),
            )
    for folder, table, column, prefix in (
        (root / "commits", "workspace_commits", "reference", "local-ws:v2:"),
        (root / "runs", "workspace_runs", "directory", ""),
    ):
        if not folder.exists():
            continue
        with os.scandir(folder) as entries:
            for batch in batched(entries, 128):
                keys = tuple(prefix + entry.name for entry in batch)
                with store.transaction() as connection:
                    retained = {
                        row[0]
                        for row in connection.execute(
                            f"SELECT {column} FROM {table} WHERE {column} IN "
                            f"({','.join('?' for _ in keys)})",
                            keys,
                        )
                    }
                for entry in batch:
                    if prefix + entry.name not in retained:
                        _remove_tree(Path(entry.path))


def _remove_tree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass


def grant_references(
    connection: sqlite3.Connection,
    value: JsonValue,
    recipient: str | None,
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
    if value.get("kind") == "file" and "ref" in value:
        file = File.model_validate(value)
        reference = file.ref.workspace
        root = LocalWorkspaceStore.resolve(reference)
        path = (root / file.ref.path).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("Invalid File commit path")
        file.bind(path).snapshot()
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
    if recipient is None:
        # Declared start preflight: the create transaction records its grants.
        return
    connection.execute(
        "INSERT OR IGNORE INTO workspace_commits (reference, path) VALUES (?, ?)",
        (reference, str(root)),
    )
    connection.execute(
        "INSERT OR IGNORE INTO workspace_grants VALUES (?, ?)", (recipient, reference)
    )
