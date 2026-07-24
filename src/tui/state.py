"""StateProvider protocol — the contract between TUI widgets and data."""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from .models import LogEntry, RunState, WorkflowInfo


class StateProvider(Protocol):
    """State and action contract consumed by the Avalanche TUI."""

    def list_workflows(self) -> list[WorkflowInfo]: ...

    def list_runs(self, workflow_selector: str) -> list[RunState]: ...

    def get_run(self, run_id: str) -> RunState | None: ...

    def start_run(self, workflow_selector: str, **kwargs: Any) -> str:
        """Start a new run, return run_id."""
        ...

    def cancel_run(self, run_id: str) -> None: ...

    def on_run_update(self, callback: Callable[[RunState], None]) -> None: ...

    def on_log(self, callback: Callable[[LogEntry], None]) -> None: ...


@runtime_checkable
class ConnectionAwareStateProvider(StateProvider, Protocol):
    """Optional connection state exposed by remote providers."""

    @property
    def connected(self) -> bool: ...

    @property
    def connection_label(self) -> str: ...

    @property
    def last_error(self) -> str: ...

    def ping(self) -> bool: ...


__all__ = ["ConnectionAwareStateProvider", "StateProvider"]
