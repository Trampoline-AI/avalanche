"""Workspaces: references to directories a host keeps, never file bytes."""

from __future__ import annotations

import base64
import binascii
import hashlib
import warnings
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    PrivateAttr,
    ValidationError,
    model_serializer,
    model_validator,
)

_WORKSPACE_SERIALIZER_CONTEXT_KEY = "__avalanche_operator_workspace_serializer__"
_MANIFEST_VERSION = 1
VALUE_WORKSPACE_UNSUPPORTED = "workspace_value_unsupported"
"""The error a host reports for a workspace that carries file bytes."""


def refuses_value_workspace(error: BaseException) -> bool:
    """Whether ``error`` is a validation failure caused by an inline file tree."""
    return isinstance(error, ValidationError) and any(
        VALUE_WORKSPACE_UNSUPPORTED in detail["msg"] for detail in error.errors()
    )


def _validated_reference(value: str) -> str:
    if type(value) is not str or not value or len(value) > 1024 or "\x00" in value:
        raise ValueError(
            "Workspace reference must be a non-empty string of at most 1024 "
            "characters without NUL"
        )
    return value


def is_inline_tree(value: object) -> bool:
    """Whether ``value`` is the removed wire form that carried a tree's file bytes."""
    return (
        type(value) is dict
        and set(value) == {"version", "entries"}
        and value["version"] == _MANIFEST_VERSION
        and type(value["entries"]) is list
        and len(value["entries"]) > 0
        and all(
            type(item) is dict
            and item.get("kind") in {"file", "directory"}
            and type(item.get("path")) is str
            for item in value["entries"]
        )
    )


def safe_relative_path(relative: str) -> str:
    """Return ``relative`` if it is a normalized POSIX path that stays inside its root."""
    path = PurePosixPath(relative)
    if (
        not relative
        or "\x00" in relative
        or "\\" in relative
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or str(path) != relative
    ):
        raise ValueError(f"Workspace entry path is unsafe: {relative!r}")
    return relative


def write_inline_tree(manifest: dict[str, Any], folder: Path) -> None:
    """Write the removed inline wire form into the empty directory ``folder``.

    Kept only so a host can still read trees stored by earlier releases. Entry paths
    are validated to stay inside ``folder`` and file digests are verified.
    """
    if not is_inline_tree(manifest):
        raise ValueError("Unsupported workspace manifest")
    written: dict[str, str] = {}
    for item in manifest["entries"]:
        relative = safe_relative_path(item["path"])
        path = PurePosixPath(relative)
        if relative in written:
            raise ValueError(f"Duplicate workspace entry {relative!r}")
        for parent in path.parents:
            if str(parent) != "." and written.get(str(parent)) != "directory":
                raise ValueError(f"Workspace entry is missing directory {str(parent)!r}")
        target = folder / relative
        if item.get("kind") == "directory" and set(item) == {"kind", "path"}:
            target.mkdir()
        elif (
            item.get("kind") == "file"
            and set(item) == {"kind", "path", "content", "sha256"}
            and type(item["content"]) is str
            and type(item["sha256"]) is str
        ):
            try:
                content = base64.b64decode(item["content"], validate=True)
            except binascii.Error as exc:
                raise ValueError("Workspace file content is not valid base64") from exc
            if hashlib.sha256(content).hexdigest() != item["sha256"].lower():
                raise ValueError("Workspace file sha256 does not match content")
            with target.open("xb") as handle:
                handle.write(content)
        else:
            raise ValueError("Malformed workspace manifest entry")
        written[relative] = item["kind"]


class Workspace(BaseModel):
    """A directory the host keeps, passed between steps by reference.

    ``Workspace()`` names the workspace of the current run: one directory the host
    creates on first use and keeps with the run. Steps read and write it in place
    through ``path``; only the reference crosses step, process and result
    boundaries. A workspace never holds file bytes and never serializes a host path.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        frozen=True,
        strict=True,
    )

    reference: str | None = None
    _directory_path: Path | None = PrivateAttr(default=None)

    @classmethod
    def __get_pydantic_json_schema__(cls, schema, handler):
        # Input catalogs validate JSON before Python reconstruction, so describe
        # every wire form _decode_manifest accepts: the tagged reference and the
        # run workspace, which keeps the empty-tree form earlier releases used.
        return {
            "anyOf": [
                handler(schema),
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "workspace", "type": "string"},
                        "version": {"const": _MANIFEST_VERSION, "type": "integer"},
                        "reference": {"type": "string", "minLength": 1},
                    },
                    "required": ["kind", "version", "reference"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "version": {"const": _MANIFEST_VERSION, "type": "integer"},
                        "entries": {"type": "array"},
                    },
                    "required": ["entries"],
                    "additionalProperties": False,
                },
            ]
        }

    @model_validator(mode="before")
    @classmethod
    def _decode_manifest(cls, value: Any) -> Any:
        if isinstance(value, cls) or type(value) is not dict:
            return value
        if "entries" in value:
            if value.get("reference") is not None:
                raise ValueError("Workspace cannot have both entries and a reference")
            if type(value["entries"]) not in (list, tuple):
                raise ValueError("Workspace manifest entries must be a list")
            if value["entries"]:
                raise ValueError(
                    f"{VALUE_WORKSPACE_UNSUPPORTED}: a Workspace no longer carries file "
                    "bytes; pass a directory reference instead"
                )
        if "version" not in value:
            return {key: item for key, item in value.items() if key != "entries"}
        if type(value["version"]) is not int or value["version"] != _MANIFEST_VERSION:
            raise ValueError("Unsupported workspace manifest")
        if set(value) == {"version", "entries"}:
            return {}
        if set(value) == {"version", "reference"}:
            warnings.warn(
                "Untagged Workspace references are deprecated; include kind=workspace. "
                "Support is removed in the next minor release.",
                DeprecationWarning,
                stacklevel=2,
            )
        elif set(value) != {"kind", "version", "reference"} or value["kind"] != "workspace":
            raise ValueError("Unsupported workspace manifest")
        return {"reference": _validated_reference(value["reference"])}

    @model_validator(mode="after")
    def _validate_reference(self) -> "Workspace":
        if self.reference is not None:
            _validated_reference(self.reference)
        return self

    @property
    def is_reference(self) -> bool:
        """Whether a host has named this workspace; ``Workspace()`` is not named yet."""
        return self.reference is not None

    @classmethod
    def from_directory(cls, path: str | Path, *, reference: str) -> "Workspace":
        """Bind a host-owned directory without copying it or owning its lifetime."""
        workspace = cls(reference=_validated_reference(reference))
        root = Path(path)
        if not root.is_absolute():
            raise ValueError("Workspace directory path must be absolute")
        if not root.is_dir():
            raise ValueError("Workspace directory must be an existing directory")
        workspace._directory_path = root
        return workspace

    @classmethod
    def from_path(cls, path: str | Path) -> "Workspace":
        """Copy a directory once into a host-owned folder and refer to the copy.

        Later edits to the source do not change the workspace, and steps never
        edit the source. Use ``from_directory`` to share a directory in place.
        """
        from .references import capture_local_workspace

        return capture_local_workspace(Path(path))

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> "Workspace":
        if type(manifest) is not dict or "version" not in manifest:
            raise ValueError("Unsupported workspace manifest")
        return cls.model_validate(manifest)

    def manifest(self) -> dict[str, Any]:
        """The wire form: a tagged reference, or the run workspace placeholder."""
        if self.reference is None:
            return {"version": _MANIFEST_VERSION, "entries": []}
        return {
            "kind": "workspace",
            "version": _MANIFEST_VERSION,
            "reference": _validated_reference(self.reference),
        }

    @model_serializer(mode="wrap")
    def _serialize(self, handler, info):
        context = info.context
        serializer = None
        if isinstance(context, dict):
            serializer = context.get(_WORKSPACE_SERIALIZER_CONTEXT_KEY)
        if callable(serializer):
            return serializer(self)
        return self.manifest()

    @property
    def path(self) -> Path:
        """The directory to read and write; the host must have bound it first."""
        if self._directory_path is not None:
            return self._directory_path
        if self.reference is not None:
            raise RuntimeError("workspace reference is not bound to a directory")
        from .references import current_run_workspace

        return current_run_workspace().path

    def snapshot(self) -> "Workspace":
        """Return this workspace: a reference already names the directory's contents."""
        _ = self.path
        return self

    def __getstate__(self):
        return self.manifest()

    def __setstate__(self, state):
        restored = type(self).from_manifest(state)
        super().__setstate__(BaseModel.__getstate__(restored))


def snapshot_workspaces(value: Any) -> Any:
    """Freeze every file reference in a supported workflow return shape.

    Workspaces pass through unchanged; a file's digest is computed here, while
    its step's bytes are still the ones on disk.
    """
    from .file import File
    from .types import LineagedResult

    if isinstance(value, Workspace):
        return value
    if isinstance(value, File):
        return value.snapshot()
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
