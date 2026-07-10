from __future__ import annotations

import hashlib
from contextvars import ContextVar
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


class Rerun(BaseModel):
    """Run-scoped request to re-execute part of a previous workflow run."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    run_id: str
    start: tuple[str, ...]
    mode: Literal["autorun", "lazy"] = "autorun"
    deployment_id: str | None = None

    @field_validator("start", mode="before")
    @classmethod
    def _normalize_start(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return value

    @field_validator("start")
    @classmethod
    def _validate_start(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("rerun start must include at least one node slug")
        if any(not isinstance(item, str) or not item for item in value):
            raise ValueError("rerun start entries must be non-empty strings")
        return value


class RunContext(BaseContext):
    """Runtime metadata injected into annotated node parameters."""

    run_id: str
    workflow_name: str
    executor_type: str = "local"
    rerun: Rerun | None = None
    node_id: str | None = None
    node_name: str | None = None
    node_slug: str | None = None
    lineage_vector: dict[str, str] = Field(default_factory=dict, exclude=True)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def for_node(self, *, node_id: str, node_name: str, node_slug: str) -> "RunContext":
        return self.model_copy(
            update={
                "node_id": node_id,
                "node_name": node_name,
                "node_slug": node_slug,
                "lineage_vector": dict(self.lineage_vector),
            }
        )


_current_run_context: ContextVar[RunContext | None] = ContextVar(
    "_current_run_context",
    default=None,
)


def get_current_run_context() -> RunContext | None:
    """Return the RunContext for the currently executing workflow node, if any."""
    return _current_run_context.get()


def run_with_context(context: RunContext, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Execute a function with a RunContext available to framework helpers."""
    from runtime._async import resolve_awaitable

    token = _current_run_context.set(context)
    try:
        return resolve_awaitable(fn(*args, **kwargs))
    finally:
        _current_run_context.reset(token)


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
    sha256: str | None = None

    @field_validator("uri")
    @classmethod
    def _validate_s3_uri(cls, value: str) -> str:
        if not value.startswith("s3://"):
            raise ValueError("S3File uri must start with s3://")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("S3File sha256 must be a 64-character hexadecimal digest")
        return normalized

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
        filesystem_options = dict(kwargs)
        open_options: dict[str, Any] = {}
        if self.version_id is not None:
            filesystem_options.setdefault("version_aware", True)
            open_options["version_id"] = self.version_id
        return s3fs.S3FileSystem(**filesystem_options).open(self.uri, mode, **open_options)

    def read_bytes(self, **kwargs: Any) -> bytes:
        with self.open("rb", **kwargs) as file:
            content = file.read()
        if self.sha256 is not None and hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError("S3File sha256 does not match content")
        return content
