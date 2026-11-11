"""Validate native machine input/event types in an isolated latest-source process.

The operator retains descriptors, never live tenant classes. JSON Schema cannot
validate runtime File/Workspace wire values, so reuse the machine's Pydantic types
at this import boundary instead of reverse-compiling schemas or forking validators.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from avalanche.machine import Machine
from avalanche.run_keys import derive_run_key

from .discovery import ConfiguredRoot
from .models import WorkflowDescriptor
from .source import resolve_live_source


class ValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    op: Literal["key", "input", "event"]
    root: str
    relative_file: str
    builder: str
    value: JsonValue
    state: str = ""
    event: str = ""


class MachineDefinitionUnavailableError(ValueError):
    """Source could not be imported/built; no payload validation took place."""


class MachineValidationProcessError(RuntimeError):
    """The validator exited without a definition or payload validation result."""


class ValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: JsonValue = None
    error: str | None = None
    definition_unavailable: bool = False


def validate_native(
    descriptor: WorkflowDescriptor,
    configured_root: ConfiguredRoot,
    op: Literal["key", "input", "event"],
    value: JsonValue,
    *,
    state: str = "",
    event: str = "",
) -> JsonValue:
    root, relative_file = resolve_live_source(configured_root, descriptor.locator)
    request = ValidationRequest(
        op=op,
        root=str(root),
        relative_file=relative_file,
        builder=descriptor.locator.builder_symbol,
        value=value,
        state=state,
        event=event,
    )
    completed = subprocess.run(
        [sys.executable, "-m", "runtime.operator.machine_validation"],
        input=request.model_dump_json(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise MachineValidationProcessError(
            f"Machine validation process exited with status {completed.returncode}"
        )
    response = ValidationResponse.model_validate_json(completed.stdout)
    if response.error is not None:
        if response.definition_unavailable:
            raise MachineDefinitionUnavailableError(response.error)
        raise ValueError(response.error)
    return response.value


def main() -> None:
    from .run_worker import _import_workflow_module

    request = ValidationRequest.model_validate_json(sys.stdin.read())
    # Keep incidental tenant-import output out of the control frame.
    from contextlib import redirect_stdout

    try:
        with redirect_stdout(sys.stderr):
            root = Path(request.root)
            sys.path.insert(0, str(root))
            try:
                module = _import_workflow_module(root, request.relative_file)
                builder = vars(module)[request.builder]
                workflow = builder()
            except Exception as exc:
                # This isolated tenant-code boundary distinguishes source/build
                # failures from a successfully loaded model refusing a payload.
                raise MachineDefinitionUnavailableError(f"{type(exc).__name__}: {exc}") from exc
            wire = json.dumps(request.value, allow_nan=False)
            if request.op == "key":
                if workflow.input_type is None or not workflow.key_fields:
                    raise ValueError("Workflow does not declare a run key")
                values = TypeAdapter(dict[str, JsonValue]).validate_python(request.value)
                value = list(derive_run_key(workflow.input_type, workflow.key_fields, values))
            elif request.op == "input":
                value = (
                    workflow.input_type.model_validate_json(wire, strict=True).model_dump(
                        mode="json"
                    )
                    if workflow.input_type is not None
                    else request.value
                )
            else:
                if not isinstance(builder, Machine):
                    raise ValueError("Target is not a machine")
                machine = builder
                if request.state not in machine.spec.states:
                    raise ValueError("state_missing")
                model = machine.payloads[request.state][request.event]
                if model is None:
                    if request.value not in (None, {}):
                        raise ValueError("event_payload_must_be_empty")
                    value = request.value
                else:
                    value = model.model_validate_json(wire, strict=True).model_dump(mode="json")
        response = ValidationResponse(value=value)
    except MachineDefinitionUnavailableError as exc:
        response = ValidationResponse(error=str(exc), definition_unavailable=True)
    except ValidationError as exc:
        issues = [
            ".".join(map(str, issue["loc"])) + ": " + issue["type"]
            for issue in exc.errors(include_input=False, include_url=False)
        ]
        response = ValidationResponse(
            error="Invalid machine input/payload: " + "; ".join(issues)
        )
    except (ValueError, KeyError) as exc:
        response = ValidationResponse(error=str(exc))
    sys.stdout.write(response.model_dump_json())


if __name__ == "__main__":
    main()
