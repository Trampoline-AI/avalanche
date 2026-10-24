"""PredictRLM terminal traces: the common trace record and the final trace event."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...agent.steps import AgentStepError
from ...agent.trace import (
    AgentTrace,
    AgentTraceHeader,
    AgentTraceStatus,
    PredictRLMNativeTrace,
)
from .events import _bounded_agent_error, native_usage

if TYPE_CHECKING:
    from predict_rlm import RunEvidence, RunTrace

    from ...agent.contract import AgentEventSink


def _emit_terminal_trace(
    trace: RunTrace,
    evidence: RunEvidence,
    events: AgentEventSink,
    *,
    model: str | None,
    sub_model: str | None,
) -> None:
    """Send the final trace; its turns already crossed the live event channel."""
    record = trace_record(trace, evidence, model=model, sub_model=sub_model)
    events.finished(
        AgentTrace(header=record.header, turns=(), native=record.native, turns_streamed=True),
        complete=evidence.complete,
    )


def _emit_trace_unavailable(
    error: BaseException, evidence: RunEvidence, events: AgentEventSink
) -> None:
    events.unavailable(
        status=_STATUSES[evidence.terminal_outcome],
        error=_bounded_agent_error(str(error)),
        external_run_id=evidence.run_id,
        complete=evidence.complete,
    )


def _model_display_name(value: Any) -> str | None:
    """Name LMs whose LiteLLM model id hides the provider they actually call.

    Codex LM reports ``openai/<model>`` for LiteLLM pricing, but it sends requests
    through a ChatGPT login rather than the OpenAI API, so show ``codex/<model>``.
    """
    from dspy_codex_lm import CodexHTTPLM

    if not isinstance(value, CodexHTTPLM):
        return None
    provider, separator, model = value.model.partition("/")
    if provider != "openai" or not separator:
        raise AgentStepError(f"unexpected Codex LM model id {value.model!r}")
    return f"codex/{model}"


_STOP_REASONS = {"completed": "submit", "max_iterations": "max_iterations"}
_STATUSES: dict[str, AgentTraceStatus] = {
    "completed": "succeeded",
    "error": "failed",
    "cancelled": "cancelled",
}


def trace_header(
    trace: RunTrace,
    evidence: RunEvidence,
    *,
    model: str | None,
    turn_count: int,
    tool_call_count: int,
) -> AgentTraceHeader:
    """The common header for one PredictRLM run, without converting its turns.

    The counts are passed in because a terminal trace sent after live events
    carries no steps. ``model`` is a display name that replaces PredictRLM's
    model id when set (see ``_model_display_name``).
    """
    from .mapping import common_usage

    status = _STATUSES[evidence.terminal_outcome]
    return AgentTraceHeader(
        runtime_kind="predict_rlm",
        trace_format="predict_rlm.run/v1",
        status=status,
        stop_reason=_STOP_REASONS.get(trace.status),
        model=trace.model if model is None else model,
        duration_ms=trace.duration_ms,
        usage=common_usage(trace.usage.main, trace.usage.sub),
        turn_count=turn_count,
        tool_call_count=tool_call_count,
        external_run_id=evidence.run_id,
        session_id=None,
    )


def trace_record(
    trace: RunTrace,
    evidence: RunEvidence,
    *,
    model: str | None,
    sub_model: str | None,
) -> AgentTrace:
    """The common trace record for one PredictRLM run, turns included.

    ``model`` and ``sub_model`` are display names that replace PredictRLM's
    model ids when set (see ``_model_display_name``).
    """
    from .mapping import turn_from_step

    return AgentTrace(
        header=trace_header(
            trace,
            evidence,
            model=model,
            turn_count=len(trace.steps),
            tool_call_count=sum(len(step.tool_calls) for step in trace.steps),
        ),
        turns=tuple(turn_from_step(step) for step in trace.steps),
        native=PredictRLMNativeTrace(
            sub_model=_sub_model_name(trace, sub_model),
            max_iterations=trace.max_iterations,
            usage=native_usage(trace.usage),
            telemetry_ref=trace.telemetry_ref,
        ),
        turns_streamed=False,
    )


def _sub_model_name(trace: RunTrace, display_name: str | None) -> str | None:
    # PredictRLM omits the sub-model when it shares the main LM.
    if trace.sub_model is None or display_name is None:
        return trace.sub_model
    return display_name
