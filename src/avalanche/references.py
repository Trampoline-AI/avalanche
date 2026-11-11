"""Host reference binding and declared-type reconstruction at step boundaries."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import is_dataclass
from pathlib import Path
from typing import Annotated, Protocol, TypeVar, get_args, get_origin, is_typeddict
from uuid import uuid4

from pydantic import BaseModel, TypeAdapter

from .file import File
from .workspace import Workspace


class ReferenceBinder(Protocol):
    """Host authorization boundary. Never trust reference paths without authorization."""

    def bind_file(self, reference: File) -> Path:
        """Authorize a snapshot/path and return its existing host file."""
        ...

    def bind_workspace(self, reference: Workspace) -> Path:
        """Authorize a snapshot and return its existing host directory."""
        ...


def _registry() -> Path:
    # Local operator processes share the same filesystem, not Python memory.
    root = Path(tempfile.gettempdir()) / f"avalanche-local-refs-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise RuntimeError("Unsafe local reference registry")
    return root


def local_workspace(path: Path) -> Workspace:
    """Publish an immutable local commit; the wire never contains a host path."""
    return LocalWorkspaceStore.for_run().commit(path)


def register_run_store(run_id: str, root: Path) -> None:
    """Publish process-local host configuration for spawned coordinators."""
    target = _registry() / ("run-" + hashlib.sha256(run_id.encode()).hexdigest())
    temporary = target.with_suffix("." + uuid4().hex)
    temporary.write_text(str(root.resolve()))
    temporary.replace(target)


class LocalWorkspaceStore:
    """Immutable content-addressed trees and run-owned CoW working directories.

    Only clone materialization falls back to a byte copy. Passing an existing
    commit publishes metadata, never another tree. Hosts own authorization.
    """

    def __init__(self, root: Path, run_id: str | None = None) -> None:
        self.root = root.resolve()
        self.run_id = run_id
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_run(cls, run_id: str | None = None) -> LocalWorkspaceStore:
        from .runtime.context import get_current_run_context

        context = get_current_run_context()
        run_id = run_id or (context.run_id if context is not None else None)
        config = (
            _registry() / ("run-" + hashlib.sha256(run_id.encode()).hexdigest())
            if run_id is not None
            else None
        )
        root = (
            Path(config.read_text())
            if config is not None and config.exists()
            else _registry() / "workspaces"
        )
        return cls(root, run_id)

    @staticmethod
    def resolve(reference: str) -> Path:
        digest = reference.removeprefix("local-ws:v2:")
        if (
            not reference.startswith("local-ws:v2:")
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("Malformed local workspace commit")
        target = _registry() / ("commit-" + digest)
        return Path(target.read_text()).resolve(strict=True)

    def commit(self, path: Path) -> Workspace:
        import shutil
        import stat

        path = path.resolve(strict=True)
        digest = hashlib.sha256()
        for directory, dirs, files in os.walk(path, followlinks=False):
            for name in sorted(dirs + files):
                entry = Path(directory) / name
                metadata = entry.lstat()
                relative = entry.relative_to(path).as_posix()
                if stat.S_ISDIR(metadata.st_mode):
                    item = [relative, "directory", metadata.st_mode & 0o777]
                elif stat.S_ISREG(metadata.st_mode):
                    with entry.open("rb") as handle:
                        content = hashlib.file_digest(handle, "sha256").hexdigest()
                    item = [relative, "file", metadata.st_mode & 0o777, content]
                else:
                    raise ValueError(f"Unsupported workspace entry: {relative}")
                digest.update(json.dumps(item, separators=(",", ":")).encode() + b"\n")
            dirs.sort()
        identity = digest.hexdigest()
        reference = "local-ws:v2:" + identity
        destination = self.root / "commits" / identity
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Reuse an already published object, including one from another local
        # store. Its host scope is checked by the operator, not this locator.
        locator = _registry() / ("commit-" + identity)
        if locator.exists():
            destination = self.resolve(reference)
        elif not destination.exists():
            temporary = destination.with_name("." + uuid4().hex)
            try:
                shutil.copytree(path, temporary, copy_function=_clone_file)
                # Files are flushed before publishing the immutable directory.
                for directory, _, files in os.walk(temporary):
                    for name in files:
                        with (Path(directory) / name).open("rb") as handle:
                            os.fsync(handle.fileno())
                temporary.rename(destination)
            except FileExistsError:
                shutil.rmtree(temporary)
        temporary_locator = locator.with_suffix("." + uuid4().hex)
        temporary_locator.write_text(str(destination))
        temporary_locator.replace(locator)
        if self.run_id is not None:
            issued = self.run_root / "issued"
            issued.mkdir(parents=True, exist_ok=True)
            (issued / identity).touch()
        return Workspace.from_directory(destination, reference=reference)

    def ingest(self, workspace: Workspace) -> Workspace:
        """Capture portable/v1 ingress once; v2 values already identify commits."""
        if workspace.reference is not None:
            if workspace.reference.startswith("local-ws:v2:"):
                return Workspace.from_directory(
                    self.resolve(workspace.reference), reference=workspace.reference
                )
            return self.commit(LocalReferenceBinder().bind_workspace(workspace))
        with tempfile.TemporaryDirectory(prefix="avalanche-ingress-") as folder:
            root = Path(folder)
            for entry in workspace.entries:
                target = root / entry.path
                if entry.kind == "directory":
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if entry.content is None:
                        raise ValueError("Workspace file is missing content")
                    target.write_bytes(entry.content)
            return self.commit(root)

    @property
    def run_root(self) -> Path:
        if self.run_id is None:
            raise RuntimeError("A mutable workspace needs a run identity")
        return self.root / "runs" / hashlib.sha256(self.run_id.encode()).hexdigest()

    def mount(self, reference: str) -> Path:
        import shutil

        source = self.resolve(reference)
        if self.run_id is None:
            return source
        target = self.run_root / "mounts" / reference.removeprefix("local-ws:v2:")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temporary = target.with_name("." + uuid4().hex)
            shutil.copytree(source, temporary, copy_function=_clone_file)
            try:
                temporary.rename(target)
            except FileExistsError:
                shutil.rmtree(temporary)
        return target

    def commit_mounts(self) -> tuple[str, ...]:
        mounts = self.run_root / "mounts"
        if not mounts.exists():
            return ()
        return tuple(self.commit(path).reference for path in sorted(mounts.iterdir()))


def _clone_file(source: str, destination: str) -> str:
    """Native CoW where available; copying is confined to materialization."""
    import errno
    import shutil
    import sys

    try:
        if sys.platform == "darwin":
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            if libc.clonefile(os.fsencode(source), os.fsencode(destination), 0) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        elif sys.platform == "linux":
            import fcntl

            with open(source, "rb") as reader, open(destination, "xb") as writer:
                fcntl.ioctl(writer.fileno(), 0x40049409, reader.fileno())  # FICLONE
        else:
            raise OSError(errno.ENOTSUP, "No clone primitive")
    except OSError as error:
        if error.errno not in {errno.ENOTSUP, errno.EXDEV, errno.EINVAL, errno.ENOSYS}:
            raise
        shutil.copyfile(source, destination)
    shutil.copystat(source, destination)
    return destination


def restore_local_workspace(reference: str, path: Path) -> Workspace:
    """Register ``path`` again under a reference ``local_workspace`` issued earlier.

    The registry lives in the system temporary folder, which a reboot may clear,
    while a folder kept elsewhere outlives it. An entry that still exists must
    already name the same folder.
    """
    if reference.startswith("local-ws:v2:"):
        restored = LocalWorkspaceStore.for_run().commit(path)
        if restored.reference != reference:
            raise ValueError("Stored workspace commit differs from its reference")
        return restored
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


def _local_workspace_token(identifier: str) -> str:
    if not identifier.startswith("local-ws:v1:"):
        raise RuntimeError("workspace reference is not bound; supply a ReferenceBinder")
    token = identifier.removeprefix("local-ws:v1:")
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("Malformed local workspace reference")
    return token


class LocalReferenceBinder:
    """Resolve only locally registered capabilities; remote refs require a host binder."""

    def bind_workspace(self, reference: Workspace) -> Path:
        identifier = reference.reference
        if identifier is None:
            raise RuntimeError("workspace reference is not bound; supply a ReferenceBinder")
        if identifier.startswith("local-ws:v2:"):
            return LocalWorkspaceStore.for_run().mount(identifier)
        target = _registry() / _local_workspace_token(identifier)
        with target.open() as handle:
            root = Path(json.load(handle)).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Local workspace reference no longer exists")
        return root

    def bind_file(self, reference: File) -> Path:
        root = (
            LocalWorkspaceStore.resolve(reference.ref.workspace)
            if reference.ref.workspace.startswith("local-ws:v2:")
            else self.bind_workspace(Workspace(reference=reference.ref.workspace))
        )
        path = (root / reference.ref.path).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("File reference escapes its workspace")
        return path


T = TypeVar("T")


def bind_references(value: T, binder: ReferenceBinder) -> T:
    """Bind native refs recursively, preserving models and container types."""
    if isinstance(value, File):
        store = LocalWorkspaceStore.for_run()
        if (
            isinstance(binder, LocalReferenceBinder)
            and store.run_id is not None
            and value.ref.workspace.startswith("local-ws:v1:")
        ):
            root = binder.bind_workspace(Workspace(reference=value.ref.workspace))
            committed = store.commit(root)
            return File.from_path(committed.path / value.ref.path, workspace=committed)
        if isinstance(binder, LocalReferenceBinder) and value._host_path is not None:
            return value
        return value.bind(binder.bind_file(value))
    if isinstance(value, Workspace):
        if not value.is_reference:
            store = LocalWorkspaceStore.for_run()
            if store.run_id is None:
                return value
            committed = store.ingest(value)
            return Workspace.from_directory(
                binder.bind_workspace(committed), reference=committed.reference
            )
        if (
            isinstance(binder, LocalReferenceBinder)
            and value._directory_path is not None
            and not value.reference.startswith("local-ws:v2:")
        ):
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
    _ = workspace.path
    if not workspace.is_reference:
        raise ValueError("Agent output workspace must be a bound reference Workspace")
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
        run_id = context.run_id if context is not None else uuid4().hex
        run_key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        root = _registry() / "runs" / run_key
        root.mkdir(parents=True, exist_ok=True)
        workspace = local_workspace(root)
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
