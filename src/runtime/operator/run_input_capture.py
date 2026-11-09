"""Capture long-lived keyed-run inputs with the ordinary file staging/binding path."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import JsonValue

from avalanche.file import File
from avalanche.references import LocalReferenceBinder
from avalanche.workspace import Workspace

from .state.runs import RunRequest

if TYPE_CHECKING:
    from .operator import Operator


def capture_keyed_input(op: Operator, request: RunRequest) -> RunRequest:
    binder = LocalReferenceBinder()
    with op.input_files.claims() as claims:

        def capture(value: JsonValue) -> JsonValue:
            if isinstance(value, dict):
                if value.get("kind") == "file" and "ref" in value:
                    reference = File.model_validate(value)
                    bound = reference.bind(binder.bind_file(reference))
                    staged = op.input_files.stage(
                        bound.name or "input.bin", bound.content_type or "", bound.read_bytes()
                    )
                    retained = claims.file(
                        staged.object_key, sha256=staged.sha256, size_bytes=staged.size_bytes
                    )
                    return retained.model_dump(mode="json")
                if value.get("kind") == "workspace":
                    workspace = Workspace.model_validate(value)
                    if workspace.is_reference:
                        workspace = Workspace.from_path(binder.bind_workspace(workspace))
                    return workspace.model_dump(mode="json")
                return {name: capture(child) for name, child in value.items()}
            if isinstance(value, list):
                return [capture(child) for child in value]
            return value

        values = (
            None
            if request.input is None
            else {name: capture(value) for name, value in request.input.items()}
        )
        return request.model_copy(
            update={"input": values, "input_files": request.input_files + claims.files}
        )
