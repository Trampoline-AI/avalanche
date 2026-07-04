"""StateProvider protocol — the contract between TUI widgets and data."""

from __future__ import annotations

from typing import Any, Callable, Protocol

from .models import LogEntry, RunState, WorkflowInfo


class StateProvider(Protocol):
    def list_workflows(self) -> list[WorkflowInfo]: ...

    def list_runs(self, flow_name: str) -> list[RunState]: ...

    def get_run(self, run_id: str) -> RunState | None: ...

    def start_run(self, flow_name: str, **kwargs: Any) -> str:
        """Start a new run, return run_id."""
        ...

    def cancel_run(self, run_id: str) -> None: ...

    def on_run_update(self, callback: Callable[[RunState], None]) -> None: ...

    def on_log(self, callback: Callable[[LogEntry], None]) -> None: ...
