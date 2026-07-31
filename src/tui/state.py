"""StateProvider protocol — the contract between TUI widgets and data."""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from .models import (
    CatalogSnapshot,
    DetailUpdate,
    LogEntry,
    ResetBaseline,
    RunState,
    StreamResetNotice,
    TraceDetail,
    WorkflowInfo,
)


class StateProvider(Protocol):
    """State and action contract consumed by the Avalanche TUI."""

    operator_reachable: bool
    operator_instance_id: str
    stream_state: str
    stream_error: str

    def list_workflows(self) -> list[WorkflowInfo]: ...

    def get_catalog(self) -> CatalogSnapshot: ...

    def list_runs(self, workflow_selector: str) -> list[RunState]: ...

    def get_run(self, run_id: str) -> RunState | None: ...
    def hydrate_trace(self, run_id: str, node_id: str) -> TraceDetail | None:
        """Hydrate one selected agent node's immutable trace body."""
        ...

    def start_run(self, workflow_selector: str, **kwargs: Any) -> str:
        """Start a new run, return run_id."""
        ...

    def cancel_run(self, run_id: str) -> None: ...

    def on_run_update(self, callback: Callable[[RunState], None]) -> None: ...

    def on_catalog_update(self, callback: Callable[[CatalogSnapshot], None]) -> None: ...

    def on_log(self, callback: Callable[[LogEntry], None]) -> None: ...
    def on_detail_update(self, callback: Callable[[DetailUpdate], None]) -> None: ...
    def start_stream(self) -> None:
        """Start update delivery after all callbacks are registered."""
        ...

    def close(self) -> None:
        """Release provider-owned streams and in-flight RPCs."""
        ...

    def on_stream_reset(self, callback: Callable[[StreamResetNotice], None]) -> None: ...

    def load_reset_baseline(self, notice: StreamResetNotice) -> ResetBaseline: ...

    def acknowledge_stream_reset(
        self,
        generation: int,
        operator_instance_id: str,
        reconciled_sequence: int,
    ) -> None: ...


@runtime_checkable
class ConnectionAwareStateProvider(Protocol):
    """Optional connection state exposed by remote providers."""

    operator_reachable: bool

    @property
    def connection_label(self) -> str: ...

    @property
    def last_error(self) -> str: ...

    def ping(self) -> bool: ...


def get_operator_reachability(provider: StateProvider) -> bool:
    """Return the provider's explicit operator reachability."""
    return provider.operator_reachable


def get_stream_state(provider: StateProvider) -> str:
    """Return the provider's explicit stream lifecycle value."""
    value = getattr(provider.stream_state, "value", provider.stream_state)
    if not isinstance(value, str):
        raise TypeError("provider.stream_state must be a string-valued state")
    return value


__all__ = [
    "ConnectionAwareStateProvider",
    "StateProvider",
    "get_operator_reachability",
    "get_stream_state",
]
