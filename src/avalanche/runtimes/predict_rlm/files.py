"""The sole bridge between public Avalanche refs and PredictRLM's path types."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Annotated, Union, get_args, get_origin
from uuid import uuid4

import dspy
from predict_rlm import File as PredictFile
from predict_rlm import Workspace as PredictWorkspace

from avalanche.file import File
from avalanche.references import agent_output_workspace
from avalanche.runtime.context import get_current_run_context
from avalanche.workspace import Workspace


def _annotation(annotation):
    if annotation is File:
        return PredictFile
    if annotation is Workspace:
        return PredictWorkspace
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Annotated:
        return Annotated[_annotation(arguments[0]), *arguments[1:]]
    if origin in {Union, types.UnionType}:
        return Union[tuple(_annotation(item) for item in arguments)]
    if origin is not None and arguments:
        return origin[tuple(_annotation(item) for item in arguments)]
    return annotation


def predict_signature(signature: type[dspy.Signature]) -> type[dspy.Signature]:
    """Keep the declared public signature; translate only the internal predictor's."""
    translated = signature
    for name, field in signature.fields.items():
        annotation = _annotation(field.annotation)
        if annotation != field.annotation:
            translated = translated.with_updated_fields(name, type_=annotation)
    return translated


def mount_inputs(value):
    if isinstance(value, File):
        return PredictFile(path=str(value.path))
    if isinstance(value, Workspace):
        return PredictWorkspace(path=str(value.path), mode="direct", read_only=True)
    if isinstance(value, list):
        return [mount_inputs(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mount_inputs(item) for item in value)
    if isinstance(value, dict):
        return {key: mount_inputs(item) for key, item in value.items()}
    return value


class AgentFileBridge:
    def __init__(self, step_name: str):
        self.workspace, attempt = agent_output_workspace()
        context = get_current_run_context()
        step = context.node_slug if context is not None and context.node_slug else step_name
        for component in (step, attempt):
            if (
                not component
                or component in {".", ".."}
                or "/" in component
                or "\\" in component
            ):
                raise ValueError("Agent step and attempt must be single path components")
        # Multiple model calls in one node attempt must not overwrite prior refs.
        self.output_dir = self.workspace.path / ".outputs" / step / attempt / uuid4().hex
        self.output_dir.mkdir(parents=True, exist_ok=False)

    def outputs(self, value):
        if isinstance(value, PredictFile):
            if value.path is None:
                raise ValueError("Agent File output has no host path")
            path = Path(value.path).resolve(strict=True)
            if not path.is_relative_to(self.output_dir.resolve()):
                raise ValueError("Agent File output escaped the reserved step/attempt folder")
            return File.from_path(path, workspace=self.workspace)
        if isinstance(value, list):
            return [self.outputs(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.outputs(item) for item in value)
        if isinstance(value, dict):
            return {key: self.outputs(item) for key, item in value.items()}
        return value
