"""The sole bridge between public Avalanche refs and PredictRLM's path types."""

from __future__ import annotations

import types
from typing import Annotated, Union, get_args, get_origin

import dspy
from predict_rlm import File as PredictFile
from predict_rlm import Workspace as PredictWorkspace
from pydantic import TypeAdapter

from avalanche.file import File
from avalanche.references import AgentOutputFolder
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
    def __init__(self, step_name: str, signature: type[dspy.Signature]):
        self._folder = AgentOutputFolder(step_name)
        self.workspace = self._folder.workspace
        self.output_dir = self._folder.path
        # PredictRLM's result event holds outputs as plain data; these restore its files.
        self._event_outputs = {
            name: TypeAdapter(field.annotation)
            for name, field in signature.output_fields.items()
        }

    def event_output(self, name, value):
        """One output of PredictRLM's result event, its files as ``ava.File`` references."""
        return self.outputs(self._event_outputs[name].validate_python(value))

    def outputs(self, value):
        if isinstance(value, PredictFile):
            if value.path is None:
                raise ValueError("Agent File output has no host path")
            return self._folder.file(value.path)
        if isinstance(value, list):
            return [self.outputs(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.outputs(item) for item in value)
        if isinstance(value, dict):
            return {key: self.outputs(item) for key, item in value.items()}
        return value
