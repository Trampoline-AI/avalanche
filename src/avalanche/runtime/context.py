from __future__ import annotations

import hashlib
from contextvars import ContextVar
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
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


def _run_with_context(context: RunContext, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Execute a function with a RunContext available to framework helpers."""
    from runtime._async import resolve_awaitable

    token = _current_run_context.set(context)
    try:
        return resolve_awaitable(fn(*args, **kwargs))
    finally:
        _current_run_context.reset(token)


def run_with_context(context: RunContext, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Execute a function with a RunContext available to framework helpers."""
    return _run_with_context(context, fn, *args, **kwargs)


_FILE_SERIALIZER_CONTEXT_KEY = "__avalanche_operator_file_serializer__"


class File(BaseModel):
    """In-memory file value accepted as workflow input or returned as output."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    content: bytes
    name: str | None = None
    content_type: str | None = None
    sha256: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(
        self,
        handler: SerializerFunctionWrapHandler,
        info: SerializationInfo,
    ) -> Any:
        """Allow the operator codec to substitute an attachment marker."""
        context = info.context
        serializer = (
            context.get(_FILE_SERIALIZER_CONTEXT_KEY) if isinstance(context, dict) else None
        )
        if callable(serializer):
            return serializer(self)
        return handler(self)

    @classmethod
    def from_path(cls, path: str | Path, *, content_type: str | None = None) -> "File":
        file_path = Path(path)
        return cls(
            name=file_path.name,
            content=file_path.read_bytes(),
            content_type=content_type,
        )

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
