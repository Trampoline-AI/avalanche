"""Thin definition inspection over the existing discovery provider."""

from __future__ import annotations

import argparse
import json
import sys

from pydantic import JsonValue

from avalanche.machine_spec import MachineSpec
from runtime.operator.client import OperatorCallError
from runtime.operator.models import WorkflowInfo


def add_definition_command(
    subcommands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    group = subcommands.add_parser("flow", help="Inspect flow and machine definitions.")
    commands = group.add_subparsers(dest="definition_command", required=True)
    for name in ("list", "show", "get"):
        command = commands.add_parser(name)
        if name != "list":
            command.add_argument("selector")
        command.add_argument("--connect", default="localhost:7433")
        command.add_argument("--json", action="store_true")
        command.set_defaults(handler=_handle)


def _machine(workflow: WorkflowInfo) -> MachineSpec | None:
    return (
        MachineSpec.model_validate_json(workflow.machine_spec_json)
        if workflow.machine_spec_json
        else None
    )


def _record(workflow: WorkflowInfo) -> dict[str, JsonValue]:
    machine = _machine(workflow)
    return {
        "selector": workflow.selector,
        "name": workflow.rendered_name,
        "kind": "machine" if machine is not None else "flow",
        "definition_file": workflow.file_path,
        "key_fields": list(workflow.key_fields),
        "machine": machine.model_dump(mode="json") if machine is not None else None,
    }


def _handle(args: argparse.Namespace) -> int:
    from .app import _make_provider

    provider = _make_provider(args.connect)
    try:
        workflows = provider.list_workflows()
        if args.definition_command == "list":
            if args.json:
                print(json.dumps([_record(workflow) for workflow in workflows]))
            else:
                print("TYPE\tSELECTOR\tNAME")
                for workflow in workflows:
                    kind = "machine" if workflow.machine_spec_json else "flow"
                    print(f"{kind}\t{workflow.selector}\t{workflow.rendered_name}")
        else:
            matches = [
                workflow for workflow in workflows
                if args.selector in {workflow.selector, workflow.name}
            ]
            if len(matches) != 1:
                raise ValueError("Select one definition by its discovery selector")
            workflow = matches[0]
            machine = _machine(workflow)
            if args.json:
                print(json.dumps(_record(workflow)))
            else:
                kind = "machine" if machine is not None else "flow"
                print(f"Kind: {kind}\nDefinition: {workflow.selector}")
                print(f"Key fields: {', '.join(workflow.key_fields) or '(none)'}")
                if machine is not None:
                    print("States: " + ", ".join(machine.states))
        return 0
    except (OperatorCallError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        provider.close()
