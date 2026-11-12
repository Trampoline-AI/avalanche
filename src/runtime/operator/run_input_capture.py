"""Retain keyed-run references and materialize portable ingress once, not per visit."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue

from avalanche.references import prepare_run_references, validate_local_references
from avalanche.workspace import Workspace

from .state.runs import RunRequest

if TYPE_CHECKING:
    from .operator import Operator


def capture_keyed_input(op: Operator, request: RunRequest) -> RunRequest:
    def capture(value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            if value.get("kind") == "workspace" or set(value) == {"version", "entries"}:
                workspace = prepare_run_references(
                    Workspace.model_validate(value), root=op.input_files.root.parent / "inputs"
                )
                return workspace.manifest()
            return {name: capture(child) for name, child in value.items()}
        if isinstance(value, list):
            return [capture(child) for child in value]
        return value

    validate_local_references(request.input)
    return request.model_copy(update={"input": capture(request.input)})
