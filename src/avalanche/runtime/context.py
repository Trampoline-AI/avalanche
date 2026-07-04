from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Literal, TextIO, overload

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_INLINE_FILE_BYTES = 3 * 1024 * 1024
MAX_INLINE_REQUEST_BYTES = MAX_INLINE_FILE_BYTES


def _validate_inline_file_size(size: int, *, label: str) -> None:
    if size > MAX_INLINE_FILE_BYTES:
        raise ValueError(
            f"{label} is {size} bytes, exceeding the maximum inline file size "
            f"of {MAX_INLINE_FILE_BYTES} bytes. Use ava.S3File for larger files."
        )


class BaseInput(BaseModel):
    """Base class for workflow run input models."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")


class BaseContext(BaseModel):
    """Base class for workflow run context models."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")


class RunContext(BaseContext):
    """Runtime metadata injected into annotated node parameters."""

    execution_id: str
    workflow_name: str
    executor_type: str = "local"
    node_id: str | None = None
    node_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def for_node(self, *, node_id: str, node_name: str) -> "RunContext":
        return self.model_copy(update={"node_id": node_id, "node_name": node_name})


class File(BaseModel):
    """Small file payload carried with a workflow run request."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    content: bytes
    name: str | None = None
    content_type: str | None = None
    sha256: str | None = None

    @classmethod
    def from_path(cls, path: str | Path, *, content_type: str | None = None) -> "File":
        file_path = Path(path)
        _validate_inline_file_size(file_path.stat().st_size, label=str(file_path))
        return cls(
            name=file_path.name,
            content=file_path.read_bytes(),
            content_type=content_type,
        )

    @field_validator("content")
    @classmethod
    def _validate_content_size(cls, value: bytes) -> bytes:
        _validate_inline_file_size(len(value), label="File content")
        return value

    @model_validator(mode="after")
    def _compute_or_validate_sha256(self) -> "File":
        digest = hashlib.sha256(self.content).hexdigest()
        if self.sha256 is None:
            self.sha256 = digest
            return self
        if self.sha256.lower() != digest:
            raise ValueError("File sha256 does not match content")
        self.sha256 = digest
        return self

    def read_bytes(self) -> bytes:
        return self.content

    def open(self) -> BytesIO:
        return BytesIO(self.content)


class S3File(BaseModel):
    """Reference to a large S3-compatible object used as workflow input."""

    model_config = ConfigDict(extra="forbid")

    uri: str
    version_id: str | None = None
    etag: str | None = None
    size_bytes: int | None = None
    content_type: str | None = None

    @field_validator("uri")
    @classmethod
    def _validate_s3_uri(cls, value: str) -> str:
        if not value.startswith("s3://"):
            raise ValueError("S3File uri must start with s3://")
        return value

    @overload
    def open(self, mode: Literal["rb"] = "rb", **kwargs: Any) -> BinaryIO: ...

    @overload
    def open(self, mode: Literal["r"], **kwargs: Any) -> TextIO: ...

    @overload
    def open(self, mode: str = "rb", **kwargs: Any) -> BinaryIO | TextIO: ...

    def open(self, mode: str = "rb", **kwargs: Any) -> BinaryIO | TextIO:
        try:
            import s3fs
        except ModuleNotFoundError as exc:
            raise ModuleNotFoundError(
                "S3File access requires s3fs. Install it with `avalanche-ai[s3]` "
                "or add s3fs to your environment.",
                name="s3fs",
            ) from exc
        return s3fs.S3FileSystem(**kwargs).open(self.uri, mode)

    def read_bytes(self, **kwargs: Any) -> bytes:
        with self.open("rb", **kwargs) as file:
            return file.read()
