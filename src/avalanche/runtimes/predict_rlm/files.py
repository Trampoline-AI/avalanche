"""TEMPORARY bridge between ``ava.File`` and PredictRLM's path-based files.

``ava.File`` carries a file's bytes; ``predict_rlm.File`` names a host path
that PredictRLM mounts into its sandbox (inputs) or syncs back from it
(outputs). Until the file model is reworked, PredictRLM steps declare
``ava.File`` like every other runtime and this bridge converts at the
adapter: input bytes are written to a per-invocation directory, and output
files are read back into ``ava.File`` values. Delete this module once
``ava.File`` can hand PredictRLM a reference it mounts directly.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Union, get_args, get_origin

import dspy
from predict_rlm import File as PredictRLMFile
from pydantic.fields import FieldInfo

from ...agent.steps import AgentStepError
from ...runtime.context import File

FileShape = Literal["one", "many"]


def file_fields(
    signature: type[dspy.Signature], fields: Mapping[str, FieldInfo]
) -> dict[str, FileShape]:
    """Fields declared as ``ava.File`` or ``list[ava.File]``, and which of the two."""
    shapes: dict[str, FileShape] = {}
    for name, field in fields.items():
        annotation = field.annotation
        if annotation is File:
            shapes[name] = "one"
        elif get_origin(annotation) is list and get_args(annotation) == (File,):
            shapes[name] = "many"
        elif _mentions_file(annotation):
            raise AgentStepError(
                f"agent signature {signature.__name__} field {name!r}: PredictRLM accepts "
                "ava.File only as the whole field type, ava.File or list[ava.File]"
            )
    return shapes


def predictor_signature(signature: type[dspy.Signature]) -> type[dspy.Signature]:
    """The signature PredictRLM is built with: ``ava.File`` fields become path files."""
    bridged = signature
    for name, shape in file_fields(signature, signature.fields).items():
        bridged = bridged.with_updated_fields(
            name, type_=PredictRLMFile if shape == "one" else list[PredictRLMFile]
        )
    return bridged


def host_inputs(
    inputs: Mapping[str, object], shapes: Mapping[str, FileShape], directory: Path
) -> dict[str, object]:
    """Write ``ava.File`` inputs under ``directory`` and pass their paths instead."""
    written = 0

    def write(file: File) -> PredictRLMFile:
        nonlocal written
        written += 1
        # One subdirectory per file keeps the original name without collisions.
        target = directory / str(written) / Path(file.name or "file").name
        target.parent.mkdir()
        target.write_bytes(file.content)
        return PredictRLMFile(path=str(target))

    converted = dict(inputs)
    for name, shape in shapes.items():
        value = inputs[name]
        if shape == "one":
            assert isinstance(value, File)
            converted[name] = write(value)
        else:
            assert isinstance(value, list)
            converted[name] = [write(file) for file in value]
    return converted


def avalanche_outputs(
    values: Mapping[str, object], shapes: Mapping[str, FileShape]
) -> dict[str, object]:
    """Read PredictRLM's output files back into ``ava.File`` values."""
    converted = dict(values)
    for name, shape in shapes.items():
        if name not in values:
            continue
        value = values[name]
        if shape == "one":
            converted[name] = _read(name, value)
        elif isinstance(value, list):
            converted[name] = [_read(name, item) for item in value]
        else:
            raise AgentStepError(
                f"PredictRLM returned {type(value).__name__} for list[ava.File] "
                f"output {name!r}"
            )
    return converted


def _read(name: str, value: object) -> File:
    if not isinstance(value, PredictRLMFile) or value.path is None:
        raise AgentStepError(
            f"PredictRLM returned {type(value).__name__} without a host path "
            f"for ava.File output {name!r}"
        )
    return File.from_path(value.path)


def _mentions_file(annotation: object) -> bool:
    if annotation is File:
        return True
    origin = get_origin(annotation)
    if origin is None:
        return False
    if origin in (Union, types.UnionType) or isinstance(origin, type):
        return any(_mentions_file(argument) for argument in get_args(annotation))
    return False
