"""Portable directory trees used as Avalanche workflow values."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
import tempfile
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr, model_serializer, model_validator

_WORKSPACE_SERIALIZER_CONTEXT_KEY = "__avalanche_operator_workspace_serializer__"
_MANIFEST_VERSION = 1
_current_materialization_owner: ContextVar[_MaterializationOwner | None] = ContextVar(
    "avalanche_workspace_materialization_owner",
    default=None,
)
_CAPTURE_READ_SIZE = 1024 * 1024
# Keep capture trees within the CLI's bounded result-materialization cleanup
# contract. A directory consumes one additional cleanup level for its contents.
_MAX_WORKSPACE_CAPTURE_DEPTH = 8
_DESCRIPTOR_ANCHORED_CAPTURE_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.listdir in os.supports_fd
)


@dataclass
class _CaptureDirectory:
    descriptor: int
    relative: str
    name: str | None
    parent_descriptor: int | None
    metadata: os.stat_result
    names: tuple[str, ...]
    next_index: int = 0


def _capture_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _capture_entry_label(relative: str) -> str:
    return f"entry {relative!r}" if relative else "root directory"


def _capture_changed(relative: str) -> ValueError:
    return ValueError(
        f"Workspace {_capture_entry_label(relative)} changed while being captured"
    )


def _require_capture_depth(relative: str, *, directory: bool) -> None:
    path_depth = len(PurePosixPath(relative).parts)
    cleanup_depth = path_depth + directory
    if cleanup_depth > _MAX_WORKSPACE_CAPTURE_DEPTH:
        raise ValueError(
            "Workspace source exceeds the capture depth limit of "
            f"{_MAX_WORKSPACE_CAPTURE_DEPTH}"
        )


def _require_descriptor_anchored_capture() -> None:
    if not _DESCRIPTOR_ANCHORED_CAPTURE_SUPPORTED:
        raise RuntimeError(
            "Workspace capture requires descriptor-anchored no-follow filesystem support"
        )


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_open_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_capture_directory(
    path: str | Path,
    *,
    relative: str,
    expected: os.stat_result,
    parent_descriptor: int | None = None,
) -> tuple[int, os.stat_result]:
    try:
        if parent_descriptor is None:
            descriptor = os.open(path, _directory_open_flags())
        else:
            descriptor = os.open(
                path,
                _directory_open_flags(),
                dir_fd=parent_descriptor,
            )
    except OSError as error:
        raise _capture_changed(relative) from error
    try:
        opened = os.fstat(descriptor)
        if _capture_metadata(opened) != _capture_metadata(expected):
            raise _capture_changed(relative)
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _capture_directory_names(descriptor: int, relative: str) -> tuple[str, ...]:
    try:
        return tuple(sorted(os.listdir(descriptor)))
    except OSError as error:
        raise ValueError(
            f"Workspace {_capture_entry_label(relative)} could not be read safely"
        ) from error


def _stat_capture_entry(
    name: str,
    *,
    relative: str,
    parent_descriptor: int,
) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as error:
        raise _capture_changed(relative) from error


def _capture_file(
    name: str,
    *,
    relative: str,
    parent_descriptor: int,
    expected: os.stat_result,
) -> bytes:
    try:
        descriptor = os.open(name, _file_open_flags(), dir_fd=parent_descriptor)
    except OSError as error:
        raise _capture_changed(relative) from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _capture_metadata(opened) != _capture_metadata(
            expected
        ):
            raise _capture_changed(relative)

        content = bytearray()
        maximum_size = opened.st_size + 1
        while len(content) < maximum_size:
            try:
                chunk = os.read(
                    descriptor,
                    min(_CAPTURE_READ_SIZE, maximum_size - len(content)),
                )
            except OSError as error:
                raise ValueError(
                    f"Workspace file {relative!r} could not be read safely"
                ) from error
            if not chunk:
                break
            content.extend(chunk)

        after = os.fstat(descriptor)
        linked = _stat_capture_entry(
            name,
            relative=relative,
            parent_descriptor=parent_descriptor,
        )
        if (
            len(content) != opened.st_size
            or _capture_metadata(after) != _capture_metadata(opened)
            or _capture_metadata(linked) != _capture_metadata(opened)
        ):
            raise _capture_changed(relative)
        return bytes(content)
    finally:
        os.close(descriptor)


def _verify_capture_directory(frame: _CaptureDirectory) -> None:
    if _capture_directory_names(frame.descriptor, frame.relative) != frame.names:
        raise _capture_changed(frame.relative)
    after = os.fstat(frame.descriptor)
    if _capture_metadata(after) != _capture_metadata(frame.metadata):
        raise _capture_changed(frame.relative)
    if frame.name is not None and frame.parent_descriptor is not None:
        linked = _stat_capture_entry(
            frame.name,
            relative=frame.relative,
            parent_descriptor=frame.parent_descriptor,
        )
        if _capture_metadata(linked) != _capture_metadata(frame.metadata):
            raise _capture_changed(frame.relative)


def _verify_capture_root(path: Path, expected: os.stat_result) -> None:
    descriptor, opened = _open_capture_directory(
        path,
        relative="",
        expected=expected,
    )
    try:
        if _capture_metadata(opened) != _capture_metadata(expected):
            raise _capture_changed("")
    finally:
        os.close(descriptor)


def _normalized_path(value: str) -> str:
    if type(value) is not str or not value:
        raise ValueError("Workspace entry path must be a non-empty string")
    if value == ".":
        raise ValueError("Workspace entry path cannot be the root pseudo-path '.'")
    if "\x00" in value:
        raise ValueError("Workspace entry path cannot contain a null byte")
    if "\\" in value:
        raise ValueError("Workspace entry path must use '/' separators")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Workspace entry path is unsafe: {value!r}")
    if str(path) != value:
        raise ValueError(f"Workspace entry path is not normalized: {value!r}")
    return value


class WorkspaceEntry(BaseModel):
    """One regular file or explicit directory in a portable workspace tree."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    kind: Literal["directory", "file"]
    content: bytes | None = None
    sha256: str | None = None

    @model_validator(mode="after")
    def _validate_entry(self) -> "WorkspaceEntry":
        object.__setattr__(self, "path", _normalized_path(self.path))
        if self.kind == "directory":
            if self.content is not None or self.sha256 is not None:
                raise ValueError("Workspace directories cannot carry content")
            return self
        if type(self.content) is not bytes:
            raise ValueError("Workspace files must carry bytes content")
        digest = hashlib.sha256(self.content).hexdigest()
        if self.sha256 is not None:
            if type(self.sha256) is not str or self.sha256.lower() != digest:
                raise ValueError("Workspace file sha256 does not match content")
        object.__setattr__(self, "sha256", digest)
        return self

    def manifest_item(self) -> dict[str, str]:
        if self.kind == "directory":
            if self.content is not None or self.sha256 is not None:
                raise ValueError("Workspace directories cannot carry content")
            return {"kind": "directory", "path": self.path}
        if (
            self.kind != "file"
            or type(self.content) is not bytes
            or type(self.sha256) is not str
        ):
            raise ValueError("Malformed workspace file entry")
        return {
            "kind": "file",
            "path": self.path,
            "content": base64.b64encode(self.content).decode("ascii"),
            "sha256": self.sha256,
        }


class Workspace(BaseModel):
    """A portable recursive collection of regular files.

    ``path`` exists only while an executor is running user node code. The
    serialized value is the manifest and file contents, never that local path.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        strict=True,
    )

    entries: tuple[WorkspaceEntry, ...] = ()
    _materialized_path: Path | None = PrivateAttr(default=None)
    _materialized_identity: tuple[int, int] | None = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def _decode_manifest(cls, value: Any) -> Any:
        if isinstance(value, cls):
            return value
        if type(value) is not dict or "version" not in value:
            return value
        if (
            set(value) != {"version", "entries"}
            or type(value["version"]) is not int
            or value["version"] != _MANIFEST_VERSION
        ):
            raise ValueError("Unsupported workspace manifest")
        raw_entries = value["entries"]
        if type(raw_entries) is not list:
            raise ValueError("Workspace manifest entries must be a list")
        entries: list[WorkspaceEntry] = []
        for item in raw_entries:
            if type(item) is not dict:
                raise ValueError("Workspace manifest entry must be an object")
            if item.get("kind") == "directory" and set(item) == {"kind", "path"}:
                entries.append(WorkspaceEntry(path=item["path"], kind="directory"))
            elif item.get("kind") == "file" and set(item) == {
                "kind",
                "path",
                "content",
                "sha256",
            }:
                if type(item["content"]) is not str:
                    raise ValueError("Workspace file content must be a base64 string")
                if type(item["sha256"]) is not str:
                    raise ValueError("Workspace file sha256 must be a string")
                try:
                    content = base64.b64decode(item["content"], validate=True)
                except (TypeError, ValueError) as exc:
                    raise ValueError("Workspace file content is not valid base64") from exc
                entries.append(
                    WorkspaceEntry(
                        path=item["path"], kind="file", content=content, sha256=item["sha256"]
                    )
                )
            else:
                raise ValueError("Malformed workspace manifest entry")
        return {"entries": tuple(entries)}

    @model_validator(mode="after")
    def _validate_tree(self) -> "Workspace":
        paths: dict[str, WorkspaceEntry] = {}
        for entry in self.entries:
            if entry.path in paths:
                raise ValueError(f"Duplicate workspace entry {entry.path!r}")
            paths[entry.path] = entry
        for path, entry in paths.items():
            for parent in PurePosixPath(path).parents:
                parent_entry = paths.get(str(parent))
                if parent == PurePosixPath("."):
                    continue
                if parent_entry is None:
                    raise ValueError(f"Workspace entry is missing directory {str(parent)!r}")
                if parent_entry.kind != "directory":
                    raise ValueError(f"Workspace file collides with descendant {path!r}")
        entries = tuple(sorted(self.entries, key=lambda item: item.path))
        object.__setattr__(self, "entries", entries)
        return self

    @classmethod
    def from_path(cls, path: str | Path) -> "Workspace":
        root = Path(path)
        _require_descriptor_anchored_capture()
        try:
            metadata = root.stat(follow_symlinks=False)
        except OSError as error:
            raise ValueError("Workspace source directory could not be inspected") from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Workspace source must be a directory")
        root_descriptor, metadata = _open_capture_directory(
            root,
            relative="",
            expected=metadata,
        )
        entries: list[WorkspaceEntry] = []
        try:
            root_names = _capture_directory_names(root_descriptor, "")
        except BaseException:
            os.close(root_descriptor)
            raise
        stack = [
            _CaptureDirectory(
                descriptor=root_descriptor,
                relative="",
                name=None,
                parent_descriptor=None,
                metadata=metadata,
                names=root_names,
            )
        ]
        try:
            while stack:
                directory = stack[-1]
                if directory.next_index == len(directory.names):
                    _verify_capture_directory(directory)
                    if directory.parent_descriptor is None:
                        _verify_capture_root(root, directory.metadata)
                    os.close(directory.descriptor)
                    stack.pop()
                    continue

                name = directory.names[directory.next_index]
                directory.next_index += 1
                relative = f"{directory.relative}/{name}" if directory.relative else name
                metadata = _stat_capture_entry(
                    name,
                    relative=relative,
                    parent_descriptor=directory.descriptor,
                )
                if stat.S_ISDIR(metadata.st_mode):
                    _require_capture_depth(relative, directory=True)
                    entries.append(WorkspaceEntry(path=relative, kind="directory"))
                    child_descriptor, metadata = _open_capture_directory(
                        name,
                        relative=relative,
                        expected=metadata,
                        parent_descriptor=directory.descriptor,
                    )
                    try:
                        names = _capture_directory_names(child_descriptor, relative)
                    except BaseException:
                        os.close(child_descriptor)
                        raise
                    stack.append(
                        _CaptureDirectory(
                            descriptor=child_descriptor,
                            relative=relative,
                            name=name,
                            parent_descriptor=directory.descriptor,
                            metadata=metadata,
                            names=names,
                        )
                    )
                elif stat.S_ISREG(metadata.st_mode):
                    _require_capture_depth(relative, directory=False)
                    entries.append(
                        WorkspaceEntry(
                            path=relative,
                            kind="file",
                            content=_capture_file(
                                name,
                                relative=relative,
                                parent_descriptor=directory.descriptor,
                                expected=metadata,
                            ),
                        )
                    )
                else:
                    raise ValueError(f"Workspace contains unsupported entry {relative!r}")
        finally:
            for directory in reversed(stack):
                os.close(directory.descriptor)
        return cls(entries=tuple(entries))

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> "Workspace":
        if (
            type(manifest) is not dict
            or set(manifest) != {"version", "entries"}
            or type(manifest["version"]) is not int
            or manifest["version"] != _MANIFEST_VERSION
        ):
            raise ValueError("Unsupported workspace manifest")
        return cls.model_validate(manifest)

    def manifest(self) -> dict[str, Any]:
        manifest = {
            "version": _MANIFEST_VERSION,
            "entries": [entry.manifest_item() for entry in self.entries],
        }
        # Re-parse the transport representation so even deliberately constructed
        # or corrupted Pydantic instances fail before any filesystem operation.
        type(self).from_manifest(manifest)
        return manifest

    @model_serializer(mode="wrap")
    def _serialize(self, handler, info):
        context = info.context
        serializer = None
        if isinstance(context, dict):
            serializer = context.get(_WORKSPACE_SERIALIZER_CONTEXT_KEY)
        if callable(serializer):
            return serializer(self)
        return self._manifest_for_serialization()

    @property
    def path(self) -> Path:
        owner = _current_materialization_owner.get()
        if owner is None:
            raise RuntimeError(
                "Workspace.path is available only during Avalanche node execution"
            )
        if self._materialized_path is None:
            validated = type(self).from_manifest(self.manifest())
            root = Path(tempfile.mkdtemp(prefix="avalanche-workspace-"))
            metadata = root.stat(follow_symlinks=False)
            self._materialized_path = root
            self._materialized_identity = (metadata.st_dev, metadata.st_ino)
            owner.register(self)
            try:
                for entry in validated.entries:
                    target = root / entry.path
                    if entry.kind == "directory":
                        target.mkdir(parents=True, exist_ok=False)
                    else:
                        if type(entry.content) is not bytes:
                            raise ValueError("Workspace files must carry bytes content")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(entry.content)
            except BaseException:
                self._cleanup_materialization()
                raise
        return self._materialized_path

    def snapshot(self) -> "Workspace":
        """Capture writes made through ``path`` for the next executor boundary."""
        if self._materialized_path is None:
            return type(self).from_manifest(self.manifest())
        return type(self).from_path(self._materialized_path)

    def _manifest_for_serialization(self) -> dict[str, Any]:
        """Capture a live materialization, then deterministically release it."""
        if self._materialized_path is None:
            return self.manifest()
        primary_error: BaseException | None = None
        try:
            snapshot = self.snapshot()
            manifest = snapshot.manifest()
            object.__setattr__(self, "entries", snapshot.entries)
            return manifest
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                self._cleanup_materialization()
            except BaseException as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(
                    "Workspace cleanup failed while preserving serialization error: "
                    f"{cleanup_error!r}"
                )

    def _cleanup_materialization(self) -> None:
        root = self._materialized_path
        identity = self._materialized_identity
        if root is None:
            return
        try:
            metadata = root.stat(follow_symlinks=False)
        except FileNotFoundError:
            self._materialized_path = None
            self._materialized_identity = None
            return
        if identity is None or (metadata.st_dev, metadata.st_ino) != identity:
            raise RuntimeError("Workspace materialization identity changed before cleanup")
        shutil.rmtree(root)
        self._materialized_path = None
        self._materialized_identity = None

    def __getstate__(self):
        self._manifest_for_serialization()
        state = super().__getstate__()
        private = dict(state.get("__pydantic_private__") or {})
        private["_materialized_path"] = None
        private["_materialized_identity"] = None
        state["__pydantic_private__"] = private
        return state


class _MaterializationOwner:
    """Own every temporary workspace first materialized during one invocation."""

    def __init__(self) -> None:
        self._workspaces: list[Workspace] = []
        self._workspace_ids: set[int] = set()

    def register(self, workspace: Workspace) -> None:
        identity = id(workspace)
        if identity not in self._workspace_ids:
            self._workspace_ids.add(identity)
            self._workspaces.append(workspace)

    def cleanup(self) -> None:
        errors: list[BaseException] = []
        for workspace in reversed(self._workspaces):
            try:
                workspace._cleanup_materialization()
            except BaseException as error:
                errors.append(error)
        if errors:
            error = RuntimeError("Failed to clean up an invocation workspace")
            for cleanup_error in errors:
                error.add_note(repr(cleanup_error))
            raise error


def _copy_workspaces_for_invocation(value: Any, memo: dict[int, Any]) -> Any:
    """Copy portable values so one invocation cannot mutate another's tree."""
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if isinstance(value, Workspace):
        copied = type(value).from_manifest(value._manifest_for_serialization())
        memo[identity] = copied
        return copied
    from .types import LineagedResult

    if isinstance(value, LineagedResult):
        copied_value = _copy_workspaces_for_invocation(value.value, memo)
        copied = (
            value
            if copied_value is value.value
            else LineagedResult(copied_value, dict(value.lineage_vector))
        )
        memo[identity] = copied
        return copied
    if isinstance(value, tuple):
        items = tuple(_copy_workspaces_for_invocation(item, memo) for item in value)
        copied = (
            value if all(item is original for item, original in zip(items, value)) else items
        )
        memo[identity] = copied
        return copied
    if isinstance(value, list):
        items = [_copy_workspaces_for_invocation(item, memo) for item in value]
        copied = (
            value if all(item is original for item, original in zip(items, value)) else items
        )
        memo[identity] = copied
        return copied
    if isinstance(value, dict):
        items = {
            key: _copy_workspaces_for_invocation(item, memo) for key, item in value.items()
        }
        copied = (
            value if all(items[key] is original for key, original in value.items()) else items
        )
        memo[identity] = copied
        return copied
    if isinstance(value, BaseModel):
        updates = {
            field_name: _copy_workspaces_for_invocation(getattr(value, field_name), memo)
            for field_name in type(value).model_fields
        }
        copied = (
            value
            if all(updates[name] is getattr(value, name) for name in updates)
            else value.model_copy(update=updates)
        )
        memo[identity] = copied
        return copied
    return value


def run_workspace_invocation(fn, /, *args: Any, **kwargs: Any) -> Any:
    """Isolate workspaces for one executor call and release all owned trees."""
    owner = _MaterializationOwner()
    token = _current_materialization_owner.set(owner)
    primary_error: BaseException | None = None
    try:
        memo: dict[int, Any] = {}
        isolated_args = _copy_workspaces_for_invocation(args, memo)
        isolated_kwargs = _copy_workspaces_for_invocation(kwargs, memo)
        return snapshot_workspaces(fn(*isolated_args, **isolated_kwargs))
    except BaseException as error:
        primary_error = error
        raise
    finally:
        _current_materialization_owner.reset(token)
        try:
            owner.cleanup()
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                "Workspace cleanup failed while preserving invocation error: "
                f"{cleanup_error!r}"
            )


def snapshot_workspaces(value: Any) -> Any:
    """Capture every workspace in a supported workflow return shape."""
    if isinstance(value, Workspace):
        return value.snapshot()
    from .types import LineagedResult

    if isinstance(value, LineagedResult):
        snapshotted = snapshot_workspaces(value.value)
        return (
            value
            if snapshotted is value.value
            else LineagedResult(snapshotted, dict(value.lineage_vector))
        )
    if isinstance(value, tuple):
        items = tuple(snapshot_workspaces(item) for item in value)
        return value if all(item is original for item, original in zip(items, value)) else items
    if isinstance(value, list):
        items = [snapshot_workspaces(item) for item in value]
        return value if all(item is original for item, original in zip(items, value)) else items
    if isinstance(value, dict):
        items = {key: snapshot_workspaces(item) for key, item in value.items()}
        if all(items[key] is original for key, original in value.items()):
            return value
        return items
    if isinstance(value, BaseModel):
        updates = {
            field_name: snapshot_workspaces(getattr(value, field_name))
            for field_name in type(value).model_fields
        }
        if all(updates[name] is getattr(value, name) for name in updates):
            return value
        return value.model_copy(update=updates)
    return value
