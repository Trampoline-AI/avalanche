from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from avalanche.file import File as File


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
