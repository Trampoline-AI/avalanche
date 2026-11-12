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
        parser.add_argument("machine", nargs="?", help="Required with --key; omit with --run")
        address = parser.add_mutually_exclusive_group(required=True)
        address.add_argument("--run")
        address.add_argument("--key", action="append", metavar="FIELD=JSON")
        parser.add_argument("event")
        parser.add_argument("--data", default="{}", help="JSON value or @file")
        parser.add_argument("--idempotency-key", default=None)
    else:
        if argv[1] != "list":
            parser.add_argument("run_id", nargs="?")
            parser.add_argument("--flow", default="")
            parser.add_argument("--key", action="append", metavar="FIELD=JSON")
        else:
            parser.add_argument("--flow", default="")
        if argv[1] == "retry":
            parser.add_argument("--entry", action="store_true", required=True)
    if not machine and argv[1] == "history":
        parser.add_argument("--limit", type=int, default=20, choices=range(1, 101))
        parser.add_argument("--cursor")
    parser.add_argument("--connect", default="localhost:7433")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_intermixed_args(argv[2:])
    from .app import _parse_assignment, _parse_json_object, _reject_json_constant

    try:
        if machine:
            if args.run is not None and args.machine is not None:
                raise ValueError("Use machine send EVENT --run RUN; omit the machine name")
            if args.run is None and args.machine is None:
                raise ValueError("Named --key fields require a machine name")
        with grpc.insecure_channel(args.connect) as channel:
            stub = pb_grpc.OperatorServiceV2Stub(channel)

            def control(command: MachineControlRequest):
                return json.loads(
                    stub.ControlMachine(
                        pb.ControlMachineRequestV2(request_json=command.model_dump_json()),
                        timeout=30,
                    ).result_json
                )

            if machine or argv[1] != "list":
                run_id = args.run if machine else args.run_id
                if run_id is not None and args.key:
                    raise ValueError("Use a run ID or --key, not both")
                if run_id is None:
                    selector = args.machine if machine else args.flow
                    if not selector or not args.key:
                        raise ValueError("Provide a run ID or --flow and --key")
                    fields = {}
                    for assignment in args.key:
                        name, raw = _parse_assignment(assignment, "--key")
                        if name in fields:
                            raise ValueError(f"Duplicate key field {name}")
                        # Same JSON field grammar as flow input forms; no type coercion.
                        fields[name] = _parse_json_object(
                            "{" + json.dumps(name) + ":" + raw + "}", "--key"
                        )[name]
                    run_id = stub.ResolveRun(
                        pb.ResolveRunRequestV2(
                            workflow_selector=selector, key_fields_json=json.dumps(fields)
                        ),
                        timeout=30,
                    ).run_id
            if machine:
                raw = (
                    Path(args.data[1:]).read_text() if args.data.startswith("@") else args.data
                )
                payload = json.loads(raw, parse_constant=_reject_json_constant)
                result = control(
                    MachineControlRequest(
                        op="send",
                        machine=args.machine or "",
                        run_id=run_id,
                        event=args.event,
                        payload=payload,
                        idempotency_key=args.idempotency_key or uuid4().hex,
                    )
                )
                print(json.dumps(result, allow_nan=False))
            elif argv[1] == "links":
                print(json.dumps(control(MachineControlRequest(op="links", run_id=run_id))))
            elif argv[1] == "show":
                snapshot = stub.GetRunSnapshot(
                    pb.GetRunSnapshotRequestV2(run_id=run_id), timeout=30
                )
                if args.json:
                    print(MessageToJson(snapshot, preserving_proto_field_name=True))
                else:
                    print(f"Kind: {snapshot.summary.kind}")
                    print(MessageToJson(snapshot, preserving_proto_field_name=True))
            elif argv[1] == "list":
                request = pb.ListRunSummariesRequestV2(
                    workflow_selector=args.flow, page_size=100
                )
                runs = []
                while True:
                    page = stub.ListRunSummaries(request, timeout=30)
                    runs.extend(page.runs)
                    if not page.HasField("next_page"):
                        break
                    request.continuation.CopyFrom(page.next_page)
                if args.json:
                    print(json.dumps({"runs": [
                        json.loads(MessageToJson(run, preserving_proto_field_name=True))
                        for run in runs
                    ]}))
                else:
                    print("TYPE\tRUN\tDEFINITION\tSTATUS")
                    for run in runs:
                        print(f"{run.kind}\t{run.run_id}\t{run.workflow_selector}\t{run.status}")
            elif argv[1] == "history":
                result = control(MachineControlRequest(
                    op="history", run_id=run_id, limit=args.limit, cursor=args.cursor
                ))
                print(json.dumps(result))
            elif argv[1] == "cancel":
                stub.CancelRun(pb.CancelRunRequestV2(run_id=run_id), timeout=30)
                print("{}")
            elif argv[1] == "retry":
                control(MachineControlRequest(op="retry", run_id=run_id))
                print("{}")
            else:
                raise ValueError(
                    "Machine reruns are not supported; use a new key "
                    "or retry an interrupted state entry"
                )
        return 0
    except (grpc.RpcError, ValueError, OSError) as exc:
        print(exc.details() if isinstance(exc, grpc.RpcError) else str(exc), file=sys.stderr)
        return 1
