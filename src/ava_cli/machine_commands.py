"""Machine event ingress and ordinary run inspection over the local operator API."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

import grpc
from google.protobuf.json_format import MessageToJson

from runtime.operator.machine_control import MachineControlRequest
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc


def run_command(argv: list[str]) -> int:
    machine = argv[:2] == ["machine", "send"]
    parser = argparse.ArgumentParser(prog="ava " + " ".join(argv[:2]))
    if machine:
        parser.add_argument("machine")
        address = parser.add_mutually_exclusive_group(required=True)
        address.add_argument("--run")
        address.add_argument("--key", action="append", metavar="FIELD=JSON")
        parser.add_argument("event")
        parser.add_argument("--data", default="{}", help="JSON value or @file")
        parser.add_argument("--idempotency-key", default=None)
    else:
        if argv[1] != "list":
            parser.add_argument("run_id")
        else:
            parser.add_argument("--flow", default="")
        if argv[1] == "retry":
            parser.add_argument("--visit", action="store_true", required=True)
    parser.add_argument("--connect", default="localhost:7433")
    parser.add_argument("--json", action="store_true", help="emit JSON (the default)")
    args = parser.parse_args(argv[2:])
    from .app import _parse_assignment, _parse_json_object, _reject_json_constant

    try:
        with grpc.insecure_channel(args.connect) as channel:
            stub = pb_grpc.OperatorServiceV2Stub(channel)

            def control(command: MachineControlRequest):
                return json.loads(
                    stub.ControlMachine(
                        pb.ControlMachineRequestV2(request_json=command.model_dump_json()),
                        timeout=30,
                    ).result_json
                )

            if machine:
                run_id = args.run
                if run_id is None:
                    fields = {}
                    for assignment in args.key:
                        name, raw = _parse_assignment(assignment, "--key")
                        if name in fields:
                            raise ValueError(f"Duplicate key field {name}")
                        # Same JSON field grammar as flow input forms; no type coercion.
                        fields[name] = _parse_json_object(
                            "{" + json.dumps(name) + ":" + raw + "}", "--key"
                        )[name]
                    run_id = control(
                        MachineControlRequest(
                            op="resolve", machine=args.machine, key_fields=fields
                        )
                    )
                raw = (
                    Path(args.data[1:]).read_text() if args.data.startswith("@") else args.data
                )
                payload = json.loads(raw, parse_constant=_reject_json_constant)
                result = control(
                    MachineControlRequest(
                        op="send",
                        machine=args.machine,
                        run_id=run_id,
                        event=args.event,
                        payload=payload,
                        idempotency_key=args.idempotency_key or uuid4().hex,
                    )
                )
                print(json.dumps(result, allow_nan=False))
            elif argv[1] == "show":
                print(
                    MessageToJson(
                        stub.GetRunSnapshot(
                            pb.GetRunSnapshotRequestV2(run_id=args.run_id), timeout=30
                        ),
                        preserving_proto_field_name=True,
                    )
                )
            elif argv[1] == "list":
                print(
                    MessageToJson(
                        stub.ListRunSummaries(
                            pb.ListRunSummariesRequestV2(
                                workflow_selector=args.flow, page_size=100
                            ),
                            timeout=30,
                        ),
                        preserving_proto_field_name=True,
                    )
                )
            elif argv[1] == "cancel":
                stub.CancelRun(pb.CancelRunRequestV2(run_id=args.run_id), timeout=30)
                print("{}")
            elif argv[1] == "retry":
                control(MachineControlRequest(op="retry", run_id=args.run_id))
                print("{}")
            else:
                raise ValueError(
                    "Machine reruns are not supported; use a new key "
                    "or retry an interrupted visit"
                )
        return 0
    except (grpc.RpcError, ValueError, OSError) as exc:
        print(exc.details() if isinstance(exc, grpc.RpcError) else str(exc), file=sys.stderr)
        return 1
