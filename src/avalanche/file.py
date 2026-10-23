"""Path-backed file references. File bodies are never part of the wire format."""

from __future__ import annotations

import hashlib
import tempfile
import warnings
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_serializer,
    model_validator,
)


class WorkspaceFileRef(BaseModel):
    """A single workspace target and its relative POSIX path; no other targets yet."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    workspace: str = Field(min_length=1, max_length=1024)
    path: str

    @field_validator("workspace")
    @classmethod
    def _reference(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("File workspace reference cannot contain NUL")
        return value

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("File path must be a relative POSIX path without traversal")
        return value


class File(BaseModel):
    """A file in a workspace, with an optional private, host-local binding.

    ``from_path`` only stats the file. Serialization streams its digest lazily;
    ``open`` streams a real read-only handle. Deserialization never grants access
    to a host path: a host must explicitly bind the reference first.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["file"] = "file"
    version: Literal[1] = 1
    ref: WorkspaceFileRef
    sha256: str | None = None
    size: int = Field(ge=0, strict=True)
    name: str | None = Field(default=None, max_length=1024)
    content_type: str | None = Field(default=None, max_length=255)
    _host_path: Path | None = PrivateAttr(default=None)

    def __init__(self, **data):
        # The compatibility constructor still creates only a path-backed ref.
        content = data.pop("content", None)
        if content is not None:
            warnings.warn(
                "File(content=...) is deprecated; use File.from_path(). "
                "Byte constructors will be removed in the next minor release.",
                DeprecationWarning,
                stacklevel=2,
            )
            if not isinstance(content, bytes):
                raise ValueError("File content must be bytes")
            root = Path(tempfile.mkdtemp(prefix="avalanche-file-"))
            path = root / "content"
            path.write_bytes(content)
            reference = type(self).from_path(path, content_type=data.pop("content_type", None))
            digest = hashlib.sha256(content).hexdigest()
            supplied = data.pop("sha256", None)
            if supplied is not None and supplied.lower() != digest:
                raise ValueError("File sha256 does not match content")
            fields = {**reference.model_dump(), "name": None, **data, "sha256": digest}
            super().__init__(**fields)
            self._host_path = path
        else:
            super().__init__(**data)

    @model_validator(mode="before")
    @classmethod
    def _wire_tag(cls, value):
        if isinstance(value, dict) and "ref" in value:
            if isinstance(value["ref"], dict) and value.get("sha256") is None:
                raise ValueError("File wire values require sha256")
            if value.get("kind") != "file" or type(value.get("version")) is not int:
                raise ValueError("File wire values require kind='file' and version=1")
        return value

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("File sha256 must be lowercase hexadecimal")
        return value

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        content_type: str | None = None,
        workspace: "Workspace | None" = None,
    ) -> File:
        """Bind an existing file without reading it, optionally inside a workspace."""
        from .references import local_workspace

        host_path = Path(path).resolve(strict=True)
        if not host_path.is_file():
            raise ValueError("File source must be a regular file")
        if workspace is None:
            workspace = local_workspace(host_path.parent)
        relative = host_path.relative_to(workspace.path.resolve()).as_posix()
        result = cls(
            kind="file",
            version=1,
            ref=WorkspaceFileRef(workspace=workspace.reference, path=relative),
            size=host_path.stat().st_size,
            name=host_path.name,
            content_type=content_type,
        )
        result._host_path = host_path
        return result

    def bind(self, path: str | Path) -> File:
        """Return a copy bound to a host-authorized regular file (no content read)."""
        host_path = Path(path).resolve(strict=True)
        if not host_path.is_file():
            raise ValueError("File binding must be a regular file")
        if host_path.stat().st_size != self.size:
            raise ValueError("File binding size does not match reference")
        result = self.model_copy()
        result._host_path = host_path
        return result

    @property
    def path(self) -> Path:
        if self._host_path is None:
            raise RuntimeError(
                "file reference is not bound to a host path; use a reference binder"
            )
        return self._host_path

    def open(self, mode: str = "rb", **kwargs):
        """Open a real file handle in read modes only (not a security sandbox)."""
        if mode not in {"r", "rb", "rt"}:
            raise ValueError("File.open accepts read modes only: 'r', 'rb', or 'rt'")
        return self.path.open(mode, **kwargs)

    @property
    def content(self) -> bytes:
        warnings.warn(
            "File.content is deprecated; use read_bytes() for explicit materialization. "
            "Removed next minor release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.read_bytes()

    def read_bytes(self) -> bytes:
        """Explicitly materialize the file body."""
        with self.open() as handle:
            return handle.read()

    def snapshot(self) -> File:
        """Stream and verify bound content, updating lazy identity before publication."""
        with self.open() as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        # file_digest may bypass buffered I/O; stat the same bound path for size.
        size = self.path.stat().st_size
        if size != self.size or (self.sha256 is not None and self.sha256 != digest):
            raise ValueError("File sha256 or size does not match bound content")
        object.__setattr__(self, "sha256", digest)
        return self

    @model_serializer(mode="plain")
    def _serialize(self):
        if self._host_path is not None:
            self.snapshot()
        if self.sha256 is None:
            raise ValueError("Unbound file reference requires sha256 before serialization")
        return {
            "kind": "file",
            "version": 1,
            "ref": self.ref.model_dump(),
            "sha256": self.sha256,
            "size": self.size,
            "name": self.name,
            "content_type": self.content_type,
        }

    def __getstate__(self):
        return self.model_dump()

    def __setstate__(self, state):
        restored = type(self).model_validate(state)
        super().__setstate__(BaseModel.__getstate__(restored))


if TYPE_CHECKING:
    from .workspace import Workspace
