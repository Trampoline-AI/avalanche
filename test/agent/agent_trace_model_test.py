"""The runtime-neutral agent trace record and file input evidence."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

import avalanche as ava
from avalanche.agent.native.claude_agent import ClaudeAgentTurn
from avalanche.agent.native.predict_rlm import (
    PredictRLMPredictCall,
    PredictRLMTokenUsage,
    PredictRLMTraceHeader,
    PredictRLMTurn,
    PredictRLMUsage,
)
from avalanche.agent.trace import (
    TRACE_HEADER_NATIVE_LIMIT_BYTES,
    AgentTrace,
    AgentTraceHeader,
    TokenUsage,
    ToolCall,
    Turn,
    file_input_evidence,
)
from avalanche.workspace import WorkspaceEntry

_NO_USAGE = PredictRLMUsage(
    main=PredictRLMTokenUsage(input_tokens=0, output_tokens=0, cost=0.0, cache_hits=0),
    sub=PredictRLMTokenUsage(input_tokens=0, output_tokens=0, cost=0.0, cache_hits=0),
)


_PREDICT_CALL = PredictRLMPredictCall(
    signature="question -> answer",
    instructions=None,
    model="sub",
    usage=_NO_USAGE.sub,
    finish_reason="stop",
)


def _tool_call(name: str) -> ToolCall:
    return ToolCall(
        call_id=None,
        caller="code",
        name=name,
        arguments={"term": "x"},
        result=["y"],
        error=None,
        duration_ms=3,
    )


def _native_turn(*, predict_calls: int = 1) -> PredictRLMTurn:
    return PredictRLMTurn(
        code="print(1)",
        output="1",
        untruncated_output="1",
        error=False,
        finish_reason="stop",
        usage=_NO_USAGE,
        predict_calls=(_PREDICT_CALL,) * predict_calls,
    )


def _turn(index: int, *, tool_calls: int = 1) -> Turn:
    return Turn(
        index=index,
        message=None,
        reasoning="think",
        # One sub-model call, a ``predict`` tool call after the turn's other tools.
        tool_calls=(*(_tool_call("lookup") for _ in range(tool_calls)), _tool_call("predict")),
        duration_ms=10,
        usage=None,
        native=_native_turn(),
    )


def _header(**changes: object) -> AgentTraceHeader:
    fields: dict[str, object] = {
        "runtime_kind": "predict_rlm",
        "trace_format": "predict_rlm.run/v1",
        "status": "succeeded",
        "stop_reason": "submit",
        "model": "openai/gpt-5.5",
        "duration_ms": 20,
        "usage": TokenUsage(input_tokens=10, output_tokens=5, cost_usd=None),
        "turn_count": 2,
        "tool_call_count": 4,
        "external_run_id": "run-1",
        "session_id": None,
        "native": PredictRLMTraceHeader(
            sub_model=None, max_iterations=5, usage=_NO_USAGE, telemetry_ref=None
        ),
    }
    fields.update(changes)
    return AgentTraceHeader.model_validate(fields)


def _record(**changes: object) -> AgentTrace:
    fields: dict[str, object] = {
        "header": _header(),
        "turns": (_turn(1), _turn(2)),
        "turns_streamed": False,
    }
    fields.update(changes)
    return AgentTrace.model_validate(fields)


def test_record_round_trips_through_json():
    record = _record()

    restored = AgentTrace.model_validate_json(record.model_dump_json())

    assert restored == record
    assert restored.schema_version == 2


@pytest.mark.parametrize(
    "header",
    [
        {"trace_format": "claude_agent.messages/v1"},
        {"turn_count": 3},
        {"tool_call_count": 2},
    ],
    ids=["format-mismatch", "turn-count", "tool-call-count"],
)
def test_record_rejects_a_header_that_disagrees_with_its_body(header):
    with pytest.raises(ValidationError):
        _record(header=_header(**header))


def test_header_rejects_a_runtime_part_too_large_for_run_snapshots():
    large = PredictRLMTraceHeader(
        sub_model=None,
        max_iterations=5,
        usage=_NO_USAGE,
        telemetry_ref={"payload": "x" * TRACE_HEADER_NATIVE_LIMIT_BYTES},
    )

    with pytest.raises(ValidationError, match="run snapshot"):
        _header(native=large)


def test_record_rejects_out_of_order_turns():
    with pytest.raises(ValidationError, match="1..n"):
        _record(turns=(_turn(2), _turn(1)))


def test_streamed_record_carries_counts_but_no_turns():
    record = _record(turns=(), turns_streamed=True)

    assert record.header.turn_count == 2
    with pytest.raises(ValidationError, match="streamed"):
        _record(turns_streamed=True)


@pytest.mark.parametrize(
    "change",
    [
        lambda body: body["header"]["native"].update(format="unknown/v1"),
        lambda body: body.update(native=body["header"].pop("native")),
        lambda body: body["header"].update(runtime_kind="unknown"),
        lambda body: body.update(schema_version=1),
        lambda body: body["turns"][0]["native"].update(format="unknown/v1"),
        lambda body: body["turns"][0]["tool_calls"][0].update(kind="tool_call"),
        lambda body: body["turns"][0].update(text=None),
        lambda body: body.update(extra=True),
    ],
    ids=[
        "unknown-format",
        "native-outside-header",
        "unknown-runtime",
        "old-schema",
        "unknown-turn-format",
        "old-tool-call",
        "old-turn",
        "extra-field",
    ],
)
def test_json_body_rejects_unknown_shapes(change):
    body = _record().model_dump(mode="json")
    change(body)

    with pytest.raises(ValidationError):
        AgentTrace.model_validate(body)


@pytest.mark.parametrize(
    "tool_calls",
    [(_tool_call("lookup"),), (_tool_call("predict"), _tool_call("lookup")), ()],
    ids=["no-predict-call", "predict-call-not-last", "no-tool-calls"],
)
def test_a_predict_rlm_turn_ends_with_one_predict_call_per_native_predict_call(tool_calls):
    with pytest.raises(ValidationError, match="predict calls"):
        Turn(
            index=1,
            message=None,
            reasoning=None,
            tool_calls=tool_calls,
            duration_ms=None,
            usage=None,
            native=_native_turn(),
        )


def test_record_rejects_a_turn_of_another_runtime():
    claude_turn = Turn(
        index=2,
        message="hi",
        reasoning=None,
        tool_calls=(),
        duration_ms=None,
        usage=None,
        native=ClaudeAgentTurn(message_id=None, model="m", stop_reason=None, api_error=None),
    )

    with pytest.raises(ValidationError, match="do not belong"):
        _record(turns=(_turn(1), claude_turn), header=_header(tool_call_count=2))


@pytest.mark.parametrize(
    ("native", "failed"),
    [
        (_native_turn(predict_calls=0), False),
        (_native_turn(predict_calls=0).model_copy(update={"error": True}), True),
        (ClaudeAgentTurn(message_id=None, model="m", stop_reason=None, api_error=None), False),
        (ClaudeAgentTurn(message_id=None, model="m", stop_reason=None, api_error="429"), True),
    ],
    ids=["code-ran", "code-failed", "claude-replied", "claude-api-error"],
)
def test_each_runtime_decides_when_its_turn_failed(native, failed):
    turn = Turn(
        index=1,
        message=None,
        reasoning=None,
        tool_calls=(),
        duration_ms=None,
        usage=None,
        native=native,
    )

    assert turn.failed is failed


def test_file_evidence_records_the_reference_without_bytes_or_host_paths(tmp_path):
    source = tmp_path / "docs" / "a.txt"
    source.parent.mkdir()
    source.write_bytes(b"hello")
    workspace = ava.Workspace.from_directory(tmp_path, reference="shared/reports")
    file = ava.File.from_path(source, content_type="text/plain", workspace=workspace)

    evidence = file_input_evidence(file)

    assert evidence.model_dump() == {
        "kind": "file",
        "workspace": "shared/reports",
        "path": "docs/a.txt",
        "name": "a.txt",
        "content_type": "text/plain",
        "size_bytes": 5,
        "sha256": hashlib.sha256(b"hello").hexdigest(),
    }
    assert str(tmp_path) not in evidence.model_dump_json()


def test_file_evidence_of_an_unbound_reference_uses_its_recorded_digest(tmp_path):
    source = tmp_path / "a.txt"
    source.write_bytes(b"hello")
    unbound = ava.File.model_validate(ava.File.from_path(source).model_dump())

    assert file_input_evidence(unbound).sha256 == hashlib.sha256(b"hello").hexdigest()


def test_value_workspace_evidence_counts_entries_and_bytes():
    workspace = ava.Workspace(
        entries=(
            WorkspaceEntry(path="docs", kind="directory"),
            WorkspaceEntry(path="docs/a.txt", kind="file", content=b"abc"),
            WorkspaceEntry(path="b.txt", kind="file", content=b"de"),
        )
    )

    assert file_input_evidence(workspace).model_dump() == {
        "kind": "workspace",
        "entry_count": 3,
        "total_size_bytes": 5,
    }


def test_reference_workspace_evidence_keeps_the_reference_not_the_path(tmp_path):
    workspace = ava.Workspace.from_directory(tmp_path, reference="shared/reports")

    assert file_input_evidence(workspace).model_dump() == {
        "kind": "workspace_reference",
        "reference": "shared/reports",
    }
