"""Regression coverage for step-scoped verbose RLM logs."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from types import SimpleNamespace

import pytest

import avalanche as ava
from runtime.operator import Operator
from runtime.operator.models import NodeState, NodeStatus, RunState, RunStatus
from runtime.operator.run_worker import (
    _QueueLogHandler,
    _QueueStream,
    _with_local_node_observers,
    _with_ray_node_observers,
)
from tui.dag_layout import DagNode
from tui.widgets.log_panel import LogWidget


def test_operator_forwards_info_not_debug_logs() -> None:
    class Queue:
        def __init__(self) -> None:
            self.items: list[dict[str, object]] = []

        def put(self, item: dict[str, object]) -> None:
            self.items.append(item)

    queue = Queue()
    handler = _QueueLogHandler(queue)
    logger = logging.getLogger("test.operator_capture")
    old_level = logger.level
    old_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        logger.debug("debug detail")
        logger.info("run started")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    assert [item["message"] for item in queue.items] == ["run started"]


@pytest.mark.parametrize("backend", ["local", "ray"])
def test_verbose_rlm_log_retains_agent_step_node_id(backend: str) -> None:
    class Queue:
        def __init__(self) -> None:
            self.items: list[dict[str, object]] = []

        def put(self, item: dict[str, object]) -> None:
            self.items.append(item)

    trace_logger = logging.getLogger("predict_rlm.trace")

    @ava.agent_step("prompt -> answer")
    async def verbose_agent(*, agent: ava.Agent) -> str:
        trace_handler = logging.StreamHandler(sys.stderr)
        old_level = trace_logger.level
        old_propagate = trace_logger.propagate
        trace_logger.addHandler(trace_handler)
        trace_logger.setLevel(logging.DEBUG)
        trace_logger.propagate = False
        try:
            trace_logger.debug("verbose PredictRLM trace detail")
        finally:
            trace_logger.removeHandler(trace_handler)
            trace_logger.setLevel(old_level)
            trace_logger.propagate = old_propagate
        return "done"

    node_id = "verbose_agent_1"
    queue = Queue()
    handler: _QueueLogHandler | None = None
    old_level: int | None = None
    old_stderr = None
    local_stderr: _QueueStream | None = None
    if backend == "local":
        root_logger = logging.getLogger()
        old_level = root_logger.level
        handler = _QueueLogHandler(queue)
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.DEBUG)
        local_stderr = _QueueStream(queue, "operator", logging.ERROR)
        old_stderr = sys.stderr
        sys.stderr = local_stderr
        wrapped = _with_local_node_observers(
            node_id,
            verbose_agent.fn,
            _QueueStream(queue, "operator", logging.INFO),
            local_stderr,
            queue,
        )
    else:
        wrapped = _with_ray_node_observers(node_id, verbose_agent.fn, queue)
    try:
        assert asyncio.run(wrapped()) == "done"
    finally:
        if local_stderr is not None:
            sys.stderr = old_stderr
        if handler is not None:
            root_logger.removeHandler(handler)
            root_logger.setLevel(old_level)

    log_event = next(
        item
        for item in queue.items
        if item["type"] == "log" and item["message"] == "verbose PredictRLM trace detail"
    )
    operator = Operator([], schedule=False, watch=False)
    run = RunState(run_id="run-rlm", flow_name="rlm-flow", status=RunStatus.RUNNING)
    run.nodes[node_id] = NodeState(
        node_id=node_id,
        name="verbose_agent",
        node_type="step",
        status=NodeStatus.RUNNING,
    )
    handle = SimpleNamespace(
        cancel_event=threading.Event(),
        result_bundle=None,
        success_quiesced=False,
    )
    try:
        with operator._lock:
            operator._runs[run.run_id] = run
        assert operator._apply_event(run.run_id, handle, log_event) is False

        materialized = operator.get_run(run.run_id)
        assert materialized is not None
        log_entry = next(
            entry
            for entry in materialized.logs
            if entry.message == "verbose PredictRLM trace detail"
        )
        assert log_entry.node_id == node_id
        assert LogWidget._entry_visible(
            log_entry,
            DagNode(name=node_id, node_type="step", display_name="verbose_agent"),
        )
    finally:
        operator.close()
