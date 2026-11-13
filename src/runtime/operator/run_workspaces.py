"""The store next to a state database that keeps the workspaces of its runs.

Each run that uses ``ava.Workspace()`` gets one folder here, created on first use
and removed when the run is deleted. Directories a client passes as run input are
copied here too. The store is the only record of these folders: a reference names
a folder inside it, so the store can be moved or copied with its database, and a
later start, a later state entry or ``ava result`` still finds the files.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from avalanche.references import (
    CAPTURED_FOLDER,
    LOCAL_WORKSPACE_ROOT_ENV,
    RUN_WORKSPACE_FOLDER,
    STORED_TREE_FOLDER,
    local_workspace_root,
    store_token,
)
from avalanche.workspace import Workspace, safe_relative_path

WORKSPACE_FOLDER_NAME = "workspaces"
MAX_STAGED_WORKSPACE_CHUNK_BYTES = 1024 * 1024


class StagedWorkspaceError(ValueError):
    """A staged input workspace stream the operator cannot accept."""


@dataclass(frozen=True)
class WorkspaceChunk:
    """One directory, or one piece of one file, of a directory a client sends."""

    path: str
    directory: bool
    content: bytes
    end_of_file: bool


@dataclass(frozen=True)
class StagedWorkspace:
    workspace: Workspace
    files: int
    size_bytes: int


def open_workspace_root(database: Path | None) -> Path:
    """Point this operator and the run processes it starts at its workspace store.

    A throwaway ``:memory:`` database keeps workspaces in the system temporary
    folder instead. Returns the store folder.
    """
    if database is None:
        os.environ.pop(LOCAL_WORKSPACE_ROOT_ENV, None)
        return local_workspace_root()
    root = database.resolve().parent / WORKSPACE_FOLDER_NAME
    root.mkdir(mode=0o700, exist_ok=True)
    os.environ[LOCAL_WORKSPACE_ROOT_ENV] = str(root)
    return root


def stage_input_workspace(store: Path, chunks: Iterable[WorkspaceChunk]) -> StagedWorkspace:
    """Write a directory a client streams into the store and return its reference.

    Entries arrive parents first; a file arrives as consecutive chunks. Nothing
    is published until the whole stream is written, and a refused or broken
    stream leaves nothing behind.
    """
    root = store / CAPTURED_FOLDER
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = uuid4().hex
    draft = root / f".{token}.staging"
    draft.mkdir(mode=0o700)
    files = size_bytes = 0
    open_path: str | None = None
    try:
        for chunk in chunks:
            try:
                relative = safe_relative_path(chunk.path)
            except ValueError as exc:
                raise StagedWorkspaceError(str(exc)) from exc
            if len(chunk.content) > MAX_STAGED_WORKSPACE_CHUNK_BYTES:
                raise StagedWorkspaceError(
                    f"Workspace chunk exceeds {MAX_STAGED_WORKSPACE_CHUNK_BYTES} bytes"
                )
            if open_path is not None and (chunk.directory or relative != open_path):
                raise StagedWorkspaceError(f"Workspace file {open_path!r} was not finished")
            target = draft / relative
            try:
                if chunk.directory:
                    if chunk.content or chunk.end_of_file:
                        raise StagedWorkspaceError("A workspace directory carries no content")
                    target.mkdir(mode=0o700)
                    continue
                with target.open("xb" if open_path is None else "ab") as handle:
                    handle.write(chunk.content)
            except (FileNotFoundError, FileExistsError, NotADirectoryError) as exc:
                raise StagedWorkspaceError(
                    f"Workspace entry {relative!r} is out of order or repeated"
                ) from exc
            size_bytes += len(chunk.content)
            open_path = None if chunk.end_of_file else relative
            files += chunk.end_of_file
        if open_path is not None:
            raise StagedWorkspaceError(f"Workspace file {open_path!r} was not finished")
        draft.rename(root / token)
    except BaseException:
        shutil.rmtree(draft, ignore_errors=True)
        raise
    workspace = Workspace.from_directory(root / token, reference=f"local-ws:v1:{token}")
    return StagedWorkspace(workspace=workspace, files=files, size_bytes=size_bytes)


def run_workspace_folders(database: Path) -> Path:
    return database.resolve().parent / WORKSPACE_FOLDER_NAME / RUN_WORKSPACE_FOLDER


def stored_tree_folders(database: Path) -> Path:
    return database.resolve().parent / WORKSPACE_FOLDER_NAME / STORED_TREE_FOLDER


def captured_folders(database: Path) -> Path:
    return database.resolve().parent / WORKSPACE_FOLDER_NAME / CAPTURED_FOLDER


def remove_run_workspace(database: Path, run_id: str) -> bool:
    """Remove the workspace folder of a deleted run; report whether it had one."""
    return remove_workspace_folder(run_workspace_folders(database) / store_token(run_id))


def remove_workspace_folder(folder: Path) -> bool:
    try:
        shutil.rmtree(folder)
    except FileNotFoundError:
        return False
    return True
