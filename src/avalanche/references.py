"""Host reference binding and declared-type reconstruction at step boundaries."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import is_dataclass
from pathlib import Path
from typing import Annotated, Protocol, TypeVar, get_args, get_origin, is_typeddict
from uuid import uuid4

from pydantic import BaseModel, JsonValue, TypeAdapter

from .file import File
from .workspace import Workspace, is_inline_tree, write_inline_tree


class ReferenceBinder(Protocol):
    """Host authorization boundary. Never trust reference paths without authorization."""

    def bind_file(self, reference: File) -> Path:
        """Authorize a snapshot/path and return its existing host file."""
        ...

    def bind_workspace(self, reference: Workspace) -> Path:
        """Authorize a snapshot and return its existing host directory."""
        ...

    def run_workspace(self) -> Workspace:
        """Return the current run's workspace, bound to its host directory."""
        ...


def _registry() -> Path:
    # Local operator processes share the same filesystem, not Python memory.
    root = Path(tempfile.gettempdir()) / f"avalanche-local-refs-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise RuntimeError("Unsafe local reference registry")
    return root


LOCAL_WORKSPACE_ROOT_ENV = "AVALANCHE_LOCAL_WORKSPACE_ROOT"
"""The folder a local host keeps its workspaces in; its child processes inherit it."""
RUN_WORKSPACE_FOLDER = "runs"
STORED_TREE_FOLDER = "stored-trees"
CAPTURED_FOLDER = "captures"
_STORE_FOLDERS = (RUN_WORKSPACE_FOLDER, STORED_TREE_FOLDER, CAPTURED_FOLDER)
_reader_store: ContextVar[Path | None] = ContextVar("avalanche_reader_store", default=None)


def local_workspace_root() -> Path:
    """The store where this host keeps the workspaces it owns.

    A host that keeps runs sets ``AVALANCHE_LOCAL_WORKSPACE_ROOT`` to a folder
    that lasts as long as its records. Without it, workspaces live in the system
    temporary folder and last only as long as that does.

    A store is the only record of its workspaces: a reference names a folder
    inside it by token, so moving or copying the store keeps every reference
    valid and nothing outside it can disagree.
    """
    reading = _reader_store.get()
    if reading is not None:
        return reading
    configured = os.environ.get(LOCAL_WORKSPACE_ROOT_ENV)
    return _registry() if not configured else Path(configured).resolve()


@contextmanager
def reading_workspace_store(root: Path | None):
    """Resolve references against a host's store while reading values it returned.

    A client on the same machine as an operator uses the store folder the
    operator reports, so it binds that operator's workspaces and no other's.
    """
    token = _reader_store.set(None if root is None else root.resolve())
    try:
        yield
    finally:
        _reader_store.reset(token)


def _store_folder(token: str) -> Path | None:
    """The folder a store keeps for ``token``: the current store, else the default one."""
    for root in dict.fromkeys((local_workspace_root(), _registry())):
        for kind in _STORE_FOLDERS:
            folder = root / kind / token
            if folder.is_dir() and not folder.is_symlink():
                return folder
    return None


def _store_workspace(kind: str, token: str) -> Workspace:
    return Workspace.from_directory(
        local_workspace_root() / kind / token, reference=f"local-ws:v1:{token}"
    )


def local_workspace(path: Path) -> Workspace:
    """Register a host-local capability without putting the path on the wire."""
    reference = f"local-ws:v1:{uuid4().hex}"
    target = _registry() / reference.removeprefix("local-ws:v1:")
    with target.open("x") as handle:
        os.chmod(target, 0o600)
        json.dump(str(path.resolve(strict=True)), handle)
    return Workspace.from_directory(path.resolve(strict=True), reference=reference)


def restore_local_workspace(reference: str, path: Path) -> Workspace:
    """Register ``path`` again under a reference ``local_workspace`` issued earlier.

    The registry lives in the system temporary folder, which a reboot may clear,
    while a folder kept elsewhere outlives it. An entry that still exists must
    already name the same folder.
    """
    token = _local_workspace_token(reference)
    root = path.resolve(strict=True)
    target = _registry() / token
    try:
        with target.open("x") as handle:
            os.chmod(target, 0o600)
            json.dump(str(root), handle)
    except FileExistsError:
        with target.open() as handle:
            registered = Path(json.load(handle))
        if registered != root:
            raise ValueError(
                f"Local workspace reference {reference} is registered for {registered}, "
                f"not {root}"
            ) from None
    return Workspace.from_directory(root, reference=reference)


def capture_local_workspace(path: Path) -> Workspace:
    """Copy ``path`` once into this host's store and return the copy's reference."""
    if not path.is_dir() or path.is_symlink():
        raise ValueError("Workspace source must be a directory")
    root = local_workspace_root() / CAPTURED_FOLDER
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = uuid4().hex
    shutil.copytree(path, root / token, symlinks=True)
    return _store_workspace(CAPTURED_FOLDER, token)


def store_token(identity: str) -> str:
    """The folder name and reference token a store uses for ``identity``."""
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def local_run_workspace(run_id: str) -> Workspace:
    """The one workspace a local host keeps for ``run_id``, created on first use.

    Its folder name follows from the run id alone, so every step, state entry and
    process of the run binds the same directory in the host's store, and the host
    finds it again to remove it with the run.
    """
    token = store_token(run_id)
    folder = local_workspace_root() / RUN_WORKSPACE_FOLDER / token
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _store_workspace(RUN_WORKSPACE_FOLDER, token)


def current_run_workspace() -> Workspace:
    """The workspace of the run this code executes in, bound to its directory."""
    workspace = _output_workspace.get()
    if workspace is not None:
        return workspace
    from .runtime.context import get_current_run_context

    context = get_current_run_context()
    if context is None:
        raise RuntimeError("Workspace.path is available only during Avalanche node execution")
    return local_run_workspace(context.run_id)


UNBOUND_RUN_WORKSPACE = "run-workspace:unbound"
"""What a record shows for ``Workspace()`` seen outside any run."""


def run_workspace_reference() -> str:
    """The reference of the current run's workspace, for records such as traces."""
    from .runtime.context import get_current_run_context

    if _output_workspace.get() is None and get_current_run_context() is None:
        return UNBOUND_RUN_WORKSPACE
    reference = current_run_workspace().reference
    if reference is None:
        raise RuntimeError("the host bound the run workspace without a reference")
    return reference


def read_stored_trees(value: JsonValue, *, own_copy: bool = False) -> JsonValue:
    """Replace file trees stored inline by earlier releases with local references.

    A stored result is read through one folder named by the tree's content, so
    reading it again finds the same reference; that folder is for reading. A run
    input passes ``own_copy``: steps edit a workspace in place, so each start
    gets a folder of its own and never sees another run's edits. Only a local
    host calls this, at the boundaries where it reads values it stored or
    accepts run input.
    """
    if isinstance(value, list):
        return [read_stored_trees(item, own_copy=own_copy) for item in value]
    if not isinstance(value, dict):
        return value
    if not is_inline_tree(value):
        return {
            name: read_stored_trees(item, own_copy=own_copy) for name, item in value.items()
        }
    if own_copy:
        kind, token = CAPTURED_FOLDER, uuid4().hex
    else:
        digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        kind, token = STORED_TREE_FOLDER, store_token(digest)
    root = local_workspace_root() / kind
    folder = root / token
    if not folder.is_dir():
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        draft = Path(tempfile.mkdtemp(prefix=f".{token}.", dir=root))
        try:
            write_inline_tree(value, draft)
            try:
                draft.rename(folder)
            except OSError:
                # Another reader published the same tree first.
                if not folder.is_dir():
                    raise
        finally:
            if draft.exists():
                shutil.rmtree(draft)
    return _store_workspace(kind, token).manifest()


def _is_token(token: str) -> bool:
    return len(token) == 32 and all(c in "0123456789abcdef" for c in token)


def _local_workspace_token(identifier: str) -> str:
    if not identifier.startswith("local-ws:v1:"):
        raise RuntimeError("workspace reference is not bound; supply a ReferenceBinder")
    token = identifier.removeprefix("local-ws:v1:")
    if not _is_token(token):
        raise ValueError("Malformed local workspace reference")
    return token


class LocalReferenceBinder:
    """Resolve only locally registered capabilities; remote refs require a host binder."""

    def bind_workspace(self, reference: Workspace) -> Path:
        identifier = reference.reference
        if identifier is None:
            raise RuntimeError("workspace reference is not bound; supply a ReferenceBinder")
        token = _local_workspace_token(identifier)
        # A folder in a host's store needs no registration; other local
        # directories are found through the entry their owner registered.
        kept = _store_folder(token)
        if kept is not None:
            return kept.resolve(strict=True)
        target = _registry() / token
        with target.open() as handle:
            root = Path(json.load(handle)).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Local workspace reference no longer exists")
        return root

    def run_workspace(self) -> Workspace:
        return current_run_workspace()

    def bind_file(self, reference: File) -> Path:
        root = self.bind_workspace(Workspace(reference=reference.ref.workspace))
        path = (root / reference.ref.path).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("File reference escapes its workspace")
        return path


T = TypeVar("T")


def validate_local_references(value) -> None:
    """Use the flow binder for references arriving at a local JSON boundary."""
    if isinstance(value, dict):
        if value.get("kind") == "file" and "ref" in value:
            file = File.model_validate(value)
            file.bind(LocalReferenceBinder().bind_file(file)).snapshot()
        elif value.get("kind") == "workspace":
            workspace = Workspace.model_validate(value)
            if workspace.reference is not None:
                LocalReferenceBinder().bind_workspace(workspace)
        else:
            for child in value.values():
                validate_local_references(child)
    elif isinstance(value, list):
        for child in value:
            validate_local_references(child)


def bind_references(value: T, binder: ReferenceBinder) -> T:
    """Bind native refs recursively, preserving models and container types."""
    if isinstance(value, File):
        if isinstance(binder, LocalReferenceBinder) and value._host_path is not None:
            return value
        return value.bind(binder.bind_file(value))
    if isinstance(value, Workspace):
        if value.reference is None:
            # Workspace() names the run's own workspace, which its path finds.
            return value
        if isinstance(binder, LocalReferenceBinder) and value._directory_path is not None:
            return value
        return Workspace.from_directory(binder.bind_workspace(value), reference=value.reference)
    if isinstance(value, BaseModel):
        updates = {
            name: bind_references(getattr(value, name), binder)
            for name in type(value).model_fields
        }
        if all(updates[name] is getattr(value, name) for name in updates):
            return value
        return value.model_copy(update=updates)
    if isinstance(value, list):
        return [bind_references(item, binder) for item in value]
    if isinstance(value, tuple):
        return tuple(bind_references(item, binder) for item in value)
    if isinstance(value, dict):
        return {key: bind_references(item, binder) for key, item in value.items()}
    return value


def bind_run_workspaces(value: T, binder: ReferenceBinder) -> T:
    """Name each ``Workspace()`` a step returns as the run's workspace reference.

    A stored result then records where the files are, not a placeholder only the
    run itself could resolve.
    """
    if isinstance(value, Workspace):
        return binder.run_workspace() if value.reference is None else value
    if isinstance(value, File):
        return value
    if isinstance(value, BaseModel):
        updates = {
            name: bind_run_workspaces(getattr(value, name), binder)
            for name in type(value).model_fields
        }
        if all(updates[name] is getattr(value, name) for name in updates):
            return value
        return value.model_copy(update=updates)
    if isinstance(value, list):
        return [bind_run_workspaces(item, binder) for item in value]
    if isinstance(value, tuple):
        return tuple(bind_run_workspaces(item, binder) for item in value)
    if isinstance(value, dict):
        return {key: bind_run_workspaces(item, binder) for key, item in value.items()}
    return value


def rebind_arguments(
    fn: Callable,
    resolved_kwargs: Mapping[str, T],
    binder: ReferenceBinder,
) -> dict[str, T]:
    """Rebuild resolved step kwargs from declared annotations and bind all refs.

    Call this for EVERY node after dependency resolution, before invocation.
    It supports File, Workspace, Pydantic models and typed collections. Untyped
    parameters remain unchanged. The binder must enforce snapshot ownership and
    ancestry before returning a path. Existing bound native values are retained.
    """
    signature = inspect.signature(fn)
    if any(isinstance(p.annotation, str) for p in signature.parameters.values()):
        from .step_interface import resolve_step_signature

        signature = resolve_step_signature(fn)
    annotations = {name: p.annotation for name, p in signature.parameters.items()}
    rebuilt = {}
    for name, value in resolved_kwargs.items():
        parameter = signature.parameters.get(name)
        annotation = annotations.get(
            name,
            parameter.annotation if parameter is not None else inspect.Parameter.empty,
        )
        if annotation is not inspect.Parameter.empty:
            if parameter is not None and parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                annotation = tuple[annotation, ...]
            elif parameter is not None and parameter.kind is inspect.Parameter.VAR_KEYWORD:
                annotation = dict[str, annotation]
            base = (
                get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation
            )
            native_dataclass = is_dataclass(base) and isinstance(value, base)
            if not native_dataclass:
                owns_config = (
                    is_dataclass(base)
                    or is_typeddict(base)
                    or (isinstance(base, type) and issubclass(base, BaseModel))
                )
                config = None if owns_config else {"arbitrary_types_allowed": True}
                value = TypeAdapter(annotation, config=config).validate_python(value)
        rebuilt[name] = bind_references(value, binder)
    return rebuilt


_output_workspace: ContextVar[Workspace | None] = ContextVar(
    "file_output_workspace", default=None
)
_output_attempt: ContextVar[str | None] = ContextVar("file_output_attempt", default=None)


@contextmanager
def file_reference_context(workspace: Workspace, *, attempt: str | None = None):
    """Set the run workspace for agent outputs (Delta: bound Mesa workspace).

    Hosts should enter this around every node invocation. A fresh UUID attempt is
    used unless the host supplies its own attempt identifier.
    """
    if workspace.reference is None:
        raise ValueError("Agent output workspace must be a bound reference Workspace")
    _ = workspace.path
    token = _output_workspace.set(workspace)
    attempt_token = _output_attempt.set(attempt)
    try:
        yield
    finally:
        _output_attempt.reset(attempt_token)
        _output_workspace.reset(token)


def agent_output_workspace() -> tuple[Workspace, str]:
    workspace = _output_workspace.get()
    if workspace is None:
        from .runtime.context import get_current_run_context

        context = get_current_run_context()
        # An agent called outside a run still needs a folder for its files.
        workspace = local_run_workspace(context.run_id if context is not None else uuid4().hex)
    return workspace, _output_attempt.get() or uuid4().hex


class AgentOutputFolder:
    """A fresh folder for one agent call's file outputs, inside the run workspace.

    Outputs live under ``.outputs/<step>/<attempt>/<invocation>/``, so multiple calls
    in one node attempt never overwrite each other's files.
    """

    def __init__(self, step_name: str):
        from .runtime.context import get_current_run_context

        self.workspace, attempt = agent_output_workspace()
        context = get_current_run_context()
        step = context.node_slug if context is not None and context.node_slug else step_name
        for component in (step, attempt):
            if (
                not component
                or component in {".", ".."}
                or "/" in component
                or "\\" in component
            ):
                raise ValueError("Agent step and attempt must be single path components")
        self.path = self.workspace.path / ".outputs" / step / attempt / uuid4().hex
        self.path.mkdir(parents=True, exist_ok=False)

    def file(self, path: str | Path) -> File:
        """The public reference for a file the agent wrote inside this folder."""
        host_path = Path(path).resolve(strict=True)
        if not host_path.is_relative_to(self.path.resolve()):
            raise ValueError("Agent File output escaped the reserved step/attempt folder")
        return File.from_path(host_path, workspace=self.workspace)
