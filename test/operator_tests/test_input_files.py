"""Run input files staged through the operator and used by runs and schedules."""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import struct
import tempfile
import threading
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import grpc
import pytest
from pydantic import BaseModel

import avalanche as ava
from runtime.operator import input_files
from runtime.operator.input_files import (
    InputFileStaging,
    InputFileStorageError,
    StagedInputFileError,
)
from runtime.operator.models import RunStatus, TriggerKind, WorkflowInfo, input_file_fields
from runtime.operator.operator import Operator
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc
from runtime.operator.server import serve as serve_operator
from runtime.operator.web import start_browser_server

_FLOWS = """import avalanche as ava

class Documents(ava.BaseInput):
    document: ava.File
    attachments: list[ava.File] = []

@ava.source
def read_documents(payload: Documents):
    return [
        f"{file.name}={file.read_bytes().decode()}"
        for file in [payload.document, *payload.attachments]
    ]

@ava.workflow(input=Documents, cron="* * * * *")
def documents():
    return read_documents()
"""


def _flow(operator: Operator) -> WorkflowInfo:
    return next(item for item in operator.list_workflows() if item.name == "documents")


def _wait_terminal(operator: Operator, run_id: str):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run.status in {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED}:
            return run
        time.sleep(0.03)
    pytest.fail(f"Run {run_id} did not finish")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def operator(tmp_path):
    flows = tmp_path / "flows.py"
    flows.write_text(_FLOWS)
    instance = Operator([str(flows)], watch=False, schedule=False, webhook_port=0)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def stub(operator):
    port = _free_port()
    server = serve_operator(operator, port=port, block=False)
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield pb_grpc.OperatorServiceV2Stub(channel)
    finally:
        channel.close()
        server.stop(grace=0).wait()


def _stage(stub, selector: str, field: str, name: str, content: bytes) -> pb.FileAttachmentV2:
    return stub.StageInputFile(
        pb.StageInputFileRequestV2(
            workflow_selector=selector,
            field_name=field,
            name=name,
            media_type="text/plain",
            content=content,
        )
    )


# ── Staging folder ───────────────────────────────────────


def _claim(staging: InputFileStaging, object_key: str, *, sha256: str, size_bytes: int):
    with staging.claims() as claims:
        return claims.file(object_key, sha256=sha256, size_bytes=size_bytes)


def test_staged_files_become_path_backed_files_inside_the_staging_folder(tmp_path):
    staging = InputFileStaging(tmp_path)

    staged = staging.stage("notes.txt", "text/plain", b"hello")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        file = _claim(staging, staged.object_key, sha256=staged.sha256, size_bytes=5)

    assert (file.name, file.content_type, file.size) == ("notes.txt", "text/plain", 5)
    assert file.path.is_relative_to(tmp_path.resolve())
    assert file.read_bytes() == b"hello"


def test_unused_staged_files_expire_and_used_ones_stay(tmp_path):
    now = 1000.0
    staging = InputFileStaging(tmp_path, ttl_seconds=60, clock=lambda: now)
    unused = staging.stage("unused.txt", "", b"a")
    used = staging.stage("used.txt", "", b"b")
    used_path = _claim(staging, used.object_key, sha256=used.sha256, size_bytes=1).path

    now += 61
    staging.prune()

    with pytest.raises(StagedInputFileError):
        _claim(staging, unused.object_key, sha256=unused.sha256, size_bytes=1)
    assert not (tmp_path / unused.object_key).exists()
    assert used_path.read_bytes() == b"b"
    assert _claim(staging, used.object_key, sha256=used.sha256, size_bytes=1).path == used_path


def test_closing_removes_the_staging_folder(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    staging = InputFileStaging(root)
    staging.stage("notes.txt", "", b"x")

    staging.close()

    assert not root.exists()


@pytest.mark.parametrize(
    "object_key",
    [
        "0123456789abcdef0123456789abcdef/notes.txt",
        "../outside.txt",
        "0123456789abcdef0123456789abcdef/../outside.txt",
        "0123456789abcdef0123456789abcdef/sub/notes.txt",
        "/etc/passwd",
        "notes.txt",
        "0123456789ABCDEF0123456789ABCDEF/notes.txt",
    ],
)
def test_claim_rejects_keys_the_staging_folder_did_not_issue(tmp_path, object_key):
    root = tmp_path / "staging"
    root.mkdir()
    staging = InputFileStaging(root)
    (tmp_path / "outside.txt").write_text("secret")

    with pytest.raises(StagedInputFileError):
        _claim(staging, object_key, sha256="0" * 64, size_bytes=6)


def test_claim_rejects_a_digest_that_does_not_match_the_upload(tmp_path):
    staging = InputFileStaging(tmp_path)
    staged = staging.stage("notes.txt", "", b"hello")

    with pytest.raises(StagedInputFileError):
        _claim(staging, staged.object_key, sha256="0" * 64, size_bytes=5)


def test_files_claimed_by_a_refused_request_expire_like_unused_ones(tmp_path):
    now = 1000.0
    staging = InputFileStaging(tmp_path, ttl_seconds=60, clock=lambda: now)
    staged = staging.stage("notes.txt", "", b"x")

    with pytest.raises(RuntimeError), staging.claims() as claims:
        claims.file(staged.object_key, sha256=staged.sha256, size_bytes=1)
        raise RuntimeError("the run was refused")
    now += 61
    staging.prune()

    assert not (tmp_path / staged.object_key).exists()


def test_a_refused_request_keeps_files_another_request_still_uses(tmp_path):
    now = 1000.0
    staging = InputFileStaging(tmp_path, ttl_seconds=60, clock=lambda: now)
    staged = staging.stage("notes.txt", "", b"x")

    with pytest.raises(RuntimeError), staging.claims() as refused:
        refused.file(staged.object_key, sha256=staged.sha256, size_bytes=1)
        with staging.claims() as schedule:
            schedule.file(staged.object_key, sha256=staged.sha256, size_bytes=1)
        raise RuntimeError("the run was refused")
    now += 61
    staging.prune()

    assert (tmp_path / staged.object_key).read_bytes() == b"x"


def test_claims_accept_uppercase_digests(tmp_path):
    staging = InputFileStaging(tmp_path)
    staged = staging.stage("notes.txt", "", b"hello")
    digest = staged.sha256.upper()

    with staging.claims() as claims:
        by_key = claims.file(staged.object_key, sha256=digest, size_bytes=5)
        inline = claims.inline("inline.txt", "", b"hello", digest)

    assert by_key.read_bytes() == inline.read_bytes() == b"hello"


def test_a_staged_file_removed_from_disk_is_a_storage_error_that_keeps_no_use(tmp_path):
    now = 1000.0
    staging = InputFileStaging(tmp_path, ttl_seconds=60, clock=lambda: now)
    staged = staging.stage("notes.txt", "", b"x")
    (tmp_path / staged.object_key).unlink()

    with pytest.raises(InputFileStorageError), staging.claims() as claims:
        claims.file(staged.object_key, sha256=staged.sha256, size_bytes=1)
    now += 61
    staging.prune()

    assert not (tmp_path / staged.object_key).parent.exists()


def test_claiming_a_file_again_registers_no_new_workspace(tmp_path):
    staging = InputFileStaging(tmp_path)
    staged = staging.stage("notes.txt", "", b"x")
    registry = Path(tempfile.gettempdir()) / f"avalanche-local-refs-{os.getuid()}"
    before = set(registry.iterdir())

    files = [
        _claim(staging, staged.object_key, sha256=staged.sha256, size_bytes=1) for _ in range(3)
    ]

    assert set(registry.iterdir()) == before
    assert len({file.ref.workspace for file in files}) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_an_expired_file_that_cannot_be_removed_does_not_fail_an_upload(tmp_path, caplog):
    now = 1000.0
    staging = InputFileStaging(tmp_path, ttl_seconds=60, clock=lambda: now)
    expired = staging.stage("old.txt", "", b"x")
    folder = (tmp_path / expired.object_key).parent
    folder.chmod(0o500)
    now += 61
    try:
        with caplog.at_level(logging.WARNING, logger=input_files.__name__):
            fresh = staging.stage("new.txt", "", b"y")
    finally:
        folder.chmod(0o700)

    assert (tmp_path / fresh.object_key).read_bytes() == b"y"
    assert caplog.records


def test_close_waits_for_an_upload_that_is_still_being_written(tmp_path, monkeypatch):
    staging = InputFileStaging(tmp_path)
    writing, finish = threading.Event(), threading.Event()
    register = input_files.local_workspace

    def slow_register(path):
        writing.set()
        assert finish.wait(5)
        return register(path)

    monkeypatch.setattr(input_files, "local_workspace", slow_register)
    upload = threading.Thread(target=staging.stage, args=("notes.txt", "", b"x"))
    upload.start()
    assert writing.wait(5)
    closing = threading.Thread(target=staging.close)
    closing.start()
    closing.join(0.2)
    assert closing.is_alive()

    finish.set()
    upload.join(5)
    closing.join(5)

    assert not closing.is_alive()
    assert not staging.root.exists()


def test_upload_limits_reject_oversized_content_and_media_types(tmp_path, monkeypatch):
    monkeypatch.setattr(input_files, "MAX_STAGED_INPUT_FILE_BYTES", 4)
    staging = InputFileStaging(tmp_path)

    with pytest.raises(StagedInputFileError):
        staging.stage("big.bin", "", b"12345")
    with pytest.raises(StagedInputFileError):
        staging.stage("notes.txt", "t" * 256, b"x")

    assert list(tmp_path.iterdir()) == []
    assert staging.stage("fits.bin", "t" * 255, b"1234").size_bytes == 4


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_storage_failures_and_closed_staging_raise_storage_errors(tmp_path):
    root = tmp_path / "staging"
    root.mkdir()
    staging = InputFileStaging(root)
    root.chmod(0o500)
    try:
        with pytest.raises(InputFileStorageError):
            staging.stage("notes.txt", "", b"x")
    finally:
        root.chmod(0o700)
    assert list(root.iterdir()) == []

    staging.close()
    with pytest.raises(InputFileStorageError):
        staging.stage("notes.txt", "", b"x")


@pytest.mark.parametrize("name", ["", ".", "..", "a/b.txt", "a\\b.txt", "nul\x00.txt"])
def test_stage_rejects_names_that_are_not_plain_file_names(tmp_path, name):
    with pytest.raises(StagedInputFileError):
        InputFileStaging(tmp_path).stage(name, "", b"x")


# ── StageInputFile RPC ───────────────────────────────────


def test_stage_input_file_returns_a_local_attachment(operator, stub):
    selector = _flow(operator).selector

    attachment = _stage(stub, selector, "document", "notes.txt", b"hello")

    assert attachment.field_name == "document"
    assert (attachment.name, attachment.media_type, attachment.size_bytes) == (
        "notes.txt",
        "text/plain",
        5,
    )
    assert attachment.object_key.endswith("/notes.txt")
    assert not attachment.object_uri
    staged = operator.input_files.root / attachment.object_key
    assert staged.read_bytes() == b"hello"


@pytest.mark.parametrize(
    ("selector", "field", "name", "status"),
    [
        ("missing.py::missing", "document", "notes.txt", grpc.StatusCode.NOT_FOUND),
        (None, "undeclared", "notes.txt", grpc.StatusCode.INVALID_ARGUMENT),
        (None, "document", "../notes.txt", grpc.StatusCode.INVALID_ARGUMENT),
        (None, "", "notes.txt", grpc.StatusCode.INVALID_ARGUMENT),
    ],
)
def test_stage_input_file_rejects_invalid_requests(
    operator, stub, selector, field, name, status
):
    with pytest.raises(grpc.RpcError) as error:
        _stage(stub, selector or _flow(operator).selector, field, name, b"x")

    assert error.value.code() is status
    assert list(operator.input_files.root.iterdir()) == []


# ── Runs and schedules ───────────────────────────────────


def test_stage_input_file_rejects_oversized_uploads(operator, stub, monkeypatch):
    monkeypatch.setattr(input_files, "MAX_STAGED_INPUT_FILE_BYTES", 4)

    with pytest.raises(grpc.RpcError) as error:
        _stage(stub, _flow(operator).selector, "document", "big.bin", b"12345")

    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert list(operator.input_files.root.iterdir()) == []


def test_stage_input_file_reports_storage_failures(operator, stub):
    operator.input_files.close()

    with pytest.raises(grpc.RpcError) as error:
        _stage(stub, _flow(operator).selector, "document", "notes.txt", b"x")

    assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION


def test_a_refused_run_does_not_keep_its_staged_files(operator, stub):
    selector = _flow(operator).selector
    document = _stage(stub, selector, "document", "notes.txt", b"hello")
    now = time.time()
    operator.input_files._clock = lambda: now

    with pytest.raises(grpc.RpcError) as error:
        stub.StartRun(
            pb.StartRunRequestV2(
                run_id="run_refused",
                workflow_selector=selector,
                input_files=[document],
                expected_version="stale",
            )
        )
    now += input_files.STAGED_INPUT_FILE_TTL_SECONDS + 1
    operator.input_files.prune()

    assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert not (operator.input_files.root / document.object_key).exists()


def test_start_run_builds_files_from_staged_single_and_list_fields(operator, stub):
    selector = _flow(operator).selector
    document = _stage(stub, selector, "document", "main.txt", b"main")
    first = _stage(stub, selector, "attachments", "a.txt", b"first")
    second = _stage(stub, selector, "attachments", "b.txt", b"second")

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        response = stub.StartRun(
            pb.StartRunRequestV2(
                run_id="run_files",
                workflow_selector=selector,
                # The input schema says attachments holds a list; the JSON need not.
                input_files=[document, first, second],
            )
        )

    run = _wait_terminal(operator, response.run_id)
    assert run.status is RunStatus.SUCCESS
    assert operator.get_run_result(run.run_id) == [
        "main.txt=main",
        "a.txt=first",
        "b.txt=second",
    ]


def test_inline_attachments_are_staged_as_path_backed_files(operator, stub):
    selector = _flow(operator).selector

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        response = stub.StartRun(
            pb.StartRunRequestV2(
                run_id="run_inline",
                workflow_selector=selector,
                input_files=[
                    pb.FileAttachmentV2(
                        field_name="document", name="inline.txt", inline_bytes=b"body"
                    )
                ],
            )
        )

    run = _wait_terminal(operator, response.run_id)
    assert operator.get_run_result(run.run_id) == ["inline.txt=body"]


def _foreign_attachment(tmp_path: Path, object_key: str):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    return pb.FileAttachmentV2(
        field_name="document", name="outside.txt", object_key=object_key, size_bytes=6
    )


@pytest.mark.parametrize(
    "object_key",
    [
        "0123456789abcdef0123456789abcdef/outside.txt",
        "../../outside.txt",
        "{token}/../../outside.txt",
    ],
)
def test_start_run_rejects_files_the_operator_did_not_stage(
    operator, stub, tmp_path, object_key
):
    selector = _flow(operator).selector
    staged = _stage(stub, selector, "document", "notes.txt", b"hello")
    token = staged.object_key.partition("/")[0]
    attachment = _foreign_attachment(tmp_path, object_key.format(token=token))

    with pytest.raises(grpc.RpcError) as error:
        stub.StartRun(
            pb.StartRunRequestV2(
                run_id="run_foreign", workflow_selector=selector, input_files=[attachment]
            )
        )

    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert operator.list_runs(selector) == []


@pytest.mark.parametrize(
    "attachment",
    [
        pb.FileAttachmentV2(field_name="document", object_uri="s3://bucket/key"),
        pb.FileAttachmentV2(name="notes.txt", inline_bytes=b"x"),
    ],
)
def test_start_run_rejects_unsupported_attachments(operator, stub, attachment):
    selector = _flow(operator).selector

    with pytest.raises(grpc.RpcError) as error:
        stub.StartRun(
            pb.StartRunRequestV2(
                run_id="run_unsupported", workflow_selector=selector, input_files=[attachment]
            )
        )

    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    ("field", "input_json"),
    [("undeclared", "{}"), ("attachments", '{"attachments": "not a list"}')],
)
def test_start_run_places_files_only_where_the_input_schema_allows(
    operator, stub, field, input_json
):
    selector = _flow(operator).selector
    staged = _stage(stub, selector, "attachments", "a.txt", b"a")
    staged.field_name = field

    with pytest.raises(grpc.RpcError) as error:
        stub.StartRun(
            pb.StartRunRequestV2(
                run_id="run_misplaced",
                workflow_selector=selector,
                input_json=input_json,
                input_files=[staged],
            )
        )

    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert operator.list_runs(selector) == []


def test_list_fields_come_from_the_declared_input_schema():
    class Fields(BaseModel):
        single: ava.File
        optional: ava.File | None = None
        many: list[ava.File] = []
        maybe_many: list[ava.File] | None = None
        note: str = ""

    schema = json.dumps(Fields.model_json_schema())

    fields = input_file_fields(schema)

    assert fields.declared == {"single", "optional", "many", "maybe_many", "note"}
    assert fields.lists == {"many", "maybe_many"}


def test_list_fields_accept_every_json_schema_type_form():
    schema = {
        "type": "object",
        "properties": {
            "names": {"type": ["array", "null"], "items": {"type": "string"}},
            "label": {"type": ["string", "null"]},
            "anything": True,
            "either": {"anyOf": [False, {"type": "array"}]},
        },
    }

    assert input_file_fields(json.dumps(schema)).lists == {"names", "either"}


@pytest.mark.parametrize(
    ("selector", "code"),
    [("documents", grpc.StatusCode.INVALID_ARGUMENT), ("missing", grpc.StatusCode.NOT_FOUND)],
)
def test_start_run_with_files_reports_ambiguous_and_unknown_workflows(tmp_path, selector, code):
    roots = [tmp_path / "left", tmp_path / "right"]
    for root in roots:
        root.mkdir()
        (root / "flows.py").write_text(_FLOWS)
    operator = Operator([str(root) for root in roots], watch=False, schedule=False)
    port = _free_port()
    server = serve_operator(operator, port=port, block=False)
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        with pytest.raises(grpc.RpcError) as error:
            pb_grpc.OperatorServiceV2Stub(channel).StartRun(
                pb.StartRunRequestV2(
                    run_id="run_selector",
                    workflow_selector=selector,
                    input_files=[
                        pb.FileAttachmentV2(
                            field_name="document", name="notes.txt", inline_bytes=b"x"
                        )
                    ],
                )
            )
    finally:
        channel.close()
        server.stop(grace=0).wait()
        operator.close()

    assert error.value.code() is code


def test_a_single_file_field_takes_one_attachment(operator, stub):
    selector = _flow(operator).selector
    first = _stage(stub, selector, "document", "a.txt", b"a")
    second = _stage(stub, selector, "document", "b.txt", b"b")

    with pytest.raises(grpc.RpcError) as error:
        stub.StartRun(
            pb.StartRunRequestV2(
                run_id="run_duplicate", workflow_selector=selector, input_files=[first, second]
            )
        )

    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_scheduled_runs_read_the_staged_schedule_files(operator, stub):
    selector = _flow(operator).selector
    document = _stage(stub, selector, "document", "main.txt", b"main")
    extra = _stage(stub, selector, "attachments", "extra.txt", b"extra")
    now = datetime(2026, 10, 29, 9, 0, 40)
    operator._scheduler._clock = lambda: now

    flow = stub.SetScheduleEnabled(
        pb.SetScheduleEnabledRequestV2(
            workflow_selector=selector,
            enabled=True,
            input_json=json.dumps({"attachments": []}),
            input_files=[document, extra],
        )
    )

    schedule_input = json.loads(flow.schedule_input_json)
    assert schedule_input["document"]["name"] == "main.txt"
    assert [item["name"] for item in schedule_input["attachments"]] == ["extra.txt"]
    now += timedelta(minutes=1)
    operator._scheduler._check_schedules()
    (started,) = operator.list_runs(selector)
    run = _wait_terminal(operator, started.run_id)
    assert (run.status, run.triggered_by) == (RunStatus.SUCCESS, TriggerKind.SCHEDULED)
    assert operator.get_run_result(run.run_id) == ["main.txt=main", "extra.txt=extra"]


def test_schedule_files_require_input_json(operator, stub):
    selector = _flow(operator).selector
    document = _stage(stub, selector, "document", "main.txt", b"main")

    with pytest.raises(grpc.RpcError) as error:
        stub.SetScheduleEnabled(
            pb.SetScheduleEnabledRequestV2(
                workflow_selector=selector, enabled=True, input_files=[document]
            )
        )

    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert _flow(operator).schedule_enabled is False


@pytest.mark.parametrize(
    ("field", "input_json"),
    [("undeclared", "{}"), ("attachments", '{"attachments": "not a list"}')],
)
def test_schedule_files_go_only_where_the_input_schema_allows(
    operator, stub, field, input_json
):
    before = _flow(operator)
    selector = before.selector
    staged = _stage(stub, selector, "attachments", "a.txt", b"a")
    staged.field_name = field

    with pytest.raises(grpc.RpcError) as error:
        stub.SetScheduleEnabled(
            pb.SetScheduleEnabledRequestV2(
                workflow_selector=selector,
                enabled=True,
                input_json=input_json,
                input_files=[staged],
            )
        )

    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    after = _flow(operator)
    assert (after.schedule_enabled, after.schedule_input) == (
        False,
        before.schedule_input,
    )


@pytest.mark.parametrize("call", ["StartRun", "SetScheduleEnabled"])
def test_a_staged_file_removed_from_disk_refuses_runs_and_schedules(operator, stub, call):
    selector = _flow(operator).selector
    document = _stage(stub, selector, "document", "main.txt", b"main")
    (operator.input_files.root / document.object_key).unlink()
    requests = {
        "StartRun": pb.StartRunRequestV2(
            run_id="run_removed", workflow_selector=selector, input_files=[document]
        ),
        "SetScheduleEnabled": pb.SetScheduleEnabledRequestV2(
            workflow_selector=selector, enabled=True, input_json="{}", input_files=[document]
        ),
    }

    with pytest.raises(grpc.RpcError) as error:
        getattr(stub, call)(requests[call])

    assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert operator.list_runs(selector) == []
    assert _flow(operator).schedule_enabled is False


def test_closing_the_operator_removes_staged_files(tmp_path):
    flows = tmp_path / "flows.py"
    flows.write_text(_FLOWS)
    operator = Operator([str(flows)], watch=False, schedule=False, webhook_port=0)
    staged = operator.stage_input_file(
        _flow(operator).selector, "document", "notes.txt", "", b"x"
    )
    root = operator.input_files.root
    assert (root / staged.object_key).exists()

    operator.close()

    assert not root.exists()


def _grpc_web_post(server, method: str, request) -> bytes:
    payload = request.SerializeToString()
    connection = http.client.HTTPConnection(server.host, server.port, timeout=30)
    try:
        connection.request(
            "POST",
            f"/avalanche.operator.OperatorServiceV2/{method}",
            body=struct.pack(">BI", 0, len(payload)) + payload,
            headers={"Content-Type": "application/grpc-web+proto"},
        )
        return connection.getresponse().read()
    finally:
        connection.close()


def test_browser_bridge_accepts_input_files_above_the_usual_request_limit(operator, tmp_path):
    port = _free_port()
    grpc_server = serve_operator(operator, port=port, block=False)
    browser = start_browser_server(f"127.0.0.1:{port}", port=0, asset_root=tmp_path)
    content = b"x" * (5 * 1024 * 1024)
    request = pb.StageInputFileRequestV2(
        workflow_selector=_flow(operator).selector,
        field_name="document",
        name="large.bin",
        content=content,
    )
    try:
        staged = _grpc_web_post(browser, "StageInputFile", request)
    finally:
        browser.close()
        grpc_server.stop(grace=0).wait()

    assert b"grpc-status: 0" in staged
    (folder,) = operator.input_files.root.iterdir()
    assert (folder / "large.bin").stat().st_size == len(content)
