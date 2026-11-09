"""RunHooks — callbacks fired during Workflow.run() execution."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable

from avalanche.machine_host import MachineEventReceipt, MachineStart
from avalanche.mapping import NodesExpanded

from .machine_steps import MachineStepRequest


@dataclass
class RunHooks:
    """Optional callbacks injected into Workflow.run() for execution monitoring.

    When hooks are active, Workflow.run() reports on_node_success only after
    actual completion, not just submission. Independent local branches can
    still overlap in the thread pool.
    """

    machine_step: (
        Callable[[MachineStepRequest], Future[MachineStart | MachineEventReceipt]] | None
    ) = None
    on_nodes_expanded: Callable[[NodesExpanded], None] | None = None
    on_node_start: Callable[[str], None] | None = None
    on_node_success: Callable[[str], None] | None = None
    on_node_failure: Callable[[str, Exception], None] | None = None
    cancel_requested: Callable[[], bool] | None = None
    wrap_fn: Callable[[str, Callable], Callable] | None = None
    """Optional function wrapper applied before executor.submit().
    Called as wrap_fn(node_id, fn) -> wrapped_fn.
    Used to capture node logs and evidence."""
    unwrap_result: Callable[[str, Any], Any] | None = None
    """Optional result unwrapper called after executor.get().
    Called as unwrap_result(node_id, raw_result) -> actual_result.
    Used to extract side-channel data (logs) from wrapped results."""
