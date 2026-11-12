"""Runs, their nodes and sequences survive operator restarts; unfinished runs are failed."""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import grpc
import pytest

from runtime.operator import run_processes
from runtime.operator.client import GrpcStateProvider
from runtime.operator.input_files import InputFileStaging, StagedInputFile
from runtime.operator.models import (
    NodeState,
    NodeStatus,
    RunInput,
    RunState,
    RunStatus,
    TerminalSealDescriptor,
    TriggerKind,
    WorkflowTopology,
)
from runtime.operator.operator import OPERATOR_RESTARTED_REASON, Operator, RunAlreadyExistsError
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.run_processes import (
    RunProcess,
    RunProcessLookupError,
    RunProcessStopError,
    identify_run_process,
    stop_run_process,
)
from runtime.operator.server import serve as serve_operator
from runtime.operator.state import OperatorStateStore, StateRowError, StateStoreError
from runtime.operator.state.metadata import OperatorMetadataTable
from runtime.operator.state.runs import RunRequest, RunsTable, StoredRun
from runtime.operator.step_io import StepIo, StepValuePreview, StepValuePreviewKind

_FLOWS = """import time

import avalanche as ava


class Greeting(ava.BaseInput):
    name: str = "world"


class Tenant(ava.RunContext):
    tenant: str = "none"


@ava.source
def greet(payload: Greeting, ctx: Tenant) -> str:
    return f"hello {payload.name} from {ctx.tenant}"


@ava.step
def shout(text: str) -> str:
    return text.upper()


@ava.workflow(input=Greeting, context=Tenant)
def quick():
    return shout(greet())


class Document(ava.BaseInput):
    document: ava.File


@ava.source
def read_document(payload: Document) -> str:
    return payload.document.read_bytes().decode()


@ava.workflow(input=Document)
def documents():
    return read_document()


@ava.source
def wait_long() -> int:
    time.sleep(300)
    return 1


@ava.step
def after_wait(value: int) -> int:
    return value


@ava.workflow
def sleepy():
    return after_wait(wait_long())
"""

# Runs an operator in its own process, starts the sleepy workflow and reports the run
# once its first step is running, then waits to be killed.
_KILLED_OPERATOR = """
import json, sys, time
from runtime.operator.models import NodeStatus
from runtime.operator.operator import Operator

operator = Operator(
    [sys.argv[1]], watch=False, schedule=False, state_db=sys.argv[2], prepare_timeout=120
)
run_id = operator.start_run("sleepy")
deadline = time.monotonic() + 120
while operator.get_run(run_id).nodes.get("wait_long_1") is None or (
    operator.get_run(run_id).nodes["wait_long_1"].status is not NodeStatus.RUNNING
):
    assert time.monotonic() < deadline, "sleepy run did not start"
    time.sleep(0.05)
print(json.dumps({
    "run_id": run_id,
    "coordinator": operator._active_runs[run_id].process.pid,
    "sequence": operator.current_sequence,
}), flush=True)
sys.stdin.read()
"""

_MAP_FLOW = """import avalanche as ava
@ava.source
def items():
    return [2, 3, 5]
@ava.step
def scale(item):
    return item * 10
@ava.step
def edit(item):
    return item + 1
@ava.workflow
def mapped():
    return edit.map(scale.map(items()))
"""


def test_mapped_run_topology_and_item_metadata_survive_restart(tmp_path):
    path = tmp_path / "mapped.py"
    path.write_text(_MAP_FLOW)
    db = tmp_path / "operator.sqlite"
    operator = _operator(path, db)
    try:
        run_id = operator.start_run("mapped")
        run = _wait_terminal(operator, run_id)
        assert run.status is RunStatus.SUCCESS
        assert run.expansions == {"scale_1": 3, "edit_1": 3}
        assert run.nodes["edit_1[2]"].map_index == 2
    finally:
        operator.close()
    restarted = _operator(path, db)
    try:
        restored = restarted.get_run(run_id)
        assert restored.status is RunStatus.SUCCESS
        assert restored.expansions == {"scale_1": 3, "edit_1": 3}
        assert restored.nodes["edit_1[2]"].map_parent == "edit_1"
        assert restored.nodes["edit_1[2]"].map_index == 2
        assert set(restored.nodes) == set(run.nodes)
    finally:
        restarted.close()


_TERMINAL = {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED}


@pytest.fixture
def flows(tmp_path) -> Path:
    path = tmp_path / "flows.py"
    path.write_text(_FLOWS)
    return path


def _operator(flows: Path, state_db: Path) -> Operator:
    # A generous preparation timeout keeps these tests steady on a busy machine.
    return Operator(
        [str(flows)],
        watch=False,
        schedule=False,
        webhook_port=0,
        state_db=state_db,
        prepare_timeout=120,
    )


def _wait_terminal(operator: Operator, run_id: str) -> RunState:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run is not None and run.status in _TERMINAL:
            return run
        time.sleep(0.03)
    pytest.fail(f"Run {run_id} did not finish")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _process_alive(pid: int) -> bool:
    state = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_until(predicate, seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.05)


def _node_view(run: RunState) -> list[tuple]:
    return [
        (node.node_id, node.status, node.started_at, node.ended_at, node.error, node.step_io)
        for node in run.nodes.values()
    ]


# ── Adapter ──────────────────────────────────────────────


def _stored_run() -> StoredRun:
    run = RunState(
        run_id="run-1",
        flow_name="Quick",
        status=RunStatus.RUNNING,
        triggered_at=1_700_000_000.5,
        started_at=12.25,
        triggered_by=TriggerKind.API,
        workflow_id="flows.py::quick",
        workflow_display_name="Quick",
        topology=WorkflowTopology(
            node_ids=("a", "b"),
            graph=(("a", ()), ("b", ("a",))),
            node_types=(("a", "source"), ("b", "step")),
            display_names=(("a", "A"), ("b", "B")),
            input_declaration_json='{"fields": []}',
        ),
        input=RunInput(values_json='{"name": "x"}', supplied_fields=("name",)),
    )
    run.nodes["a"] = NodeState(
        "a",
        "A",
        "source",
        status=NodeStatus.SUCCESS,
        started_at=12.5,
        ended_at=13.0,
        step_io=StepIo(
            inputs=(),
            output=StepValuePreview(
                kind=StepValuePreviewKind.JSON,
                type_name="str",
                preview_json='"x"',
                truncated=False,
            ),
        ),
    )
    run.nodes["b"] = NodeState("b", "B", "step", status=NodeStatus.RUNNING, started_at=13.0)
    return StoredRun(
        run=run,
        created_sequence=4,
        revision=9,
        activity_sequence=2,
        node_revisions={"a": 8, "b": 9},
    )


def test_runs_table_round_trips_runs_nodes_requests_and_processes():
    stored = _stored_run()
    request = RunRequest(
        input={"name": "x", "file": {"kind": "file", "size": 3}},
        context={"tenant": "local"},
        input_files=(StagedInputFile("a" * 32 + "/notes.txt", "notes.txt", "", "b" * 64, 3),),
    )
    process = RunProcess(
        process_group=4321,
        started="Mon Nov  2 10:00:00 2026",
        booted="Thu Oct  8 03:04:29 2026",
    )
    store = OperatorStateStore.open(":memory:")
    try:
        with store.transaction() as connection:
            RunsTable.create(connection, stored, request, process)
        run = stored.run
        run.status = RunStatus.FAILED
        run.ended_at = 14.0
        run.nodes["b"].status = NodeStatus.FAILED
        run.nodes["b"].error = "boom"
        run.terminal_seal = TerminalSealDescriptor(
            activity_id="terminal_seal:run-1:3",
            run_sequence=3,
            timestamp=datetime(2026, 11, 2, 10, 0, 1),
            terminal_status=RunStatus.FAILED,
            reason="why",
        )
        updated = StoredRun(
            run=run,
            created_sequence=4,
            revision=11,
            activity_sequence=3,
            node_revisions={"a": 8, "b": 11},
        )
        with store.transaction() as connection:
            RunsTable.update(connection, updated, ("b",))
        with store.transaction() as connection:
            (loaded,) = RunsTable.load_all(connection)
            stored_request = RunsTable.read_request(connection, "run-1")
    finally:
        store.close()

    assert loaded.stored.run == run
    assert list(loaded.stored.run.nodes) == ["a", "b"]
    assert (loaded.stored.created_sequence, loaded.stored.revision) == (4, 11)
    assert loaded.stored.activity_sequence == 3
    assert dict(loaded.stored.node_revisions) == {"a": 8, "b": 11}
    assert loaded.process == process
    assert stored_request == request


def test_a_stored_run_that_does_not_match_its_model_is_an_error_not_skipped():
    store = OperatorStateStore.open(":memory:")
    try:
        with store.transaction() as connection:
            RunsTable.create(connection, _stored_run(), RunRequest(), None)
            connection.execute("UPDATE runs SET topology_json = '{\"graph\": 3}'")
        with pytest.raises(StateRowError), store.transaction() as connection:
            RunsTable.load_all(connection)
    finally:
        store.close()


# ── Restart ──────────────────────────────────────────────


def test_finished_runs_survive_a_restart_with_their_state_revisions_and_sequences(
    flows, tmp_path
):
    state_db = tmp_path / "state" / "state.db"
    first = _operator(flows, state_db)
    try:
        run_id = first.start_run("quick", input={"name": "ada"}, context={"tenant": "t"})
        before = _wait_terminal(first, run_id)
        summaries = first.list_run_summaries().runs
        snapshot = first.get_latest_run_snapshot(
            run_id, operator_instance_id=first.operator_instance_id
        )
        sequence = first.current_sequence
        old_instance = first.operator_instance_id
    finally:
        first.close()

    second = _operator(flows, state_db)
    try:
        assert second.operator_instance_id != old_instance
        assert second.current_sequence >= sequence
        after = second.get_run(run_id)
        assert after.status is RunStatus.SUCCESS
        assert _node_view(after) == _node_view(before)
        assert after.input == before.input
        assert after.topology == before.topology
        assert after.terminal_seal == before.terminal_seal
        assert second.list_run_summaries().runs == summaries
        restored = second.get_latest_run_snapshot(
            run_id, operator_instance_id=second.operator_instance_id
        )
        assert restored.summary == snapshot.summary
        assert [
            (node.node_id, node.status, node.revision, node.step_io) for node in restored.nodes
        ] == [
            (node.node_id, node.status, node.revision, node.step_io) for node in snapshot.nodes
        ]
        assert restored.as_of_sequence >= snapshot.as_of_sequence

        # New publications continue after every sequence the first operator used.
        next_id = second.start_run("quick")
        _wait_terminal(second, next_id)
        assert second.list_run_summaries().runs[0].created_sequence > sequence
        with pytest.raises(RunAlreadyExistsError):
            second.start_run("quick", run_id=run_id)
        with second._state_store.transaction() as connection:
            request = RunsTable.read_request(connection, run_id)
        assert request == RunRequest(input={"name": "ada"}, context={"tenant": "t"})
    finally:
        second.close()


def test_keyed_flow_start_restart_and_expired_history(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    fixture = Path(__file__).parents[1] / "fixtures" / "local_machine.py"
    state_db = tmp_path / "state.db"
    journal = tmp_path / "flow.txt"
    address = f"127.0.0.1:{(port := _free_port())}"
    command = [
        sys.executable,
        "-m",
        "ava_cli",
        "operator",
        str(fixture),
        "--port",
        str(port),
        "--webhook-port",
        "0",
        "--no-web",
        "--state-db",
        str(state_db),
    ]

    def start(stub, proposed_id, values, attachments=()):
        return stub.StartRun(
            pb.StartRunRequestV2(
                workflow_selector="ordinary_child",
                run_id=proposed_id,
                input_json=json.dumps(values),
                input_files=attachments,
            ),
            timeout=60,
        )

    def stop(process):
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)

    # Separate operator process, real spawned workflow work, real gRPC and CLI.
    with (tmp_path / "operator.log").open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            with grpc.insecure_channel(address) as channel:
                grpc.channel_ready_future(channel).result(timeout=30)
                stub = pb_grpc.OperatorServiceV2Stub(channel)
                values = {"ticket": "123", "journal": str(journal)}
                with ThreadPoolExecutor(max_workers=2) as callers:
                    responses = list(
                        callers.map(lambda i: start(stub, f"flow-key-{i}", values), range(2))
                    )
                run_id = responses[0].run_id
                assert {value.run_id for value in responses} == {run_id}
                assert all(value.key_json == "[123]" for value in responses)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    snapshot = stub.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run_id))
                    if snapshot.summary.status in {"success", "failed", "cancelled"}:
                        break
                    time.sleep(0.05)
                assert snapshot.summary.status == "success", snapshot
                assert len(stub.ListRunSummaries(pb.ListRunSummariesRequestV2()).runs) == 1
                assert journal.read_text().splitlines() == [f"{run_id}:observe_1"]
                # Replay validates only the typed key, not new input or stale uploads.
                replay = start(
                    stub,
                    "unused",
                    {"ticket": 123, "pause": "invalid"},
                    [pb.FileAttachmentV2(field_name="unknown", object_key="expired")],
                )
                assert replay.run_id == run_id
                with pytest.raises(grpc.RpcError) as invalid:
                    start(stub, "invalid", {"ticket": {"invalid": True}})
                assert invalid.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        finally:
            stop(process)

        for expired in (False, True):
            if expired:
                deleted = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "ava_cli",
                        "state",
                        "delete",
                        run_id,
                        "--state-db",
                        str(state_db),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert deleted.returncode == 0, deleted.stderr
            process = subprocess.Popen(command, stdout=log, stderr=log)
            try:
                with grpc.insecure_channel(address) as channel:
                    grpc.channel_ready_future(channel).result(timeout=30)
                    stub = pb_grpc.OperatorServiceV2Stub(channel)
                    replay = start(stub, "after-restart", {"ticket": 123})
                    assert replay.run_id == run_id
                    assert replay.read_state == ("expired" if expired else "retained")
                    resolved = stub.ResolveRun(
                        pb.ResolveRunRequestV2(
                            workflow_selector="ordinary_child", key_fields_json='{"ticket":123}'
                        ),
                        timeout=30,
                    )
                    assert resolved == replay
                    shown = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "ava_cli",
                            "run",
                            "show",
                            "--flow",
                            "ordinary_child",
                            "--key",
                            "ticket=123",
                            "--connect",
                            address,
                            "--json",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    assert shown.returncode == 0, shown.stderr
                    summary = json.loads(shown.stdout)["summary"]
                    assert summary["run_id"] == run_id
                    assert summary["read_state"] == replay.read_state
                    assert len(stub.ListRunSummaries(pb.ListRunSummariesRequestV2()).runs) == 1
                    assert journal.read_text().splitlines() == [f"{run_id}:observe_1"]
            finally:
                stop(process)


def test_machine_file_and_workspace_inputs_belong_to_the_run_after_restart(tmp_path):
    source = Path(__file__).parents[1] / "fixtures" / "local_machine.py"
    fixture = tmp_path / "local_machine.py"
    fixture.write_text(source.read_text())
    state_db = tmp_path / "state.db"
    journal = tmp_path / "artifacts.txt"
    document = tmp_path / "document.txt"
    document.write_text("document")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.txt").write_text("captured")
    op = Operator(
        [str(fixture)], watch=False, schedule=False, webhook_port=0, state_db=state_db
    )
    server = serve_operator(op, port=(port := _free_port()), block=False)
    try:
        created = subprocess.run(
            [
                sys.executable,
                "-m",
                "ava_cli",
                "run",
                "artifact_approval",
                "--connect",
                f"127.0.0.1:{port}",
                "--input",
                json.dumps({"ticket": 11, "journal": str(journal)}),
                "--file",
                f"document={document}",
                "--workspace",
                f"workspace={workspace}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert created.returncode == 0, created.stderr
        run_id = created.stdout.strip()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and op.get_run(run_id).status != RunStatus.WAITING:
            time.sleep(0.03)
        assert op.get_run(run_id).status == RunStatus.WAITING
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb_grpc.OperatorServiceV2Stub(channel)
            existing = stub.StartRun(
                pb.StartRunRequestV2(
                    run_id="unused-id",
                    workflow_selector=op._registry.resolve("artifact_approval").workflow_id,
                    input_json='{"ticket":11}',
                    context_json="{}",
                    input_files=[
                        pb.FileAttachmentV2(field_name="document", object_key="expired")
                    ],
                )
            )
            assert existing.run_id == run_id
        from avalanche.results import RunResult

        visit_value = op.get_run_result(run_id, visit=1)
        step_value = op.get_run_result(run_id, visit=1, node_id="inspect_artifacts_1")
        assert visit_value == step_value == {"text": "document:captured", "counts": [1, 2, 3]}
        readback = subprocess.run(
            [
                sys.executable,
                "-m",
                "ava_cli",
                "run",
                "result",
                run_id,
                "--wait",
                "--visit",
                "1",
                "--node",
                "inspect_artifacts_1",
                "--json",
                "--connect",
                f"127.0.0.1:{port}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert readback.returncode == 0, readback.stderr
        assert RunResult.model_validate_json(readback.stdout).decode() == step_value
        import urllib.error
        import urllib.request

        from runtime.operator.web import start_browser_server

        browser = start_browser_server(f"127.0.0.1:{port}", port=0)
        base = f"http://{browser.host}:{browser.port}/api/v1/runs/{run_id}/result"
        try:
            with urllib.request.urlopen(
                base + "?visit=1&node_id=inspect_artifacts_1", timeout=30
            ) as response:
                assert response.status == 200
                assert json.loads(response.read()) == json.loads(readback.stdout)
            try:
                urllib.request.urlopen(base, timeout=30)
            except urllib.error.HTTPError as pending:
                assert pending.code == 409
            else:
                raise AssertionError("pending machine result must be 409")
        finally:
            browser.close()
        equivalent = subprocess.run(
            [
                sys.executable,
                "-m",
                "ava_cli",
                "run",
                "artifact_flow",
                "--connect",
                f"127.0.0.1:{port}",
                "--input",
                json.dumps({"ticket": 12, "journal": str(tmp_path / "flow.txt")}),
                "--file",
                f"document={document}",
                "--workspace",
                f"workspace={workspace}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert equivalent.returncode == 0, equivalent.stderr
        flow_id = equivalent.stdout.strip()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and op.get_run(flow_id).status != RunStatus.SUCCESS:
            time.sleep(0.03)
        assert op.get_run_result(flow_id) == visit_value
        assert op.get_run_result(flow_id, node_id="inspect_artifacts_1") == step_value
        document.unlink()
        (workspace / "data.txt").unlink()
        workspace.rmdir()
        server.stop(grace=0).wait()
        op.close()
        op = Operator(
            [str(fixture)], watch=False, schedule=False, webhook_port=0, state_db=state_db
        )
        assert op.get_run_result(run_id, visit=1) == visit_value
        assert op.get_run_result(run_id, visit=1, node_id="inspect_artifacts_1") == step_value
        op._machine_host.send(run_id, "approved", {}, "late-visit")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and op.get_run(run_id).status != RunStatus.SUCCESS:
            time.sleep(0.03)
        assert op.get_run(run_id).status == RunStatus.SUCCESS
        assert op.get_run_result(run_id) == op.get_run_result(run_id, visit=2) == visit_value
        assert journal.read_text().splitlines() == [
            "v1.inspect_artifacts_1:document:captured",
            "v2.inspect_artifacts_1:document:captured",
        ]
        op.close()
        expired = subprocess.run(
            [
                sys.executable,
                "-m",
                "ava_cli",
                "state",
                "delete",
                run_id,
                "--state-db",
                str(state_db),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert expired.returncode == 0, expired.stderr
        op = Operator(
            [str(fixture)], watch=False, schedule=False, webhook_port=0, state_db=state_db
        )
        assert op.start_run("artifact_approval", input={"ticket": 11}) == run_id
        from runtime.operator.machine_control import MachineControlRequest, control_machine

        history = control_machine(op, MachineControlRequest(op="history", run_id=run_id))
        assert history["read_state"] == "expired"
        with op._state_store.transaction() as connection:
            assert (
                connection.execute(
                    "SELECT key, read_state FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()["read_state"]
                == "expired"
            )
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert not names.intersection({"machine_instances", "machine_timers"})
        assert journal.read_text().splitlines() == [
            "v1.inspect_artifacts_1:document:captured",
            "v2.inspect_artifacts_1:document:captured",
        ]
        # Kind belongs to the retained run, even after expiry, restart and a
        # same-name catalog replacement with an ordinary flow.
        op.close()
        fixture.write_text(
            "import avalanche as ava\n"
            "@ava.step\ndef value():\n    return 1\n"
            "@ava.workflow\ndef artifact_approval():\n    return value()\n"
        )
        op = Operator(
            [str(fixture)], watch=False, schedule=False, webhook_port=0, state_db=state_db
        )
        server = serve_operator(op, port=port, block=False)
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb_grpc.OperatorServiceV2Stub(channel)
            (definition,) = stub.DiscoverFlows(pb.DiscoverFlowsRequestV2()).flows
            assert not definition.machine_spec_json
            summaries = stub.ListRunSummaries(pb.ListRunSummariesRequestV2()).runs
            assert {row.run_id for row in summaries} == {run_id, flow_id}
            summary = next(row for row in summaries if row.run_id == run_id)
            assert summary.kind == "machine"
            assert summary.read_state == "expired"
            snapshot = stub.GetRunSnapshot(pb.GetRunSnapshotRequestV2(run_id=run_id))
            assert snapshot.summary.kind == "machine" and snapshot.read_state == "expired"
    finally:
        server.stop(grace=0).wait()
        op.close()


_KILLED_MACHINE_OPERATOR = """
import json, sys, time
from pathlib import Path
from runtime.operator.operator import Operator
from runtime.operator.state.machines import MachinesTable
op = Operator([sys.argv[1]], watch=False, schedule=False, webhook_port=0, state_db=sys.argv[2])
mode, journal = sys.argv[4], sys.argv[3]
run_id = op.start_run("approval", input={
    "ticket": 9, "journal": journal, "pause": mode == "active"})
receipt = op._machine_host.send(run_id, "approved", {}, "before-crash")
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    with op._state_store.transaction() as c:
        record = MachinesTable.load(c, run_id)
    active = mode == "active" and Path(journal).exists()
    waiting = mode == "waiting" and record.visit == 2 and record.phase == "waiting"
    if active or waiting:
        break
    time.sleep(0.03)
else:
    raise AssertionError("machine did not reach crash point")
handle = op._active_runs.get(run_id)
with op._state_store.transaction() as c:
    timers = MachinesTable.timers(c, run_id, record.visit)
print(json.dumps({"run_id": run_id, "coordinator": handle.process.pid if handle else None,
                  "due": timers[0].due if timers else None,
                  "receipt": receipt.model_dump(mode="json")}), flush=True)
sys.stdin.read()
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_machine_timer_and_interrupted_visit_survive_sigkill(tmp_path):
    from runtime.operator.state.machines import MachinesTable

    fixture = tmp_path / "machine.py"
    fixture.write_text(
        (Path(__file__).parents[1] / "fixtures" / "local_machine.py")
        .read_text()
        .replace("seconds=2", "seconds=10")
    )
    for mode in ("waiting", "active"):
        root = tmp_path / mode
        root.mkdir()
        state_db, journal = root / "state.db", root / "visits.txt"
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _KILLED_MACHINE_OPERATOR,
                str(fixture),
                str(state_db),
                str(journal),
                mode,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        report = None
        op = None
        try:
            report = json.loads(child.stdout.readline())
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
            before = journal.read_text()
            if mode == "waiting":
                # Restart past the actual persisted entry deadline, not a guessed sleep.
                time.sleep(max(0, report["due"] - time.time()) + 0.1)
            op = Operator(
                [str(fixture)], watch=False, schedule=False, webhook_port=0, state_db=state_db
            )
            run_id = report["run_id"]
            replay = op._machine_host.send(run_id, "approved", {}, "before-crash")
            assert replay.duplicate and replay.receipt_id == report["receipt"]["receipt_id"]
            if mode == "active":
                assert not _process_alive(report["coordinator"])
                assert journal.read_text() == before  # No automatic side-effect replay.
                with op._state_store.transaction() as c:
                    instance = MachinesTable.load(c, run_id)
                    visit = MachinesTable.visit(c, run_id, 1)
                assert (
                    instance.run_status == RunStatus.WAITING
                    and instance.reason == "outcome_unknown"
                )
                assert visit.disposition == "indeterminate" and visit.snapshot is not None
                Path(str(journal) + ".release").touch()
                op._machine_host.retry(run_id)
            deadline = time.monotonic() + 30
            while (
                time.monotonic() < deadline and op.get_run(run_id).status != RunStatus.SUCCESS
            ):
                time.sleep(0.03)
            assert op.get_run(run_id).status == RunStatus.SUCCESS
            with op._state_store.transaction() as c:
                assert (
                    sum(
                        timer.fired_at is not None
                        for timer in MachinesTable.all_timers(c, run_id)
                    )
                    == 1
                )
                instance = MachinesTable.load(c, run_id)
            assert instance.visit == (3 if mode == "waiting" else 4)
            final_journal = journal.read_text()
            op.close()
            op = Operator(
                [str(fixture)], watch=False, schedule=False, webhook_port=0, state_db=state_db
            )
            assert op.get_run(run_id).status == RunStatus.SUCCESS
            assert journal.read_text() == final_journal
            assert op._machine_host.send(run_id, "approved", {}, "before-crash").duplicate
        finally:
            if op is not None:
                op.close()
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            child.stdout.close()
            child.stdin.close()
            if report and report["coordinator"] and _process_alive(report["coordinator"]):
                os.killpg(report["coordinator"], signal.SIGKILL)


@pytest.mark.parametrize("loss", ["deleted", "renamed", "syntax", "symbol", "build"])
@pytest.mark.parametrize("boundary", ["restart", "timer"])
def test_missing_machine_definition_parks_without_stopping_operator(tmp_path, loss, boundary):
    from runtime.operator.state.machines import MachinesTable

    root = tmp_path / "source"
    root.mkdir()
    fixture = root / "machine.py"
    source = (Path(__file__).parents[1] / "fixtures" / "local_machine.py").read_text()
    source = source.replace(
        "import avalanche as ava",
        "import avalanche as ava\n\nclass Approval(ava.BaseInput):\n    by: str",
    ).replace('pending.on("approved")', 'pending.on("approved", payload=Approval)')
    fixture.write_text(source.replace("seconds=2", "seconds=4"))
    db = tmp_path / "state.db"
    op = _operator(root, db)
    try:
        run_id = op.start_run(
            "approval", input={"ticket": 17, "journal": str(tmp_path / "visits")}
        )
        _wait_until(lambda: op.get_run(run_id).status is RunStatus.WAITING)
        if boundary == "timer":
            op._machine_host.send(run_id, "approved", {"by": "Ada"}, "advance")
            _wait_until(
                lambda: (
                    _machine_record(op, run_id).visit == 2
                    and _machine_record(op, run_id).phase == "waiting"
                )
            )
            with op._state_store.transaction() as c:
                due = MachinesTable.timers(c, run_id, 2)[0].due
        else:
            op.close()
            op = None
        if loss == "deleted":
            fixture.unlink()
        elif loss == "renamed":
            fixture.rename(root / "renamed.py")
        elif loss == "symbol":
            fixture.write_text(source.replace("def approval():", "def renamed_approval():"))
        elif loss == "build":
            fixture.write_text(
                source.replace(
                    "pending = ava.state(observe, initial=True)",
                    "raise ValueError('temporary build failure')",
                )
            )
        else:
            fixture.write_text("def invalid syntax:\n")
        if boundary == "restart":
            op = _operator(root, db)
        else:
            op._registry.scan([str(root)])
            time.sleep(max(0, due - time.time()) + 0.3)
        _wait_until(
            lambda: _machine_record(op, run_id).reason == "definition_unavailable", seconds=5
        )
        record = _machine_record(op, run_id)
        assert record.phase == "waiting" and record.run_status is RunStatus.WAITING
        assert op.fatal_error is None
        assert op.list_run_summaries().runs[0].run_id == run_id
        with op._state_store.transaction() as c:
            errors = c.execute(
                "SELECT record_json FROM machine_facts WHERE run_id=?", (run_id,)
            ).fetchall()
            assert any(
                (fact := json.loads(row[0]))["status"] == "definition_unavailable"
                and fact["reason"]
                for row in errors
            )
            if boundary == "timer":
                assert MachinesTable.timers(c, run_id, 2)[0].due == due
        if boundary == "restart":
            invalid = op._machine_host.send(run_id, "approved", {"by": 42}, "bad-payload")
            unknown = op._machine_host.send(run_id, "unknown", {}, "unknown-event")
            receipt = op._machine_host.send(
                run_id, "approved", {"by": "Ada"}, "while-unavailable"
            )
            assert invalid.status == unknown.status == receipt.status == "accepted"
            assert op._machine_host.send(
                run_id, "approved", {"by": "Ada"}, "while-unavailable"
            ).duplicate
            with op._state_store.transaction() as c:
                assert [row.receipt_id for row in MachinesTable.inbox(c, run_id)] == [
                    invalid.receipt_id,
                    unknown.receipt_id,
                    receipt.receipt_id,
                ]
            op.close()
            op = None
        if loss == "renamed":
            (root / "renamed.py").unlink()
        fixture.write_text(source.replace("seconds=2", "seconds=4"))
        if boundary == "restart":
            op = _operator(root, db)
            _wait_until(lambda: _machine_record(op, run_id).visit == 2)
        # Timer recovery deliberately does not rescan or restart the operator.
        _wait_until(lambda: _machine_record(op, run_id).reason is None)
        assert (
            op.start_run("approval", input={"ticket": 17, "journal": str(tmp_path / "visits")})
            == run_id
        )
        assert _wait_terminal(op, run_id).status is RunStatus.SUCCESS
        record = _machine_record(op, run_id)
        assert record.reason is None and record.visit == 3 and record.key == [17]
        with op._state_store.transaction() as c:
            fired = [
                timer
                for timer in MachinesTable.all_timers(c, run_id)
                if timer.fired_at is not None
            ]
            assert len(fired) == 1
            if boundary == "timer":
                assert fired[0].due == due
            else:
                facts = [
                    json.loads(row[0])
                    for row in c.execute(
                        "SELECT record_json FROM machine_facts WHERE run_id=?", (run_id,)
                    )
                ]
                assert any(
                    fact["status"] == "rejected"
                    and fact["reason"]
                    and fact["receipt_id"] == invalid.receipt_id
                    for fact in facts
                )
                assert any(
                    fact["status"] == "ignored" and fact["receipt_id"] == unknown.receipt_id
                    for fact in facts
                )
                assert (
                    sum(
                        fact["status"] == "consumed"
                        and fact["receipt_id"] == receipt.receipt_id
                        for fact in facts
                    )
                    == 1
                )
                assert not MachinesTable.inbox(c, run_id)
        assert op.fatal_error is None
    finally:
        if op is not None:
            op.close()


def test_stale_definition_retry_stalls_new_healthy_run(tmp_path):
    """Reviewer M1: repairing a cancelled run must not strand the next keyed run."""
    root = tmp_path / "source"
    root.mkdir()
    fixture = root / "machine.py"
    source = (Path(__file__).parents[1] / "fixtures" / "local_machine.py").read_text()
    fixture.write_text(source)
    op = Operator(
        [str(root)], watch=True, schedule=False, webhook_port=0, state_db=tmp_path / "state.db"
    )
    try:
        values = {"ticket": 1, "journal": str(tmp_path / "j")}
        first = op.start_run("approval", input=values)
        _wait_until(lambda: _machine_record(op, first).phase == "waiting")
        fixture.write_text("def invalid syntax:\n")
        _wait_until(lambda: _machine_record(op, first).reason == "definition_unavailable")
        op._machine_host.cancel(first)
        fixture.write_text(source)
        _wait_until(
            lambda: (
                not any(
                    d.kind in {"import_error", "build_error"}
                    for d in op._registry.view.diagnostics
                )
            )
        )
        time.sleep(2)
        print("retries after restore:", dict(op._machine_host._definition_retries))
        second = op.start_run("approval", input={**values, "ticket": 2})
        _wait_until(lambda: _machine_record(op, second).phase == "waiting")
        time.sleep(5)
        record = _machine_record(op, second)
        print("new healthy run:", record.phase, record.state, record.reason)
        assert record.reason is None
        assert op.fatal_error is None
    finally:
        op.close()


def test_send_storm_owner_latency(tmp_path):
    """Reviewer M2: eight concurrent sends must not sleep on the shared owner."""
    from concurrent.futures import ThreadPoolExecutor

    from runtime.operator.discovery import WorkflowDiscoveryError

    root = tmp_path / "source"
    root.mkdir()
    fixture = root / "machine.py"
    fixture.write_text(
        (Path(__file__).parents[1] / "fixtures" / "local_machine.py").read_text()
    )
    op = _operator(root, tmp_path / "state.db")
    try:
        values = {"ticket": 1, "journal": str(tmp_path / "j")}
        broken = op.start_run("approval", input=values)
        other = op.start_run("approval", input={**values, "ticket": 2})
        _wait_until(lambda: _machine_record(op, broken).phase == "waiting")
        _wait_until(lambda: _machine_record(op, other).phase == "waiting")
        fixture.write_text("def invalid syntax:\n")
        with pytest.raises(WorkflowDiscoveryError):
            op._refresh_workflows()
        op._machine_host.send(broken, "approved", {}, "prime")
        assert _machine_record(op, broken).reason == "definition_unavailable"
        calls = _count_machine_reloads(op)
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            sends = [
                pool.submit(op._machine_host.send, broken, "approved", {}, f"storm-{i}")
                for i in range(8)
            ]
            time.sleep(0.3)
            before_cancel = time.monotonic()
            op._machine_host.cancel(other)
            latency = time.monotonic() - before_cancel
            receipts = [send.result(timeout=30) for send in sends]
        elapsed = time.monotonic() - started
        print(
            "send storm:",
            {
                "sends": len(receipts),
                "total_s": elapsed,
                "refreshes": len(calls),
                "refresh_busy_s": sum(cost for _, cost in calls),
                "unrelated_cancel_latency_s": latency,
            },
        )
        assert all(receipt.status == "accepted" for receipt in receipts)
        assert op.fatal_error is None
        assert latency < 1.5
        assert len(calls) <= int(elapsed) + 1
        assert all(b[0] - a[0] >= 1.0 for a, b in zip(calls, calls[1:]))
    finally:
        op.close()


@pytest.mark.parametrize("failure", ["timeout", "unknown", "exit", "signal"])
def test_timer_inbox_validation_failure_is_bounded(tmp_path, monkeypatch, failure):
    """Real timer delivery, SQLite inbox and validation children, including crashes."""
    from runtime.operator import machine_validation
    from runtime.operator.discovery import WorkflowDiscoveryError
    from runtime.operator.state.machines import MachinesTable

    root = tmp_path / "source"
    root.mkdir()
    fixture = root / "machine.py"
    source = (Path(__file__).parents[1] / "fixtures" / "local_machine.py").read_text()
    source = source.replace(
        "import avalanche as ava",
        """import avalanche as ava
import os
import signal
from pydantic import model_validator

class Approval(ava.BaseInput):
    gate: str
    attempts: str
    failure: str

    @model_validator(mode="after")
    def check_process(self):
        with Path(self.attempts).open("a") as output:
            output.write(f"{time.time()}\\n")
        if Path(self.gate).exists():
            if self.failure == "exit":
                os._exit(23)
            if self.failure == "signal":
                os.kill(os.getpid(), signal.SIGKILL)
            if self.failure == "timeout":
                time.sleep(65)
        return self
""",
    ).replace('pending.on("approved")', 'pending.on("approved", payload=Approval)')
    source = source.replace(
        "    reviewed.after(",
        "    pending.after(timedelta(seconds=4)) >> pending\n    reviewed.after(",
    )
    fixture.write_text(source)
    gate = tmp_path / "fail-validation"
    gate.touch()
    attempts = tmp_path / "attempts"
    db = tmp_path / "state.db"
    op = _operator(root, db)
    try:
        run_id = op.start_run("approval", input={"ticket": 1, "journal": str(tmp_path / "j")})
        _wait_until(lambda: _machine_record(op, run_id).phase == "waiting")
        fixture.write_text("def invalid syntax:\n")
        with pytest.raises(WorkflowDiscoveryError):
            op._refresh_workflows()
        receipt = op._machine_host.send(
            run_id,
            "approved",
            {
                "gate": str(gate),
                "attempts": str(attempts),
                "failure": failure,
            },
            "queued-before-repair",
        )
        assert receipt.status == "accepted"
        fixture.write_text(source)
        op._registry.scan([str(root)])  # No wake: the persisted timer must deliver recovery.
        if failure == "timeout":
            run = machine_validation.subprocess.run

            def bounded_child(args, **kwargs):
                if args[1:] == ["-m", "runtime.operator.machine_validation"]:
                    kwargs["timeout"] = 1  # Real child timeout, not a synthetic exception.
                return run(args, **kwargs)

            monkeypatch.setattr(machine_validation.subprocess, "run", bounded_child)
        if failure == "unknown":
            validate = op._machine_host._validate_event

            def remove_during_lookup(record, name, payload):
                # Force the real registry race after _current_spec succeeded.
                monkeypatch.setattr(op._machine_host, "_validate_event", validate)
                fixture.rename(root / "removed.txt")
                op._registry.scan([str(root)])
                attempts.write_text(f"{time.time()}\n")
                try:
                    return validate(record, name, payload)
                finally:
                    (root / "removed.txt").rename(fixture)
                    op._registry.scan([str(root)])

            monkeypatch.setattr(op._machine_host, "_validate_event", remove_during_lookup)
        _wait_until(lambda: attempts.exists() or op.fatal_error is not None)

        def recovery_observed():
            with op._state_store.transaction() as c:
                facts = _machine_facts(c, run_id)
            return op.fatal_error is not None or any(
                fact.status == "definition_unavailable"
                and (
                    "validation" in (fact.reason or "").lower()
                    or "UnknownWorkflow" in (fact.reason or "")
                    or "TimeoutExpired" in (fact.reason or "")
                )
                for fact in facts
            )

        _wait_until(recovery_observed)
        assert op.fatal_error is None
        with op._state_store.transaction() as c:
            assert [row.receipt_id for row in MachinesTable.inbox(c, run_id)] == [
                receipt.receipt_id
            ]
        if failure in {"exit", "signal"}:
            _wait_until(lambda: len(attempts.read_text().splitlines()) >= 3)
            time.sleep(4)
            times = [float(line) for line in attempts.read_text().splitlines()]
            print("validation crash attempts:", failure, times)
            assert len(times) == 3
            assert times[1] - times[0] >= 1 and times[2] - times[1] >= 2
            with op._state_store.transaction() as c:
                facts = _machine_facts(c, run_id)
            assert any("validation_retry_limit" in (fact.reason or "") for fact in facts)
            op.close()
            op = _operator(root, db)
            time.sleep(2)
            assert len(attempts.read_text().splitlines()) == 3  # Cap survives restart.
        else:
            print("transient validation recovery:", failure, attempts.read_text())
        gate.unlink()
        if failure in {"exit", "signal"}:
            op._machine_host.retry(run_id)  # Explicit repair resets the crash budget.
        assert _wait_terminal(op, run_id).status is RunStatus.SUCCESS
        with op._state_store.transaction() as c:
            facts = _machine_facts(c, run_id)
            assert (
                sum(
                    f.status == "consumed" and f.receipt_id == receipt.receipt_id for f in facts
                )
                == 1
            )
            assert not MachinesTable.inbox(c, run_id)
            fired = [t for t in MachinesTable.all_timers(c, run_id) if t.fired_at]
            assert len(fired) == 2  # Overdue pending self-transition, then reviewed -> done.
        assert op.fatal_error is None
    finally:
        op.close()


def _count_machine_reloads(op):
    calls = []
    refresh = op._refresh_workflows

    def measured(*args, **kwargs):
        started = time.monotonic()
        try:
            return refresh(*args, **kwargs)
        finally:
            calls.append((started, time.monotonic() - started))

    op._refresh_workflows = measured
    return calls


@pytest.mark.parametrize("run_count", [1, 5])
def test_unavailable_machine_reload_cost_is_bounded(tmp_path, run_count):
    """Reviewer probe: real discovery cost must not scale with waiting run count."""
    from runtime.operator.state.machines import MachinesTable

    root = tmp_path / "source"
    root.mkdir()
    fixture = root / "machine.py"
    source = (Path(__file__).parents[1] / "fixtures" / "local_machine.py").read_text()
    fixture.write_text(source.replace("seconds=2", "seconds=15"))
    op = _operator(root, tmp_path / "state.db")
    try:
        runs = [
            op.start_run("approval", input={"ticket": n, "journal": str(tmp_path / "j")})
            for n in range(run_count)
        ]
        for run_id in runs:
            _wait_until(lambda: _machine_record(op, run_id).phase == "waiting")
            op._machine_host.send(run_id, "approved", {}, "advance")
        _wait_until(
            lambda: all(
                _machine_record(op, r).visit == 2 and _machine_record(op, r).phase == "waiting"
                for r in runs
            )
        )
        with op._state_store.transaction() as c:
            deadlines = {r: MachinesTable.timers(c, r, 2)[0].due for r in runs}
        fixture.unlink()
        op._registry.scan([str(root)])
        calls = _count_machine_reloads(op)
        _wait_until(
            lambda: all(
                _machine_record(op, r).reason == "definition_unavailable" for r in runs
            ),
            seconds=25,
        )
        start = time.monotonic()
        time.sleep(30)
        window = [(at, elapsed) for at, elapsed in calls if at >= start]
        print(
            "unavailable reload cost:",
            {
                "runs": run_count,
                "window_s": 30,
                "refreshes": len(window),
                "busy_s": sum(elapsed for _, elapsed in window),
                "offsets_s": [round(at - start, 2) for at, _ in window],
            },
        )
        fixture.write_text(source)
        restored = time.monotonic()
        _wait_until(
            lambda: all(op.get_run(r).status is RunStatus.SUCCESS for r in runs), seconds=65
        )
        print("timer recovery seconds:", round(time.monotonic() - restored, 2))
        with op._state_store.transaction() as c:
            for run_id in runs:
                fired = [t for t in MachinesTable.all_timers(c, run_id) if t.fired_at]
                assert len(fired) == 1 and fired[0].due == deadlines[run_id]
        assert op.fatal_error is None
        # 1, 2, 4, 8, 16, 32, ... delays: at most five attempts in this window,
        # regardless of how many runs share the unavailable machine definition.
        assert len(window) <= 5
    finally:
        op.close()


def test_unrelated_broken_source_does_not_reload_healthy_machine(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "machine.py").write_text(
        (Path(__file__).parents[1] / "fixtures" / "local_machine.py").read_text()
    )
    op = _operator(root, tmp_path / "state.db")
    try:
        (root / "other.py").write_text("import avalanche as ava\ndef broken(:\n")
        from runtime.operator.discovery import WorkflowDiscoveryError

        with pytest.raises(WorkflowDiscoveryError):
            op._refresh_workflows()
        assert any(d.kind == "import_error" for d in op._registry.view.diagnostics)
        calls = _count_machine_reloads(op)
        run_id = op.start_run("approval", input={"ticket": 9, "journal": str(tmp_path / "j")})
        _wait_until(lambda: op.get_run(run_id).status is RunStatus.WAITING)
        op._machine_host.send(run_id, "approved", {}, "advance")
        assert _wait_terminal(op, run_id).status is RunStatus.SUCCESS
        print("healthy machine with unrelated broken source refreshes:", len(calls))
        assert op.fatal_error is None
        assert not calls
    finally:
        op.close()


def test_watcher_reloads_wake_unavailable_machines_without_polling(tmp_path):
    """No timer or send is needed to recover queued work when watching source."""
    root = tmp_path / "source"
    root.mkdir()
    fixture = root / "machine.py"
    source = (Path(__file__).parents[1] / "fixtures" / "local_machine.py").read_text()
    fixture.write_text(source)
    op = Operator(
        [str(root)], watch=True, schedule=False, webhook_port=0, state_db=tmp_path / "state.db"
    )
    try:
        runs = [
            op.start_run("approval", input={"ticket": n, "journal": str(tmp_path / "j")})
            for n in range(3)
        ]
        _wait_until(lambda: all(_machine_record(op, r).phase == "waiting" for r in runs))
        calls = _count_machine_reloads(op)
        fixture.write_text("def invalid syntax:\n")
        _wait_until(
            lambda: all(_machine_record(op, r).reason == "definition_unavailable" for r in runs)
        )
        for run_id in runs:
            op._machine_host.send(run_id, "approved", {}, "queued")
        before = len(calls)
        time.sleep(3)
        assert len(calls) == before  # No owner polling while the watcher owns discovery.
        fixture.write_text(source)
        _wait_until(lambda: all(op.get_run(r).status is RunStatus.SUCCESS for r in runs))
        assert op.fatal_error is None
    finally:
        op.close()


@pytest.mark.parametrize("recovery", ["restart", "send", "retry"])
def test_transient_syntax_error_at_restart(tmp_path, recovery):
    """Reviewer reproduction: fix a typo, then recover the same keyed live run."""
    root = tmp_path / "source"
    root.mkdir()
    fixture = root / "machine.py"
    source = (Path(__file__).parents[1] / "fixtures" / "local_machine.py").read_text()
    fixture.write_text(source)
    db = tmp_path / "state.db"
    op = _operator(root, db)
    try:
        values = {"ticket": 31, "journal": str(tmp_path / "visits")}
        run_id = op.start_run("approval", input=values)
        _wait_until(lambda: op.get_run(run_id).status is RunStatus.WAITING)
        print("before:", _machine_record(op, run_id).phase)
        op.close()
        op = None
        fixture.write_text("def invalid syntax:\n")
        op = _operator(root, db)
        print("broken restart:", _machine_record(op, run_id).phase)
        if recovery == "restart":
            op.close()
            op = None
        fixture.write_text(source)
        if recovery == "restart":
            op = _operator(root, db)
        print("restored source:", _machine_record(op, run_id).phase)
        if recovery == "retry":
            op._machine_host.retry(run_id)
        receipt = op._machine_host.send(run_id, "approved", {}, "after-fix")
        _wait_until(lambda: _machine_record(op, run_id).visit == 2, seconds=10)
        record = _machine_record(op, run_id)
        same_key = op.start_run("approval", input=values)
        print(
            "outcomes:",
            {
                "recovery": recovery,
                "send": receipt.status,
                "after_send_phase": record.phase,
                "visit": record.visit,
                "same_key": same_key == run_id,
            },
        )
        assert receipt.status == "accepted" and same_key == run_id
        assert record.reason is None and record.phase != "needs_attention"
        assert _wait_terminal(op, run_id).status is RunStatus.SUCCESS
        assert op.fatal_error is None
    finally:
        if op is not None:
            op.close()


def _machine_facts(connection, run_id):
    from runtime.operator.state.machines import FactRecord

    return [
        FactRecord.model_validate({**json.loads(row["record_json"]), "tseq": row["tseq"]})
        for row in connection.execute(
            "SELECT tseq, record_json FROM machine_facts WHERE run_id=?", (run_id,)
        )
    ]


def _machine_record(op, run_id):
    from runtime.operator.state.machines import MachinesTable

    with op._state_store.transaction() as c:
        return MachinesTable.load(c, run_id)


def test_machine_visit_import_failure_is_interrupted_and_retryable(tmp_path):
    fixture = tmp_path / "machine.py"
    source = (Path(__file__).parents[1] / "fixtures" / "local_machine.py").read_text()
    fixture.write_text(source.replace("seconds=2", "seconds=4"))
    op = _operator(fixture, tmp_path / "state.db")
    try:
        run_id = op.start_run(
            "approval", input={"ticket": 18, "journal": str(tmp_path / "visits")}
        )
        _wait_until(lambda: op.get_run(run_id).status is RunStatus.WAITING)
        op._machine_host.send(run_id, "approved", {}, "advance")
        _wait_until(
            lambda: (
                _machine_record(op, run_id).visit == 2
                and _machine_record(op, run_id).phase == "waiting"
            )
        )
        fixture.write_text("def invalid syntax:\n")
        _wait_until(lambda: _machine_record(op, run_id).phase in {"interrupted", "closed"})
        assert _machine_record(op, run_id).phase == "interrupted"
        assert op.get_run(run_id).status is RunStatus.WAITING
        interrupted_visit = _machine_record(op, run_id).visit

        def retry_refused(reason):
            import http.client

            from runtime.operator.web import start_browser_server

            # Behaviour fix: an unperformed retry must fail on every public path,
            # not acknowledge success while leaving the interrupted visit alone.
            with pytest.raises(ValueError, match=reason):
                op._machine_host.retry(run_id)
            port = _free_port()
            address = f"127.0.0.1:{port}"
            server = serve_operator(op, port=port, block=False)
            browser = start_browser_server(address, port=0)
            try:
                with closing(grpc.insecure_channel(address)) as channel:
                    stub = pb_grpc.OperatorServiceV2Stub(channel)
                    with pytest.raises(grpc.RpcError) as error:
                        stub.ControlMachine(
                            pb.ControlMachineRequestV2(
                                request_json=json.dumps({"op": "retry", "run_id": run_id})
                            ),
                            timeout=10,
                        )
                    assert error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
                    assert error.value.details() == reason
                http = http.client.HTTPConnection(browser.host, browser.port, timeout=10)
                try:
                    http.request(
                        "POST",
                        f"/api/v1/runs/{run_id}/machine/visits:retry",
                        body="{}",
                        headers={"Content-Type": "application/json"},
                    )
                    response = http.getresponse()
                    body = response.read().decode()
                    assert response.status == 409 and reason in body
                finally:
                    http.close()
                cli = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "ava_cli",
                        "run",
                        "retry",
                        run_id,
                        "--visit",
                        "--connect",
                        address,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert cli.returncode != 0 and reason in cli.stdout + cli.stderr
            finally:
                browser.close()
                server.stop(grace=0).wait()
            assert _machine_record(op, run_id).phase == "interrupted"
            assert _machine_record(op, run_id).visit == interrupted_visit

        retry_refused("definition_unavailable")
        with op._state_store.transaction() as c:
            from runtime.operator.state.machines import MachinesTable

            assert (
                MachinesTable.visit(c, run_id, interrupted_visit).disposition == "indeterminate"
            )
        state = _machine_record(op, run_id).state
        fixture.write_text(source.replace(state, "retired_" + state))
        op._refresh_workflows()
        receipt = op._machine_host.send(run_id, "approved", {}, "missing-state")
        with op._state_store.transaction() as c:
            facts = [
                json.loads(row[0])
                for row in c.execute(
                    "SELECT record_json FROM machine_facts WHERE run_id=?", (run_id,)
                )
            ]
        assert any(
            fact["receipt_id"] == receipt.receipt_id and fact["reason"] == "state_missing"
            for fact in facts
        )
        retry_refused("state_missing")
        fixture.write_text(source)
        op._machine_host.retry(run_id)
        assert _wait_terminal(op, run_id).status is RunStatus.SUCCESS
        assert _machine_record(op, run_id).visit == 4
        assert op.fatal_error is None
    finally:
        op.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_run_left_running_by_a_killed_operator_is_stopped_and_failed(flows, tmp_path):
    state_db = tmp_path / "state.db"
    killed = subprocess.Popen(
        [sys.executable, "-c", _KILLED_OPERATOR, str(flows), str(state_db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        report = json.loads(killed.stdout.readline())
        coordinator = report["coordinator"]
        assert _process_alive(coordinator)
        killed.send_signal(signal.SIGKILL)
        killed.wait(timeout=10)
        # The coordinator runs in its own session, so it outlives the operator.
        assert _process_alive(coordinator)

        operator = _operator(flows, state_db)
        try:
            assert not _process_alive(coordinator)
            run = operator.get_run(report["run_id"])
            assert run.status is RunStatus.FAILED
            assert run.terminal_seal.reason == OPERATOR_RESTARTED_REASON
            assert {node.status for node in run.nodes.values()} == {NodeStatus.FAILED}
            assert {node.error for node in run.nodes.values()} == {OPERATOR_RESTARTED_REASON}
            summary = operator.list_run_summaries().runs[0]
            assert summary.status is RunStatus.FAILED
            assert summary.revision > report["sequence"]
            assert operator.current_sequence > report["sequence"]
        finally:
            operator.close()

        # The failure is stored, so the next restart neither stops nor fails it again.
        operator = _operator(flows, state_db)
        try:
            assert operator.get_run(report["run_id"]).terminal_seal == run.terminal_seal
        finally:
            operator.close()
    finally:
        if killed.poll() is None:
            killed.kill()
        killed.stdout.close()
        killed.stdin.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_stopping_a_recorded_process_group_leaves_unrelated_processes_alone():
    sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        recorded = identify_run_process(sleeper.pid)
        assert recorded is not None
        reused = recorded.model_copy(update={"started": "Thu Jan  1 00:00:00 1970"})
        rebooted = recorded.model_copy(update={"booted": "Thu Jan  1 00:00:00 1970"})

        # Another process with the recorded ID, or any process after a restart of
        # the machine, is not the run's.
        assert stop_run_process(reused, grace=1.0) is False
        assert stop_run_process(rebooted, grace=1.0) is False
        assert sleeper.poll() is None

        assert stop_run_process(recorded, grace=1.0) is True
        assert sleeper.wait(timeout=5) == -signal.SIGTERM
        assert stop_run_process(recorded, grace=1.0) is False
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


# ── Clients after a restart ──────────────────────────────


def test_v2_cursors_after_a_restart_start_at_the_stored_sequence(flows, tmp_path):
    state_db = tmp_path / "state.db"
    store = OperatorStateStore.open(state_db)
    try:
        with store.transaction() as connection:
            OperatorMetadataTable.write_sequences(
                connection, update_sequence=50_000_000, catalog_sequence=50_000_000
            )
    finally:
        store.close()
    operator = _operator(flows, state_db)
    port = _free_port()
    server = serve_operator(operator, port=port, block=False)
    try:
        with closing(grpc.insecure_channel(f"127.0.0.1:{port}")) as channel:
            stub = pb_grpc.OperatorServiceV2Stub(channel)
            # Naming fifty million earlier sequences would exceed this deadline.
            page = stub.ListRunSummaries(pb.ListRunSummariesRequestV2(), timeout=10)
            catalog = stub.DiscoverFlows(pb.DiscoverFlowsRequestV2(), timeout=10)
        assert page.cursor.event_ulid
        assert catalog.cursor.event_ulid
        assert operator.current_sequence >= 50_000_000
    finally:
        server.stop(grace=0).wait()
        operator.close()


def test_a_connected_client_reads_stored_runs_from_the_restarted_operator(flows, tmp_path):
    state_db = tmp_path / "state.db"
    port = _free_port()
    first = _operator(flows, state_db)
    server = serve_operator(first, port=port, block=False)
    provider = GrpcStateProvider(f"127.0.0.1:{port}", unary_timeout=5)
    try:
        run_id = provider.start_run("quick", input={"name": "ada"})
        _wait_terminal(first, run_id)
        before = provider.get_run(run_id)
        old_instance = before.operator_instance_id
        server.stop(grace=0).wait()
        first.close()

        second = _operator(flows, state_db)
        server = serve_operator(second, port=port, block=False)
        try:
            _wait_until(lambda: provider.ping())
            after = provider.get_run(run_id)
            assert after.operator_instance_id == second.operator_instance_id != old_instance
            assert after.status is RunStatus.SUCCESS
            assert _node_view(after) == _node_view(before)
            assert [run.run_id for run in provider.list_runs("quick")] == [run_id]
            # The result stored before the restart is served for the stored run.
            assert provider.get_run_result(run_id) == "HELLO ADA FROM NONE"
        finally:
            server.stop(grace=0).wait()
            second.close()
    finally:
        provider.close()


# ── Input files ──────────────────────────────────────────


def test_files_a_run_used_stay_next_to_the_database_and_unused_ones_go(flows, tmp_path):
    state_db = tmp_path / "state" / "state.db"
    port = _free_port()
    operator = _operator(flows, state_db)
    server = serve_operator(operator, port=port, block=False)
    try:
        with closing(grpc.insecure_channel(f"127.0.0.1:{port}")) as channel:
            stub = pb_grpc.OperatorServiceV2Stub(channel)

            def stage(name: str, content: bytes) -> pb.FileAttachmentV2:
                return stub.StageInputFile(
                    pb.StageInputFileRequestV2(
                        workflow_selector="documents",
                        field_name="document",
                        name=name,
                        media_type="text/plain",
                        content=content,
                    )
                )

            used = stage("used.txt", b"kept")
            unused = stage("unused.txt", b"dropped")
            stub.StartRun(
                pb.StartRunRequestV2(
                    run_id="run_files", workflow_selector="documents", input_files=[used]
                )
            )
        _wait_terminal(operator, "run_files")
    finally:
        server.stop(grace=0).wait()
        operator.close()

    folder = state_db.parent / "input-files"
    assert (folder / used.object_key).read_bytes() == b"kept"
    assert not (folder / unused.object_key).parent.exists()
    operator = _operator(flows, state_db)
    try:
        assert operator.get_run("run_files").status is RunStatus.SUCCESS
        with operator._state_store.transaction() as connection:
            request = RunsTable.read_request(connection, "run_files")
        assert request.input_files == (
            StagedInputFile(
                object_key=used.object_key,
                name="used.txt",
                media_type="text/plain",
                sha256=used.sha256,
                size_bytes=4,
            ),
        )
        assert request.input is not None and "document" in request.input
    finally:
        operator.close()


def test_closing_a_lasting_staging_folder_keeps_only_used_files(tmp_path):
    staging = InputFileStaging(tmp_path, keep_used_files=True)
    used = staging.stage("used.txt", "", b"u")
    unused = staging.stage("unused.txt", "", b"x")
    with staging.claims() as claims:
        claims.file(used.object_key, sha256=used.sha256, size_bytes=1)
        assert claims.files == (used,)

    staging.close()

    assert (tmp_path / used.object_key).read_bytes() == b"u"
    assert not (tmp_path / unused.object_key).parent.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_group_that_cannot_be_stopped_fails_startup_with_its_id(flows, tmp_path, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        recorded = identify_run_process(sleeper.pid)
        store = OperatorStateStore.open(tmp_path / "state.db")
        try:
            stored = _stored_run()
            with store.transaction() as connection:
                RunsTable.create(connection, stored, RunRequest(), recorded)
                OperatorMetadataTable.write_sequences(
                    connection, update_sequence=11, catalog_sequence=11
                )
        finally:
            store.close()

        def refuse(process_group, signal_number):
            raise PermissionError("operation not permitted")

        monkeypatch.setattr(run_processes.os, "killpg", refuse)
        with pytest.raises(RunProcessStopError, match=str(sleeper.pid)):
            _operator(flows, tmp_path / "state.db")
        assert sleeper.poll() is None
        # Nothing was marked failed: the run is still unfinished for the next start.
        store = OperatorStateStore.open(tmp_path / "state.db")
        try:
            with store.transaction() as connection:
                (loaded,) = RunsTable.load_all(connection)
        finally:
            store.close()
        assert loaded.stored.run.status is RunStatus.RUNNING
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)


def test_a_run_whose_first_write_fails_can_be_started_again_with_its_id(
    flows, tmp_path, monkeypatch
):
    operator = _operator(flows, tmp_path / "state.db")
    try:
        create = RunsTable.create
        calls = []

        def fail_once(*args, **kwargs):
            calls.append(None)
            if len(calls) == 1:
                raise StateStoreError("disk full")
            return create(*args, **kwargs)

        monkeypatch.setattr(RunsTable, "create", staticmethod(fail_once))
        sequence = operator.current_sequence
        with pytest.raises(StateStoreError):
            operator.start_run("quick", run_id="retried")
        assert operator.get_run("retried") is None
        assert operator.current_sequence == sequence

        run_id = operator.start_run("quick", run_id="retried")
        assert _wait_terminal(operator, run_id).status is RunStatus.SUCCESS
    finally:
        operator.close()


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_run_processes_are_identified_from_proc_without_ps(monkeypatch):
    monkeypatch.setenv("PATH", "")
    sleeper = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        recorded = identify_run_process(sleeper.pid)
        assert recorded is not None
        assert recorded.process_group == sleeper.pid
        assert stop_run_process(recorded, grace=1.0) is True
        assert sleeper.wait(timeout=5) == -signal.SIGTERM
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_a_run_process_is_still_stopped_after_the_clock_steps(monkeypatch):
    sleeper = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        recorded = identify_run_process(sleeper.pid)
        assert recorded is not None
        # A wall-clock step (NTP, a VM time sync) moves the kernel's reported boot
        # time without a reboot.
        read_proc = run_processes._read_proc

        def stepped(path):
            text = read_proc(path)
            if path == run_processes._PROC / "stat":
                text = re.sub(
                    r"^btime (\d+)$",
                    lambda match: f"btime {int(match[1]) + 3600}",
                    text,
                    flags=re.MULTILINE,
                )
            return text

        monkeypatch.setattr(run_processes, "_read_proc", stepped)
        assert stop_run_process(recorded, grace=1.0) is True
        assert sleeper.wait(timeout=5) == -signal.SIGTERM
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_a_run_process_recorded_in_another_boot_is_not_stopped(monkeypatch):
    sleeper = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    try:
        recorded = identify_run_process(sleeper.pid)
        assert recorded is not None
        # After a reboot the kernel reports a new boot ID, and a process with the
        # recorded ID and start time would be an unrelated one.
        read_proc = run_processes._read_proc
        boot_id = run_processes._PROC / "sys" / "kernel" / "random" / "boot_id"

        def rebooted(path):
            if path == boot_id:
                return "00000000-0000-4000-8000-000000000000\n"
            return read_proc(path)

        monkeypatch.setattr(run_processes, "_read_proc", rebooted)
        assert stop_run_process(recorded, grace=1.0) is False
        assert sleeper.poll() is None
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_missing_ps_command_is_a_lookup_error(monkeypatch):
    monkeypatch.setattr(run_processes.sys, "platform", "darwin")
    monkeypatch.setattr(run_processes.shutil, "which", lambda name: None)
    with pytest.raises(RunProcessLookupError, match="ps"):
        identify_run_process(os.getpid())
