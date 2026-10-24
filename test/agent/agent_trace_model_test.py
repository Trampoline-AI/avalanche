"""The runtime-neutral agent trace record and file input evidence."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

import avalanche as ava
from avalanche.agent.native.predict_rlm import PredictRLMTokenUsage, PredictRLMUsage
from avalanche.agent.trace import (
    AgentTrace,
    AgentTraceHeader,
    CodeExecutionAction,
    ModelCallAction,
    PredictRLMNativeTrace,
    TokenUsage,
    ToolCallAction,
    Turn,
    file_input_evidence,
)
from avalanche.workspace import WorkspaceEntry

_NO_USAGE = PredictRLMUsage(
    main=PredictRLMTokenUsage(input_tokens=0, output_tokens=0, cost=0.0, cache_hits=0),
    sub=PredictRLMTokenUsage(input_tokens=0, output_tokens=0, cost=0.0, cache_hits=0),
)


def _turn(index: int, *, tool_calls: int = 1) -> Turn:
    tools = tuple(
        ToolCallAction(
            call_id=f"t{index}-{n}",
            caller="code",
            name="lookup",
            arguments={"term": "x"},
            result=["y"],
            error=None,
            duration_ms=3,
        )
        for n in range(tool_calls)
    )
    return Turn(
        index=index,
        text=None,
        reasoning="think",
        actions=(
            CodeExecutionAction(language="python", code="print(1)", output="1", error=None),
            *tools,
            ModelCallAction(
                call_id=None,
                caller="code",
                model="sub",
                duration_ms=None,
                usage=TokenUsage(input_tokens=1, output_tokens=2),
                error=None,
            ),
        ),
        duration_ms=10,
        usage=None,
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
        "tool_call_count": 2,
        "external_run_id": "run-1",
        "session_id": None,
    }
    fields.update(changes)
    return AgentTraceHeader.model_validate(fields)


def _record(**changes: object) -> AgentTrace:
    fields: dict[str, object] = {
        "header": _header(),
        "turns": (_turn(1), _turn(2)),
        "native": PredictRLMNativeTrace(
            sub_model=None, max_iterations=5, usage=_NO_USAGE, telemetry_ref=None
        ),
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
        {"tool_call_count": 1},
    ],
    ids=["format-mismatch", "turn-count", "tool-call-count"],
)
def test_record_rejects_a_header_that_disagrees_with_its_body(header):
    with pytest.raises(ValidationError):
        _record(header=_header(**header))


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
        lambda body: body["native"].update(format="unknown/v1"),
        lambda body: body["header"].update(runtime_kind="unknown"),
        lambda body: body.update(schema_version=1),
        lambda body: body["turns"][0]["actions"][0].update(kind="unknown"),
        lambda body: body.update(extra=True),
    ],
    ids=["unknown-format", "unknown-runtime", "old-schema", "unknown-action", "extra-field"],
)
def test_json_body_rejects_unknown_shapes(change):
    body = _record().model_dump(mode="json")
    assert AgentTrace.model_validate_json(json.dumps(body)) == _record()
    change(body)

    with pytest.raises(ValidationError):
        AgentTrace.model_validate_json(json.dumps(body))


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
