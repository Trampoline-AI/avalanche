"""Runtime map contracts shared by capture, scheduling and the operator."""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_MAX_ITEMS = 8
HARD_CAP = 64


class MapSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mapped_args: tuple[str, ...]
    broadcast_args: tuple[str, ...]
    max_items: int = Field(ge=0, le=HARD_CAP)
    inherited: bool
    fail_fast: bool


class NodesExpanded(BaseModel):
    """Publication barrier: emitted before any lifecycle event for these children."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    parent: str
    children: tuple[str, ...]
    n_expected: int = Field(ge=0, le=HARD_CAP)
    chain: dict[str, int]


@dataclass(frozen=True)
class MapItemFailure:
    index: int
    child_id: str
    error: Exception


class MapItemsFailed(RuntimeError):  # noqa: N818 - public map API name
    """A map has no downstream value if any item failed terminally."""

    def __init__(self, name: str, failures: tuple[MapItemFailure, ...]):
        self.failures = tuple(sorted(failures, key=lambda item: item.index))
        details = "; ".join(
            f"({item.index}, {item.child_id}, {type(item.error).__name__}: {item.error})"
            for item in self.failures
        )
        super().__init__(f"{name}: MapItemsFailed [{details}]")
