"""Which signature fields hold files and workspaces, for runtimes that hand them over whole."""

from __future__ import annotations

import types
from dataclasses import dataclass
from typing import Annotated, Union, get_args, get_origin

from pydantic import BaseModel

from ..agent.contract import AgentRequest
from ..agent.steps import AgentStepError
from ..file import File
from ..workspace import Workspace


def mentions(annotation: object, target: type, seen: frozenset[type] = frozenset()) -> bool:
    """Whether ``target`` appears anywhere in a field type, including inside models."""
    if annotation is target:
        return True
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in seen:
            return False
        return any(
            mentions(field.annotation, target, seen | {annotation})
            for field in annotation.model_fields.values()
        )
    origin = get_origin(annotation)
    if origin is None:
        return False
    if origin is Annotated:
        return mentions(get_args(annotation)[0], target, seen)
    if origin in (Union, types.UnionType) or isinstance(origin, type):
        return any(mentions(argument, target, seen) for argument in get_args(annotation))
    return False


@dataclass(frozen=True)
class FileFields:
    """A signature's file and workspace fields, each typed exactly as a runtime takes it."""

    # The one ``ava.Workspace`` input, if any.
    workspace_input: str | None
    workspace_outputs: tuple[str, ...]
    # ``ava.File`` and ``list[ava.File]`` outputs; ``True`` for a single file.
    file_outputs: dict[str, bool]

    @classmethod
    def of(cls, request: AgentRequest, *, runtime: str) -> FileFields:
        """Check the request's file fields; other shapes that hold files are rejected."""
        signature = request.signature
        step = request.step_name
        for name, info in signature.input_model.model_fields.items():
            annotation = info.annotation
            if annotation in (File, Workspace, list[File]):
                continue
            if mentions(annotation, File) or mentions(annotation, Workspace):
                raise AgentStepError(
                    f"agent step {step!r}: {runtime} input {name!r} must be typed exactly "
                    "ava.File, list[ava.File] or ava.Workspace when it holds files"
                )
        workspace_inputs = [
            name
            for name, info in signature.input_model.model_fields.items()
            if info.annotation is Workspace
        ]
        if len(workspace_inputs) > 1:
            raise AgentStepError(
                f"agent step {step!r}: {runtime} accepts at most one ava.Workspace input, "
                f"got {workspace_inputs}"
            )
        outputs = signature.output_model.model_fields
        file_outputs = {
            name: info.annotation is File
            for name, info in outputs.items()
            if info.annotation in (File, list[File])
        }
        unsupported = [
            name
            for name, info in outputs.items()
            if name not in file_outputs
            and (
                mentions(info.annotation, File)
                or (info.annotation is not Workspace and mentions(info.annotation, Workspace))
            )
        ]
        if unsupported:
            raise AgentStepError(
                f"agent step {step!r}: {runtime} outputs that hold files must be typed exactly "
                f"ava.File, list[ava.File] or ava.Workspace {unsupported}"
            )
        return cls(
            workspace_input=workspace_inputs[0] if workspace_inputs else None,
            workspace_outputs=tuple(
                name for name, info in outputs.items() if info.annotation is Workspace
            ),
            file_outputs=file_outputs,
        )


def input_files(request: AgentRequest) -> list[File]:
    """Every ``ava.File`` input, in field order."""
    files: list[File] = []
    for name, info in request.signature.input_model.model_fields.items():
        value = getattr(request.inputs, name)
        if info.annotation is File:
            files.append(value)
        elif info.annotation == list[File]:
            files.extend(value)
    return files
