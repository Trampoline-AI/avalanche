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
    """Register a host-local capability without putting the path on the wire."""
    reference = f"local-ws:v1:{uuid4().hex}"
    target = _registry() / reference.removeprefix("local-ws:v1:")
    with target.open("x") as handle:
        os.chmod(target, 0o600)
        json.dump(str(path.resolve(strict=True)), handle)
    return Workspace.from_directory(path.resolve(strict=True), reference=reference)


class LocalReferenceBinder:
    """Resolve only locally registered capabilities; remote refs require a host binder."""

    def bind_workspace(self, reference: Workspace) -> Path:
        identifier = reference.reference
        if identifier is None or not identifier.startswith("local-ws:v1:"):
            raise RuntimeError("workspace reference is not bound; supply a ReferenceBinder")
        token = identifier.removeprefix("local-ws:v1:")
        if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
            raise ValueError("Malformed local workspace reference")
        target = _registry() / token
        with target.open() as handle:
            root = Path(json.load(handle)).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Local workspace reference no longer exists")
        return root

    def bind_file(self, reference: File) -> Path:
        root = self.bind_workspace(Workspace(reference=reference.ref.workspace))
        path = (root / reference.ref.path).resolve(strict=True)
        if not path.is_relative_to(root):
            raise ValueError("File reference escapes its workspace")
        return path


T = TypeVar("T")


def bind_references(value: T, binder: ReferenceBinder) -> T:
    """Bind native refs recursively, preserving models and container types."""
    if isinstance(value, File):
        if isinstance(binder, LocalReferenceBinder) and value._host_path is not None:
            return value
        return value.bind(binder.bind_file(value))
    if isinstance(value, Workspace):
        if not value.is_reference:
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
