"""PredictRLM terminal traces: the v1 wire events and the common trace record."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Mapping

from ..._agent_evidence import (
    AgentInvocationId,
    AgentTraceFinishedEvent,
    AgentTraceUnavailableEvent,
    emit_agent_evidence,
)
from ...agent.steps import AgentStepError
from ...agent.trace import (
    AgentTrace,
    AgentTraceHeader,
    AgentTraceStatus,
    PredictRLMNativeTrace,
)
from .events import _bounded_agent_error

if TYPE_CHECKING:
    from predict_rlm import RunEvidence, RunTrace

    from ..._agent_trace import AgentEvidenceMetadata


def _evidence_metadata(evidence: RunEvidence) -> AgentEvidenceMetadata:
    from ..._agent_trace import AgentEvidenceMetadata

    return AgentEvidenceMetadata(
        run_id=evidence.run_id,
        complete=evidence.complete,
        terminal_outcome=evidence.terminal_outcome,
    )


def _emit_terminal_trace(
    trace: RunTrace,
    *,
    invocation_id: AgentInvocationId,
    evidence: AgentEvidenceMetadata,
    runtime_kwargs: Mapping[str, Any],
) -> AgentTraceFinishedEvent:
    # Iterations already crossed the bounded event channel. Do not re-send
    # original payloads that the operator would discard after validation.
    update: dict[str, Any] = {"steps": []}
    main_label = _model_display_name(runtime_kwargs.get("lm"))
    if main_label is not None:
        update["model"] = main_label
    sub_label = _model_display_name(runtime_kwargs.get("sub_lm"))
    # PredictRLM omits the sub-model when it shares the main LM.
    if sub_label is not None and trace.sub_model is not None:
        update["sub_model"] = sub_label
    parsed = json.loads(trace.model_copy(update=update).to_exportable_json())
    event: AgentTraceFinishedEvent = {
        "kind": "trace_finished",
        "invocation_id": invocation_id,
        "trace": parsed,
        "evidence": evidence.model_dump(mode="json"),
    }
    emit_agent_evidence(event)
    return event


def _emit_trace_unavailable(
    error: BaseException,
    *,
    invocation_id: AgentInvocationId,
    evidence: AgentEvidenceMetadata,
) -> AgentTraceUnavailableEvent:
    event: AgentTraceUnavailableEvent = {
        "kind": "trace_unavailable",
        "invocation_id": invocation_id,
        "error": _bounded_agent_error(str(error)),
        "evidence": evidence.model_dump(mode="json"),
    }
    emit_agent_evidence(event)
    return event


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

    status: AgentTraceStatus = {
        "completed": "succeeded",
        "error": "failed",
        "cancelled": "cancelled",
    }[evidence.terminal_outcome]
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

    usage = trace.usage
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
            cache_hits=usage.main.cache_hits + usage.sub.cache_hits,
        ),
        turns_streamed=False,
    )


def _sub_model_name(trace: RunTrace, display_name: str | None) -> str | None:
    # PredictRLM omits the sub-model when it shares the main LM.
    if trace.sub_model is None or display_name is None:
        return trace.sub_model
    return display_name
