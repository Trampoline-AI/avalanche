"""Check keyed-run references and read file trees stored inline by earlier releases."""

from __future__ import annotations

from avalanche.references import read_stored_trees, validate_local_references

from .state.runs import RunRequest


def capture_keyed_input(request: RunRequest) -> RunRequest:
    captured = (
        None if request.input is None else read_stored_trees(request.input, own_copy=True)
    )
    validate_local_references(captured)
    return request.model_copy(update={"input": captured})
