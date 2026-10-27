"""Recorded PredictRLM runs replayed through the real adapter, for mocks and tests.

Fixtures built this way cannot drift from what a live PredictRLM step sends.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..._agent_evidence import AgentEvidenceObserverEvent, capture_agent_evidence
from ...agent.contract import PythonOutputs
from ...agent.steps import _InvocationEvents
from .events import project_event
from .trace import _emit_terminal_trace, _emit_trace_unavailable

if TYPE_CHECKING:
    from predict_rlm import RunEvent, RunEvidence, RunTrace


def replay(
    run_events: Sequence[RunEvent],
    evidence: RunEvidence,
    *,
    invocation_id: str,
    trace: RunTrace | None,
    error: str = "trace unavailable",
) -> list[AgentEvidenceObserverEvent]:
    """The evidence an invocation sends for these events, then its final event.

    The final event carries the outputs of the run's ``run.succeeded`` event, if any.
    Without ``trace`` the invocation ends as unavailable with ``error``.
    """
    sent: list[AgentEvidenceObserverEvent] = []
    events = _InvocationEvents(invocation_id, "predict_rlm")
    outputs = None
    with capture_agent_evidence(sent.append, errors="raise"):
        for event in run_events:
            if event.kind.value == "run.succeeded":
                outputs = PythonOutputs(values=event.data["outputs"])
            projected = project_event(event)
            if projected is not None:
                events.record(*projected)
        if trace is None:
            _emit_trace_unavailable(RuntimeError(error), evidence, events)
        else:
            _emit_terminal_trace(
                trace, evidence, events, model=None, sub_model=None, outputs=outputs
            )
    return sent
