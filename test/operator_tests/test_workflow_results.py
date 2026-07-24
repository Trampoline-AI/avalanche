"""Behavioral coverage for native operator workflow results."""

from __future__ import annotations

import hashlib
import inspect
import json
import multiprocessing
import os
import queue
import shutil
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    computed_field,
    field_serializer,
    model_serializer,
)

from avalanche.operator import Operator
from avalanche.operator.client import GrpcStateProvider
from avalanche.operator.models import RunState, RunStatus
from avalanche.operator.server import serve
from avalanche.runtime import File
from runtime.operator import operator as operator_module
from runtime.operator import result_store as result_store_module
from runtime.operator.operator import RunResultUnavailableError
from runtime.operator.result_store import (
    MAX_RESULT_MANIFEST_BYTES,
    MAX_RETAINED_RESULT_BYTES,
    MAX_RETAINED_RESULTS,
    ResultPublicationCancelledError,
    ResultStore,
    StoredWorkflowResult,
    detach_transferred_bundle_descriptor,
    duplicate_bundle_descriptor_for_spawn,
    publish_workflow_result,
)
from runtime.operator.results import (
    MAX_ATTACHMENT_MEDIA_TYPE_LENGTH,
    MAX_ATTACHMENT_NAME_LENGTH,
    MAX_RESULT_ATTACHMENT_BYTES,
    MAX_RESULT_ATTACHMENTS,
    MAX_RESULT_ATTACHMENTS_BYTES,
    MAX_RESULT_NESTING_DEPTH,
    MAX_RESULT_TOTAL_BYTES,
    MAX_RESULT_VALUE_JSON_BYTES,
    EncodedWorkflowResult,
    ResultFileAttachment,
    decode_workflow_result,
    encode_workflow_result,
)


def _spawn_publish_with_transferred_descriptor(
    transferred_descriptor,
    identity,
    ready,
    start,
    result_queue,
):
    descriptor = detach_transferred_bundle_descriptor(
        transferred_descriptor,
        identity,
    )
    try:
        ready.set()
        if not start.wait(timeout=10):
            raise TimeoutError("publication gate was not released")
        digest = publish_workflow_result(
            encode_workflow_result(File(content=b"inode-bound")),
            descriptor,
            identity,
            _NeverCancelled(),
        )
        result_queue.put(("ok", digest))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        os.close(descriptor)


def _discover_and_mutate_from_escaped_descriptor(
    bundle_descriptor,
    mutate,
    result_queue,
):
    value_descriptor = None
    try:
        os.setsid()
        if sys.platform == "darwin":
            import fcntl

            recovered_bundle = Path(
                fcntl.fcntl(bundle_descriptor, fcntl.F_GETPATH, b"\0" * 1024)
                .rstrip(b"\0")
                .decode()
            )
        elif Path(f"/proc/self/fd/{bundle_descriptor}").exists():
            recovered_bundle = Path(os.readlink(f"/proc/self/fd/{bundle_descriptor}"))
        else:
            result_queue.put(("unsupported", None))
            return
        recovered_root = recovered_bundle.parent
        value_descriptor = os.open(
            "value.json",
            os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=bundle_descriptor,
        )
        value_metadata = os.fstat(value_descriptor)
        result_queue.put(
            (
                "ready",
                (
                    (value_metadata.st_dev, value_metadata.st_ino),
                    str(recovered_root),
                ),
            )
        )
        if not mutate.wait(timeout=10):
            raise TimeoutError("escaped mutation gate was not released")
        try:
            os.fchmod(bundle_descriptor, 0o700)
        except FileNotFoundError:
            pass
        try:
            os.fchmod(value_descriptor, 0o600)
        except FileNotFoundError:
            pass
        os.ftruncate(value_descriptor, 0)
        os.write(value_descriptor, b"mutated-after-success")
        os.fsync(value_descriptor)
        result_queue.put(
            (
                "mutated",
                sorted(path.name for path in recovered_root.iterdir()),
            )
        )
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        if value_descriptor is not None:
            os.close(value_descriptor)
        os.close(bundle_descriptor)


class _PydanticResultWithFile(BaseModel):
    created_at: datetime
    file: File


class _FieldSerializedFile(BaseModel):
    file: File

    @field_serializer("file")
    def serialize_file(self, value: File):
        return {"digest": value.sha256, "serialized": True}


class _ModelSerializedMetadata(BaseModel):
    file: File

    @model_serializer
    def serialize_model(self):
        return {"file_metadata": {"digest": self.file.sha256}}


class _ModelSerializedFile(BaseModel):
    file: File

    @model_serializer
    def serialize_model(self):
        return {"wrapped": self.file}


class _AliasedFile(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    file: File = Field(serialization_alias="document")


class _ComputedFile(BaseModel):
    source: File

    @computed_field
    @property
    def generated(self) -> File:
        return File(name="computed.bin", content=b"\xffcomputed")


class _RootFiles(RootModel[list[File]]):
    pass


class _NeverCancelled:
    def is_set(self) -> bool:
        return False


class _CancelDuringWrite:
    def __init__(self, allowed_checks: int) -> None:
        self.allowed_checks = allowed_checks
        self.checks = 0

    def is_set(self) -> bool:
        self.checks += 1
        return self.checks > self.allowed_checks


class _QuiescenceEventQueue(queue.Queue):
    def __init__(self):
        super().__init__()
        self.closed = False

    def close(self):
        self.closed = True


class _QuiescenceProcess:
    pid = 424242
    exitcode = 0


def _provisional_success_run(operator: Operator, run_id: str):
    pending = operator._result_store.prepare()
    digest = publish_workflow_result(
        encode_workflow_result(File(content=b"stable")),
        pending.descriptor,
        (pending.device, pending.inode),
        _NeverCancelled(),
    )
    event_queue = _QuiescenceEventQueue()
    handle = SimpleNamespace(
        process=_QuiescenceProcess(),
        event_queue=event_queue,
        cancel_event=threading.Event(),
        start_event=threading.Event(),
        assignment_event=threading.Event(),
        windows_job=None,
        result_bundle=pending,
        drain_thread=None,
        success_quiesced=False,
        publication_event=threading.Event(),
    )
    handle.publication_event.set()
    operator._runs[run_id] = RunState(
        run_id=run_id,
        flow_name="flow",
        status=RunStatus.RUNNING,
    )
    operator._active_runs[run_id] = handle
    event = {
        "type": "terminal",
        "status": "success",
        "result_manifest_sha256": digest,
    }
    return pending, handle, event


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        return sock.getsockname()[1]


def _wait_for_terminal(client: GrpcStateProvider, run_id: str, *, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = client.get_run(run_id)
        if run is not None and run.status in {
            RunStatus.SUCCESS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return run
        time.sleep(0.05)
    pytest.fail(f"Run {run_id} did not become terminal")


@pytest.fixture(scope="module")
def result_client(tmp_path_factory):
    workflow_path = tmp_path_factory.mktemp("workflow-results") / "result_flows.py"
    workflow_path.write_text(
        """
import time

import avalanche as ava
from pydantic import BaseModel


class FileReport(BaseModel):
    title: str
    files: list[ava.File]


@ava.source
def json_value():
    return {"count": 3, "ok": True, "items": [None, "value", 2.5]}


@ava.workflow
def json_result():
    return json_value()


@ava.source
def direct_file_value():
    return ava.File(
        name="report.txt",
        content=b"direct-file-bytes",
        content_type="text/plain",
    )


@ava.workflow
def direct_file_result():
    return direct_file_value()


@ava.source
def empty_metadata_file_value():
    return ava.File(name="", content=b"empty-metadata", content_type="")


@ava.workflow
def empty_metadata_file_result():
    return empty_metadata_file_value()


@ava.source
def nested_file_value():
    return {
        "report": FileReport(
            title="weekly",
            files=[
                ava.File(name="first.txt", content=b"first", content_type="text/plain"),
                ava.File(name="second.bin", content=b"second"),
            ],
        ),
        "pair": (ava.File(name="third.txt", content=b"third"), "done"),
    }


@ava.workflow
def nested_file_result():
    return nested_file_value()


@ava.source
def fail():
    raise ValueError("result failed")


@ava.workflow
def failed_result():
    return fail()


@ava.source
def slow():
    time.sleep(1.0)
    return "finished"


@ava.workflow
def nonterminal_result():
    return slow()


@ava.source
def large_file_value():
    return ava.File(
        name="large.bin",
        content=b"x" * (4 * 1024 * 1024 + 1),
        content_type="application/octet-stream",
    )


@ava.workflow
def large_file_result():
    return large_file_value()
"""
    )
    operator = Operator(
        workflow_paths=[str(workflow_path)],
        schedule=False,
        watch=False,
    )
    port = _unused_port()
    server = serve(operator, port=port, block=False)
    client = GrpcStateProvider(f"localhost:{port}")
    try:
        yield operator, client
    finally:
        client.close()
        server.stop(grace=1)
        operator.close()


def test_result_codec_preserves_existing_scalar_and_json_container_values():
    value = {
        "none": None,
        "bool": True,
        "integer": 7,
        "float": 2.5,
        "string": "value",
        "list": [1, 2],
        "tuple": ("left", "right"),
    }

    assert decode_workflow_result(encode_workflow_result(value)) == value


@pytest.mark.parametrize(
    ("name", "content_type"),
    [(None, None), ("", ""), ("name.txt", "text/plain")],
)
def test_result_codec_preserves_file_metadata_presence(name, content_type):
    result = decode_workflow_result(
        encode_workflow_result(File(name=name, content=b"presence", content_type=content_type))
    )

    assert result.name == name
    assert result.content_type == content_type


def test_result_codec_rejects_corrupt_file_content():
    content = b"expected"
    encoded = encode_workflow_result(File(name="data.bin", content=content))
    attachment = encoded.files[0]
    corrupted = EncodedWorkflowResult(
        value_json=encoded.value_json,
        files=(
            ResultFileAttachment(
                attachment_id=attachment.attachment_id,
                name=attachment.name,
                content=b"corrupted",
                media_type=attachment.media_type,
                sha256=attachment.sha256,
            ),
        ),
    )

    with pytest.raises(ValueError, match="sha256 does not match"):
        decode_workflow_result(corrupted)


def test_result_codec_uses_pydantic_json_values_while_extracting_files():
    result = _PydanticResultWithFile(
        created_at=datetime(2026, 7, 22, 12, 30, tzinfo=timezone.utc),
        file=File(name="data.bin", content=b"\xff\x00"),
    )

    decoded = decode_workflow_result(encode_workflow_result(result))

    assert decoded["created_at"] == "2026-07-22T12:30:00Z"
    assert isinstance(decoded["file"], File)
    assert decoded["file"].content == b"\xff\x00"


def test_result_codec_honors_field_serializer_that_replaces_file_with_metadata():
    value = _FieldSerializedFile(file=File(content=b"\xff"))

    encoded = encode_workflow_result(value)

    assert encoded.files == ()
    assert decode_workflow_result(encoded) == {
        "file": {
            "digest": hashlib.sha256(b"\xff").hexdigest(),
            "serialized": True,
        }
    }


def test_result_codec_honors_model_serializers_with_metadata_or_native_files():
    metadata = encode_workflow_result(_ModelSerializedMetadata(file=File(content=b"\xff")))
    wrapped = encode_workflow_result(_ModelSerializedFile(file=File(content=b"\xff")))

    assert metadata.files == ()
    assert decode_workflow_result(metadata) == {
        "file_metadata": {"digest": hashlib.sha256(b"\xff").hexdigest()}
    }
    assert len(wrapped.files) == 1
    assert decode_workflow_result(wrapped)["wrapped"].content == b"\xff"


def test_result_codec_preserves_alias_root_and_computed_field_json_semantics():
    aliased = decode_workflow_result(
        encode_workflow_result(_AliasedFile(file=File(content=b"alias")))
    )
    rooted = decode_workflow_result(
        encode_workflow_result(_RootFiles([File(content=b"\xffroot")]))
    )
    computed = decode_workflow_result(
        encode_workflow_result(_ComputedFile(source=File(content=b"source")))
    )

    assert set(aliased) == {"document"}
    assert aliased["document"].content == b"alias"
    assert isinstance(rooted, list)
    assert rooted[0].content == b"\xffroot"
    assert computed["source"].content == b"source"
    assert computed["generated"].content == b"\xffcomputed"


@pytest.mark.parametrize(
    "value_json",
    [
        '{"version":1}',
        '{"version":true,"value":{"kind":"scalar","value":1}}',
        '{"version":1,"value":{"kind":"scalar","value":NaN}}',
        '{"version":1,"value":{"kind":"scalar","value":Infinity}}',
        '{"version":1,"value":{"kind":"scalar","value":1e400}}',
        '{"version":1,"value":{"kind":"unknown"}}',
        '{"version":1,"value":{"kind":"scalar"}}',
        '{"version":1,"value":{"kind":"scalar","value":1,"extra":2}}',
        '{"version":1,"value":{"kind":"list","items":{}}}',
        '{"version":1,"value":{"kind":"tuple","items":{}}}',
        '{"version":1,"value":{"kind":"tuple","items":[],"extra":true}}',
    ],
)
def test_result_codec_strictly_rejects_malformed_documents(value_json):
    with pytest.raises(ValueError):
        decode_workflow_result(EncodedWorkflowResult(value_json=value_json))


@pytest.mark.parametrize(
    "value_json",
    [
        '{"version":1,"version":1,"value":{"kind":"scalar","value":1}}',
        '{"version":1,"value":{"kind":"scalar","value":1,"value":2}}',
        (
            '{"version":1,"value":{"kind":"dict","items":'
            '[["nested",{"kind":"scalar","value":"a","value":"b"}]]}}'
        ),
    ],
)
def test_result_codec_rejects_duplicate_json_keys_at_every_depth(value_json):
    with pytest.raises(ValueError, match="Duplicate JSON object key"):
        decode_workflow_result(EncodedWorkflowResult(value_json=value_json))


@pytest.mark.parametrize(
    "attachment_id",
    ["file_00", "file_01", f"file_{MAX_RESULT_ATTACHMENTS}", "file_" + "9" * 1000],
)
def test_result_codec_rejects_noncanonical_or_unbounded_attachment_ids(
    attachment_id,
):
    content = b"value"
    with pytest.raises(ValueError, match="attachment ID is malformed"):
        decode_workflow_result(
            EncodedWorkflowResult(
                value_json=(
                    '{"version":1,"value":{"attachment_id":'
                    f'"{attachment_id}","kind":"file"}}'
                ),
                files=(
                    ResultFileAttachment(
                        attachment_id=attachment_id,
                        content=content,
                        name=None,
                        media_type=None,
                        sha256=hashlib.sha256(content).hexdigest(),
                    ),
                ),
            )
        )


def test_manifest_decoder_rejects_duplicate_json_keys_at_every_depth():
    with pytest.raises(ValueError, match="Duplicate JSON object key"):
        result_store_module._decode_manifest(
            b'{"version":1,"value":{"sha256":"'
            + b"0" * 64
            + b'","size":0,"size":1},"files":[]}'
        )


def test_result_encoder_enforces_attachment_and_metadata_limits():
    at_limit = File(
        name="n" * MAX_ATTACHMENT_NAME_LENGTH,
        content=b"",
        content_type="m" * MAX_ATTACHMENT_MEDIA_TYPE_LENGTH,
    )
    assert encode_workflow_result(at_limit).files[0].name == at_limit.name

    with pytest.raises(ValueError, match="name exceeds"):
        encode_workflow_result(File(name="n" * (MAX_ATTACHMENT_NAME_LENGTH + 1), content=b""))
    with pytest.raises(ValueError, match="media type exceeds"):
        encode_workflow_result(
            File(
                content=b"",
                content_type="m" * (MAX_ATTACHMENT_MEDIA_TYPE_LENGTH + 1),
            )
        )
    assert (
        len(
            encode_workflow_result(
                [File(content=b"") for _ in range(MAX_RESULT_ATTACHMENTS)]
            ).files
        )
        == MAX_RESULT_ATTACHMENTS
    )
    with pytest.raises(ValueError, match="file attachments"):
        encode_workflow_result([File(content=b"") for _ in range(MAX_RESULT_ATTACHMENTS + 1)])


def test_result_value_json_size_limit_accepts_edge_and_rejects_one_more_byte():
    prefix = '{"version":1,"value":{"kind":"scalar","value":"'
    suffix = '"}}'
    at_limit = prefix + "x" * (MAX_RESULT_VALUE_JSON_BYTES - len(prefix) - len(suffix)) + suffix

    assert decode_workflow_result(EncodedWorkflowResult(value_json=at_limit)).startswith("x")
    with pytest.raises(ValueError, match="JSON exceeds"):
        decode_workflow_result(EncodedWorkflowResult(value_json=at_limit + " "))


def test_result_attachment_size_limit_accepts_edge_and_rejects_max_plus_one():
    at_limit = File(content=b"x" * MAX_RESULT_ATTACHMENT_BYTES)
    assert len(encode_workflow_result(at_limit).files[0].content) == (
        MAX_RESULT_ATTACHMENT_BYTES
    )

    with pytest.raises(ValueError, match="attachment exceeds"):
        encode_workflow_result(File(content=b"x" * (MAX_RESULT_ATTACHMENT_BYTES + 1)))


def test_result_total_limit_accepts_edge_and_rejects_max_plus_one():
    document = json.dumps(
        {
            "version": 1,
            "value": {
                "kind": "list",
                "items": [
                    {"kind": "file", "attachment_id": f"file_{index}"} for index in range(4)
                ],
            },
        },
        separators=(",", ":"),
    )
    value_json = document + " " * (MAX_RESULT_VALUE_JSON_BYTES - len(document))
    attachment_sizes = [
        MAX_RESULT_ATTACHMENT_BYTES,
        MAX_RESULT_ATTACHMENT_BYTES,
        MAX_RESULT_ATTACHMENT_BYTES,
        MAX_RESULT_TOTAL_BYTES - MAX_RESULT_VALUE_JSON_BYTES - 3 * MAX_RESULT_ATTACHMENT_BYTES,
    ]
    files = tuple(
        ResultFileAttachment(
            attachment_id=f"file_{index}",
            content=b"x" * size,
            name=None,
            media_type=None,
            sha256="0" * 64,
        )
        for index, size in enumerate(attachment_sizes)
    )
    encoded = EncodedWorkflowResult(value_json=value_json, files=files)
    result_store_module._validate_publication_metadata(
        encoded,
        value_json.encode(),
    )

    over_limit = EncodedWorkflowResult(
        value_json=value_json,
        files=(
            *files[:-1],
            ResultFileAttachment(
                **{
                    **files[-1].__dict__,
                    "content": files[-1].content + b"x",
                }
            ),
        ),
    )
    with pytest.raises(ValueError, match="Workflow result exceeds"):
        result_store_module._validate_publication_metadata(
            over_limit,
            value_json.encode(),
        )


def test_manifest_limits_apply_before_unbounded_metadata_parsing():
    valid = json.dumps(
        {
            "version": 1,
            "value": {"sha256": "0" * 64, "size": 0},
            "files": [],
        },
        separators=(",", ":"),
    ).encode()
    at_limit = valid + b" " * (MAX_RESULT_MANIFEST_BYTES - len(valid))
    assert result_store_module._decode_manifest(at_limit).value_size == 0

    with pytest.raises(ValueError, match="manifest exceeds"):
        result_store_module._decode_manifest(b" " * (MAX_RESULT_MANIFEST_BYTES + 1))

    value = {"sha256": "0" * 64, "size": MAX_RESULT_VALUE_JSON_BYTES + 1}
    with pytest.raises(ValueError, match="value JSON exceeds"):
        result_store_module._decode_manifest(
            json.dumps({"version": 1, "value": value, "files": []}).encode()
        )

    attachment = {
        "attachment_id": "file_0",
        "storage_name": "attachment_00000000.bin",
        "name": None,
        "media_type": None,
        "sha256": "0" * 64,
        "size": 0,
    }
    with pytest.raises(ValueError, match="file attachments"):
        result_store_module._decode_manifest(
            json.dumps(
                {
                    "version": 1,
                    "value": {"sha256": "0" * 64, "size": 0},
                    "files": [attachment] * (MAX_RESULT_ATTACHMENTS + 1),
                }
            ).encode()
        )


def test_manifest_attachment_size_and_totals_are_rejected_before_file_reads():
    attachment = {
        "attachment_id": "file_0",
        "storage_name": "attachment_00000000.bin",
        "name": None,
        "media_type": None,
        "sha256": "0" * 64,
        "size": MAX_RESULT_ATTACHMENT_BYTES,
    }
    at_limit = result_store_module._decode_manifest(
        json.dumps(
            {
                "version": 1,
                "value": {"sha256": "0" * 64, "size": 0},
                "files": [
                    {
                        **attachment,
                        "attachment_id": f"file_{index}",
                        "storage_name": f"attachment_{index:08d}.bin",
                    }
                    for index in range(
                        MAX_RESULT_ATTACHMENTS_BYTES // MAX_RESULT_ATTACHMENT_BYTES
                    )
                ],
            }
        ).encode()
    )
    assert sum(item.size for item in at_limit.files) == MAX_RESULT_ATTACHMENTS_BYTES

    with pytest.raises(ValueError, match="attachment exceeds"):
        result_store_module._decode_manifest(
            json.dumps(
                {
                    "version": 1,
                    "value": {"sha256": "0" * 64, "size": 0},
                    "files": [{**attachment, "size": MAX_RESULT_ATTACHMENT_BYTES + 1}],
                }
            ).encode()
        )

    with pytest.raises(ValueError, match="attachments exceed"):
        result_store_module._decode_manifest(
            json.dumps(
                {
                    "version": 1,
                    "value": {"sha256": "0" * 64, "size": 0},
                    "files": [
                        {
                            **attachment,
                            "attachment_id": f"file_{index}",
                            "storage_name": f"attachment_{index:08d}.bin",
                        }
                        for index in range(
                            MAX_RESULT_ATTACHMENTS_BYTES // MAX_RESULT_ATTACHMENT_BYTES
                        )
                    ]
                    + [
                        {
                            **attachment,
                            "attachment_id": "file_4",
                            "storage_name": "attachment_00000004.bin",
                            "size": 1,
                        }
                    ],
                }
            ).encode()
        )


def test_result_store_caps_manifest_read_at_max_plus_one(tmp_path):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    manifest_path = Path(pending.path) / "manifest.json"
    manifest_bytes = b" " * (MAX_RESULT_MANIFEST_BYTES + 1)
    manifest_path.write_bytes(manifest_bytes)
    os.chmod(manifest_path, 0o600)
    try:
        with pytest.raises(ValueError, match="manifest.json exceeds"):
            store.accept(pending, hashlib.sha256(manifest_bytes).hexdigest())
    finally:
        store.discard(pending)
        store.close()


def test_regular_file_hash_stops_at_declared_max_plus_one(tmp_path, monkeypatch):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    oversized = Path(pending.path) / "value.json"
    oversized.write_bytes(b"x" * MAX_RESULT_VALUE_JSON_BYTES)
    os.chmod(oversized, 0o600)
    opened = store._open_retained_bundle(
        pending.descriptor,
        (pending.device, pending.inode),
    )
    digest, size = result_store_module._hash_regular_file(
        opened,
        "value.json",
        maximum_bytes=MAX_RESULT_VALUE_JSON_BYTES,
    )
    assert size == MAX_RESULT_VALUE_JSON_BYTES
    assert digest == hashlib.sha256(b"x" * MAX_RESULT_VALUE_JSON_BYTES).hexdigest()
    with oversized.open("ab") as stream:
        stream.write(b"x" * 1024)
    original_read = result_store_module.os.read
    bytes_read = 0

    def counted_read(descriptor, size):
        nonlocal bytes_read
        chunk = original_read(descriptor, size)
        bytes_read += len(chunk)
        return chunk

    monkeypatch.setattr(result_store_module.os, "read", counted_read)
    try:
        with pytest.raises(ValueError, match="value.json exceeds"):
            result_store_module._hash_regular_file(
                opened,
                "value.json",
                maximum_bytes=MAX_RESULT_VALUE_JSON_BYTES,
            )
        assert bytes_read == MAX_RESULT_VALUE_JSON_BYTES + 1
    finally:
        opened.close()
        store.discard(pending)
        store.close()


def test_result_retrieval_accepts_attachment_max_into_immutable_memory(tmp_path):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    encoded = encode_workflow_result(File(content=b"x" * MAX_RESULT_ATTACHMENT_BYTES))
    digest = publish_workflow_result(
        encoded,
        pending.descriptor,
        (pending.device, pending.inode),
        _NeverCancelled(),
    )
    stored = store.accept(pending, digest)
    assert not Path(pending.path).exists()
    assert len(store.load(stored).files[0].content) == MAX_RESULT_ATTACHMENT_BYTES
    try:
        assert {entry.name for entry in store.root.iterdir()} == {
            result_store_module._OWNER_MARKER
        }
        assert not hasattr(stored, "descriptor")
        assert not hasattr(stored, "bundle_name")
    finally:
        store.discard(stored)
        store.close()


def test_accept_is_immutable_after_namespace_discovery_and_same_user_mutation(tmp_path):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    digest = publish_workflow_result(
        encode_workflow_result(File(content=b"stable")),
        pending.descriptor,
        (pending.device, pending.inode),
        _NeverCancelled(),
    )
    context = multiprocessing.get_context("fork")
    mutate = context.Event()
    result_queue = context.Queue()
    escaped_descriptor = os.dup(pending.descriptor)
    process = context.Process(
        target=_discover_and_mutate_from_escaped_descriptor,
        args=(escaped_descriptor, mutate, result_queue),
    )
    process.start()
    os.close(escaped_descriptor)
    stored = None
    try:
        status, detail = result_queue.get(timeout=10)
        if status == "unsupported":
            pytest.skip("No descriptor-to-path discovery mechanism is available")
        assert status == "ready", detail
        pending_value_identity, recovered_root = detail
        assert Path(recovered_root) == store.root

        stored = store.accept(pending, digest)
        mutate.set()
        status, discovered_names = result_queue.get(timeout=10)
        process.join(timeout=10)

        assert (status, process.exitcode) == ("mutated", 0), discovered_names
        assert not Path(pending.path).exists()
        assert pending_value_identity[0] >= 0
        assert discovered_names == [result_store_module._OWNER_MARKER]
        assert not hasattr(stored, "descriptor")
        assert not hasattr(stored, "bundle_name")
        assert store.load(stored).files[0].content == b"stable"
        assert store.load(stored).files[0].content == b"stable"
    finally:
        mutate.set()
        if process.is_alive():
            process.kill()
        process.join(timeout=5)
        result_queue.close()
        if stored is None:
            store.discard(pending)
        else:
            store.discard(stored)
        store.close()


def test_accept_materialization_failure_leaves_only_the_provisional_bundle(
    tmp_path, monkeypatch
):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    digest = publish_workflow_result(
        encode_workflow_result(File(content=b"stable")),
        pending.descriptor,
        (pending.device, pending.inode),
        _NeverCancelled(),
    )

    read_regular_file = result_store_module._read_regular_file
    reads = 0

    def fail_second_materialized_read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            raise OSError("accepted materialization failed")
        return read_regular_file(*args, **kwargs)

    monkeypatch.setattr(
        result_store_module,
        "_read_regular_file",
        fail_second_materialized_read,
    )
    try:
        with pytest.raises(OSError, match="accepted materialization failed"):
            store.accept(pending, digest)
        assert {entry.name for entry in store.root.iterdir()} == {
            result_store_module._OWNER_MARKER,
            pending.name,
        }
        assert Path(pending.path).is_dir()
    finally:
        store.discard(pending)
        store.close()


def test_result_store_enforces_aggregate_result_and_byte_caps(tmp_path, monkeypatch):
    store = ResultStore(tmp_path)
    encoded = encode_workflow_result(File(content=b"bounded"))
    stored = []
    monkeypatch.setattr(result_store_module, "MAX_RETAINED_RESULTS", 2)
    try:
        for _ in range(2):
            pending = store.prepare()
            digest = publish_workflow_result(
                encoded,
                pending.descriptor,
                (pending.device, pending.inode),
                _NeverCancelled(),
            )
            stored.append(store.accept(pending, digest))

        assert MAX_RETAINED_RESULTS >= 2
        assert MAX_RETAINED_RESULT_BYTES >= sum(item.byte_size for item in stored)
        assert store._retained_result_bytes == sum(item.byte_size for item in stored)

        pending = store.prepare()
        digest = publish_workflow_result(
            encoded,
            pending.descriptor,
            (pending.device, pending.inode),
            _NeverCancelled(),
        )
        with pytest.raises(RuntimeError, match="retains at most 2 results"):
            store.accept(pending, digest)
        store.discard(pending)

        store.discard(stored.pop())
        monkeypatch.setattr(
            result_store_module,
            "MAX_RETAINED_RESULT_BYTES",
            store._retained_result_bytes,
        )
        pending = store.prepare()
        digest = publish_workflow_result(
            encoded,
            pending.descriptor,
            (pending.device, pending.inode),
            _NeverCancelled(),
        )
        with pytest.raises(RuntimeError, match="retained bytes would exceed"):
            store.accept(pending, digest)
        store.discard(pending)
    finally:
        for item in stored:
            store.discard(item)
        store.close()


def test_result_codec_rejects_duplicate_unused_and_traversal_attachment_ids():
    attachment = ResultFileAttachment(
        attachment_id="file_0",
        content=b"value",
        name=None,
        media_type=None,
        sha256=hashlib.sha256(b"value").hexdigest(),
    )
    references_one = '{"version":1,"value":{"attachment_id":"file_0","kind":"file"}}'

    with pytest.raises(ValueError, match="Duplicate"):
        decode_workflow_result(
            EncodedWorkflowResult(
                value_json=references_one,
                files=(attachment, attachment),
            )
        )
    with pytest.raises(ValueError, match="Unreferenced"):
        decode_workflow_result(
            EncodedWorkflowResult(
                value_json='{"version":1,"value":{"kind":"scalar","value":1}}',
                files=(attachment,),
            )
        )
    with pytest.raises(ValueError, match="ID is malformed"):
        decode_workflow_result(
            EncodedWorkflowResult(
                value_json=(
                    '{"version":1,"value":' '{"attachment_id":"../file_0","kind":"file"}}'
                ),
                files=(
                    ResultFileAttachment(
                        attachment_id="../file_0",
                        content=b"value",
                        name=None,
                        media_type=None,
                        sha256=hashlib.sha256(b"value").hexdigest(),
                    ),
                ),
            )
        )


def test_result_codec_rejects_missing_attachment_reference():
    with pytest.raises(ValueError, match="missing file attachment"):
        decode_workflow_result(
            EncodedWorkflowResult(
                value_json=('{"version":1,"value":' '{"attachment_id":"file_0","kind":"file"}}')
            )
        )


def test_result_codec_rejects_one_attachment_referenced_twice():
    content = b"one-blob"
    attachment = ResultFileAttachment(
        attachment_id="file_0",
        content=content,
        name=None,
        media_type=None,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    value_json = json.dumps(
        {
            "version": 1,
            "value": {
                "kind": "list",
                "items": [
                    {"kind": "file", "attachment_id": "file_0"},
                    {"kind": "file", "attachment_id": "file_0"},
                ],
            },
        },
        separators=(",", ":"),
    )

    with pytest.raises(ValueError, match="Duplicate.*attachment reference"):
        decode_workflow_result(
            EncodedWorkflowResult(value_json=value_json, files=(attachment,))
        )


def test_result_codec_rejects_over_depth_and_over_count_with_stable_errors():
    nested: dict = {"kind": "scalar", "value": None}
    for _ in range(MAX_RESULT_NESTING_DEPTH + 1):
        nested = {"kind": "list", "items": [nested]}
    with pytest.raises(ValueError, match="nesting exceeds"):
        decode_workflow_result(
            EncodedWorkflowResult(value_json=json.dumps({"version": 1, "value": nested}))
        )

    attachment_ids = {f"file_{index}" for index in range(MAX_RESULT_ATTACHMENTS)}
    references = [
        {"kind": "file", "attachment_id": f"file_{index}"}
        for index in range(MAX_RESULT_ATTACHMENTS + 1)
    ]
    with pytest.raises(ValueError, match="file attachment references"):
        result_store_module.validate_workflow_result_document(
            json.dumps(
                {
                    "version": 1,
                    "value": {"kind": "list", "items": references},
                }
            ),
            attachment_ids,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"content": bytearray(b"value")},
        {"name": 1},
        {"media_type": False},
        {"sha256": "A" * 64},
    ],
)
def test_result_codec_rejects_invalid_exact_attachment_types(changes):
    fields = {
        "attachment_id": "file_0",
        "content": b"value",
        "name": None,
        "media_type": None,
        "sha256": hashlib.sha256(b"value").hexdigest(),
    }
    fields.update(changes)

    with pytest.raises(ValueError):
        decode_workflow_result(
            EncodedWorkflowResult(
                value_json=(
                    '{"version":1,"value":' '{"attachment_id":"file_0","kind":"file"}}'
                ),
                files=(ResultFileAttachment(**fields),),
            )
        )


def test_result_store_uses_private_modes_and_rejects_symlink_substitution(tmp_path):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    encoded = encode_workflow_result(File(name="safe.bin", content=b"safe"))
    digest = publish_workflow_result(
        encoded,
        pending.descriptor,
        (pending.device, pending.inode),
        _NeverCancelled(),
    )
    attachment_path = Path(pending.path) / "attachment_00000000.bin"
    external = tmp_path / "external.bin"
    external.write_bytes(b"safe")

    assert os.stat(store.root).st_mode & 0o777 == 0o700
    assert os.stat(pending.path).st_mode & 0o777 == 0o700
    assert os.stat(attachment_path).st_mode & 0o777 == 0o600
    attachment_path.unlink()
    attachment_path.symlink_to(external)
    try:
        with pytest.raises(OSError):
            store.accept(pending, digest)
    finally:
        store.discard(pending)
        store.close()


def test_result_store_fails_closed_without_anchored_io_before_writing(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "unsupported-result-storage"
    monkeypatch.setattr(result_store_module.os, "supports_dir_fd", set())

    with pytest.raises(RuntimeError, match="directory-anchored file operations"):
        ResultStore(base)

    assert not base.exists()


def test_worker_publication_holds_bundle_authority_across_directory_replacement(
    tmp_path,
    monkeypatch,
):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    moved = tmp_path / "moved-bundle"
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    write_private_file = result_store_module._write_private_file
    replaced = False

    def replace_bundle(directory_descriptor, final_name, content, cancel_signal):
        nonlocal replaced
        if not replaced:
            replaced = True
            Path(pending.path).rename(moved)
            Path(pending.path).symlink_to(external, target_is_directory=True)
        return write_private_file(
            directory_descriptor,
            final_name,
            content,
            cancel_signal,
        )

    monkeypatch.setattr(result_store_module, "_write_private_file", replace_bundle)
    try:
        publish_workflow_result(
            encode_workflow_result(File(content=b"private-result")),
            pending.descriptor,
            (pending.device, pending.inode),
            _NeverCancelled(),
        )
        assert list(external.iterdir()) == []
        assert (moved / "attachment_00000000.bin").read_bytes() == b"private-result"
    finally:
        Path(pending.path).unlink()
        moved.rename(pending.path)
        store.discard(pending)
        store.close()


@pytest.mark.parametrize("replace", ["root", "bundle"])
def test_spawned_publication_cannot_be_relocated_by_path_replacement(
    tmp_path,
    replace,
):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    start = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_spawn_publish_with_transferred_descriptor,
        args=(
            duplicate_bundle_descriptor_for_spawn(pending),
            (pending.device, pending.inode),
            ready,
            start,
            result_queue,
        ),
    )
    process.start()
    assert ready.wait(timeout=10)

    root = store.root
    if replace == "root":
        moved = tmp_path / "moved-root"
        root.rename(moved)
        root.mkdir(mode=0o700)
        published_bundle = moved / pending.name
    else:
        moved = root / "moved-bundle"
        Path(pending.path).rename(moved)
        Path(pending.path).mkdir(mode=0o700)
        published_bundle = moved

    try:
        start.set()
        status, detail = result_queue.get(timeout=10)
        process.join(timeout=10)
        assert (status, process.exitcode) == ("ok", 0), detail
        assert (published_bundle / "attachment_00000000.bin").read_bytes() == (b"inode-bound")
        replacement = root if replace == "root" else Path(pending.path)
        assert list(replacement.iterdir()) == []
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        result_queue.close()
        if replace == "root":
            root.rmdir()
            moved.rename(root)
        else:
            Path(pending.path).rmdir()
            moved.rename(pending.path)
        store.discard(pending)
        store.close()


def test_worker_entrypoint_has_no_bundle_path_argument():
    from runtime.operator.run_worker import run_worker

    parameters = inspect.signature(run_worker).parameters
    assert "result_bundle_path" not in parameters
    assert "transferred_result_bundle_descriptor" in parameters
    start_run_source = inspect.getsource(Operator.start_run)
    assert "result_bundle.path" not in start_run_source
    assert "duplicate_bundle_descriptor_for_spawn" in start_run_source


def test_worker_publication_fails_closed_without_anchored_io(
    tmp_path,
    monkeypatch,
):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.bin"
    sentinel.write_bytes(b"external")
    monkeypatch.setattr(result_store_module.os, "supports_dir_fd", set())
    try:
        with pytest.raises(RuntimeError, match="directory-anchored file operations"):
            publish_workflow_result(
                encode_workflow_result(File(content=b"private-result")),
                pending.descriptor,
                (pending.device, pending.inode),
                _NeverCancelled(),
            )
        assert list(Path(pending.path).iterdir()) == []
        assert sentinel.read_bytes() == b"external"
    finally:
        store.discard(pending)
        store.close()


def test_result_publication_cancellation_never_publishes_partial_manifest(tmp_path):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    encoded = encode_workflow_result(File(content=b"x" * (5 * 1024 * 1024)))

    with pytest.raises(ResultPublicationCancelledError):
        publish_workflow_result(
            encoded,
            pending.descriptor,
            (pending.device, pending.inode),
            _CancelDuringWrite(allowed_checks=3),
        )

    assert not (Path(pending.path) / "manifest.json").exists()
    store.discard(pending)
    store.close()


def test_parent_cancellation_wins_after_success_bundle_validation(tmp_path, monkeypatch):
    operator = Operator([], watch=False, schedule=False)
    run_id = "run_cancel_during_result_accept"
    pending = operator._result_store.prepare()
    digest = publish_workflow_result(
        encode_workflow_result(File(content=b"published")),
        pending.descriptor,
        (pending.device, pending.inode),
        _NeverCancelled(),
    )
    cancel_event = threading.Event()
    handle = SimpleNamespace(
        result_bundle=pending,
        cancel_event=cancel_event,
        success_quiesced=True,
    )
    operator._runs[run_id] = RunState(
        run_id=run_id,
        flow_name="flow",
        status=RunStatus.RUNNING,
    )
    accept = operator._result_store.accept

    def accept_then_cancel(*args, **kwargs):
        stored = accept(*args, **kwargs)
        cancel_event.set()
        return stored

    monkeypatch.setattr(operator._result_store, "accept", accept_then_cancel)
    try:
        assert operator._apply_event(
            run_id,
            handle,
            {
                "type": "terminal",
                "status": "success",
                "result_manifest_sha256": digest,
            },
        )
        assert operator.get_run(run_id).status == RunStatus.CANCELLED
        assert run_id not in operator._stored_results
        assert not Path(pending.path).exists()
    finally:
        operator.close()


@pytest.mark.parametrize(
    "mutated_file",
    ["value.json", "attachment_00000000.bin"],
    ids=["background-thread", "child-process"],
)
def test_provisional_success_quiesces_before_detecting_late_mutation(
    monkeypatch,
    mutated_file,
):
    operator = Operator([], watch=False, schedule=False)
    run_id = f"run_late_{mutated_file}"
    pending, handle, event = _provisional_success_run(operator, run_id)
    mutated = False

    def quiesce(*_args, **_kwargs):
        nonlocal mutated
        if not mutated:
            mutated = True
            with (Path(pending.path) / mutated_file).open("ab") as stream:
                stream.write(b"corrupt-after-event")
        return True

    monkeypatch.setattr(operator_module, "_teardown_process_group", quiesce)
    try:
        operator._drain_run_events(run_id, handle, [event])
        assert operator.get_run(run_id).status == RunStatus.FAILED
        assert run_id not in operator._stored_results
        assert not Path(pending.path).exists()
    finally:
        operator.close()


def test_provisional_success_waits_for_delayed_exit_before_notification(monkeypatch):
    operator = Operator([], watch=False, schedule=False)
    run_id = "run_delayed_exit"
    _, handle, event = _provisional_success_run(operator, run_id)
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def delayed_quiesce(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(timeout=5)
        return True

    monkeypatch.setattr(
        operator_module,
        "_teardown_process_group",
        delayed_quiesce,
    )
    drain = threading.Thread(
        target=operator._drain_run_events,
        args=(run_id, handle, [event]),
    )
    drain.start()
    try:
        assert entered.wait(timeout=2)
        assert operator.get_run(run_id).status == RunStatus.RUNNING
        assert run_id not in operator._stored_results
        release.set()
        drain.join(timeout=5)
        assert not drain.is_alive()
        assert operator.get_run(run_id).status == RunStatus.SUCCESS
        assert operator.get_run_result(run_id).content == b"stable"
    finally:
        release.set()
        drain.join(timeout=5)
        operator.close()


def test_cancellation_during_success_quiescence_wins(monkeypatch):
    operator = Operator([], watch=False, schedule=False)
    run_id = "run_cancel_during_quiescence"
    pending, handle, event = _provisional_success_run(operator, run_id)

    def cancel_while_quiescing(*_args, **_kwargs):
        handle.cancel_event.set()
        return True

    monkeypatch.setattr(
        operator_module,
        "_teardown_process_group",
        cancel_while_quiescing,
    )
    try:
        operator._drain_run_events(run_id, handle, [event])
        assert operator.get_run(run_id).status == RunStatus.CANCELLED
        assert run_id not in operator._stored_results
        assert not Path(pending.path).exists()
    finally:
        operator.close()


@pytest.mark.parametrize("failure_mode", ["reported", "raised"])
def test_success_fails_closed_when_process_group_cannot_be_quiesced(
    monkeypatch,
    failure_mode,
):
    operator = Operator([], watch=False, schedule=False)
    run_id = "run_quiescence_failed"
    pending, handle, event = _provisional_success_run(operator, run_id)
    calls = 0

    def fail_quiescence(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if failure_mode == "raised" and calls == 1:
            raise OSError("teardown failed")
        return False

    monkeypatch.setattr(operator_module, "_teardown_process_group", fail_quiescence)
    try:
        operator._drain_run_events(run_id, handle, [event])
        assert operator.get_run(run_id).status == RunStatus.FAILED
        assert run_id not in operator._stored_results
        assert not Path(pending.path).exists()
    finally:
        operator.close()


def test_success_rechecks_protocol_queue_after_quiescence(monkeypatch):
    operator = Operator([], watch=False, schedule=False)
    run_id = "run_late_protocol_event"
    pending, handle, event = _provisional_success_run(operator, run_id)
    handle.event_queue.put({"type": "terminal", "status": "cancelled"})
    monkeypatch.setattr(
        operator_module,
        "_teardown_process_group",
        lambda *_args, **_kwargs: True,
    )
    try:
        operator._drain_run_events(run_id, handle, [event])
        assert operator.get_run(run_id).status == RunStatus.FAILED
        assert run_id not in operator._stored_results
        assert not Path(pending.path).exists()
    finally:
        operator.close()


def test_success_rechecks_unsuccessful_coordinator_exit_after_quiescence(monkeypatch):
    operator = Operator([], watch=False, schedule=False)
    run_id = "run_failed_exit_after_success"
    pending, handle, event = _provisional_success_run(operator, run_id)
    monkeypatch.setattr(
        operator_module,
        "_teardown_process_group",
        lambda *_args, **_kwargs: operator_module._ProcessGroupTeardown(
            True,
            natural_exitcode=1,
        ),
    )
    try:
        operator._drain_run_events(run_id, handle, [event])
        assert operator.get_run(run_id).status == RunStatus.FAILED
        assert run_id not in operator._stored_results
        assert not Path(pending.path).exists()
    finally:
        operator.close()


def test_valid_success_is_accepted_only_after_quiescence(monkeypatch):
    operator = Operator([], watch=False, schedule=False)
    run_id = "run_quiesced_success"
    _, handle, event = _provisional_success_run(operator, run_id)
    quiesced = False

    def quiesce(*_args, **_kwargs):
        nonlocal quiesced
        quiesced = True
        return True

    monkeypatch.setattr(operator_module, "_teardown_process_group", quiesce)
    try:
        operator._drain_run_events(run_id, handle, [event])
        assert quiesced
        assert operator.get_run(run_id).status == RunStatus.SUCCESS
        assert operator.get_run_result(run_id).content == b"stable"
    finally:
        operator.close()


def test_operator_and_grpc_roundtrip_existing_json_result(result_client):
    operator, client = result_client
    run_id = client.start_run("json_result")

    assert _wait_for_terminal(client, run_id).status == RunStatus.SUCCESS
    expected = {"count": 3, "ok": True, "items": [None, "value", 2.5]}
    assert operator.get_run_result(run_id) == expected
    assert client.get_run_result(run_id) == expected


def test_grpc_roundtrips_direct_file_with_metadata_and_hash(result_client):
    _, client = result_client
    run_id = client.start_run("direct_file_result")

    assert _wait_for_terminal(client, run_id).status == RunStatus.SUCCESS
    result = client.get_run_result(run_id)

    assert isinstance(result, File)
    assert result.name == "report.txt"
    assert result.content_type == "text/plain"
    assert result.content == b"direct-file-bytes"
    assert result.sha256 == hashlib.sha256(result.content).hexdigest()


def test_grpc_roundtrips_empty_file_metadata_distinct_from_absence(result_client):
    _, client = result_client
    run_id = client.start_run("empty_metadata_file_result")

    assert _wait_for_terminal(client, run_id).status == RunStatus.SUCCESS
    result = client.get_run_result(run_id)

    assert isinstance(result, File)
    assert result.name == ""
    assert result.content_type == ""
    assert result.content == b"empty-metadata"


def test_grpc_roundtrips_files_nested_in_pydantic_and_containers(result_client):
    _, client = result_client
    run_id = client.start_run("nested_file_result")

    assert _wait_for_terminal(client, run_id).status == RunStatus.SUCCESS
    result = client.get_run_result(run_id)

    assert result["report"]["title"] == "weekly"
    first, second = result["report"]["files"]
    assert isinstance(first, File)
    assert (first.name, first.content_type, first.content) == (
        "first.txt",
        "text/plain",
        b"first",
    )
    assert isinstance(second, File)
    assert (second.name, second.content) == ("second.bin", b"second")
    assert isinstance(result["pair"], tuple)
    assert isinstance(result["pair"][0], File)
    assert result["pair"][0].content == b"third"
    assert result["pair"][1] == "done"


def test_grpc_rejects_result_retrieval_for_nonterminal_and_failed_runs(
    result_client,
):
    _, client = result_client
    nonterminal_id = client.start_run("nonterminal_result")

    with pytest.raises(grpc.RpcError) as nonterminal_error:
        client.get_run_result(nonterminal_id)
    assert nonterminal_error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "not terminal" in nonterminal_error.value.details()

    failed_id = client.start_run("failed_result")
    assert _wait_for_terminal(client, failed_id).status == RunStatus.FAILED
    with pytest.raises(grpc.RpcError) as failed_error:
        client.get_run_result(failed_id)
    assert failed_error.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    assert "status failed" in failed_error.value.details()


def test_grpc_roundtrips_result_file_above_default_message_limit(result_client):
    _, client = result_client
    run_id = client.start_run("large_file_result")

    assert _wait_for_terminal(client, run_id, timeout=20).status == RunStatus.SUCCESS
    result = client.get_run_result(run_id)

    assert isinstance(result, File)
    assert result.name == "large.bin"
    assert result.content_type == "application/octet-stream"
    assert len(result.content) == 4 * 1024 * 1024 + 1
    assert result.sha256 == hashlib.sha256(result.content).hexdigest()


def test_repeated_results_retain_only_bounded_opaque_memory_handles(
    result_client,
    monkeypatch,
):
    operator, client = result_client
    terminal_events = {}
    apply_event = operator._apply_event

    def capture_event(run_id, handle, event):
        if event.get("type") == "terminal" and event.get("status") == "success":
            terminal_events[run_id] = event.copy()
        return apply_event(run_id, handle, event)

    monkeypatch.setattr(operator, "_apply_event", capture_event)
    run_ids = [client.start_run("direct_file_result") for _ in range(2)]

    for run_id in run_ids:
        assert _wait_for_terminal(client, run_id).status == RunStatus.SUCCESS

    stored = [operator._stored_results[run_id] for run_id in run_ids]
    assert all(type(item) is StoredWorkflowResult for item in stored)
    assert stored[0].storage_key != stored[1].storage_key
    assert not hasattr(operator, "_run_results")
    assert all(not hasattr(item, "content") for item in stored)
    assert all(not hasattr(item, "descriptor") for item in stored)
    assert operator._result_store._retained_result_bytes == sum(
        item.handle.byte_size for item in operator._result_store._accepted_results.values()
    )
    assert operator._result_store._retained_result_bytes >= sum(
        item.byte_size for item in stored
    )
    assert set(terminal_events) >= set(run_ids)
    assert all(
        set(terminal_events[run_id])
        == {
            "type",
            "status",
            "result_manifest_sha256",
        }
        and len(terminal_events[run_id]["result_manifest_sha256"]) == 64
        for run_id in run_ids
    )


def test_failed_and_cancelled_runs_remove_their_pending_result_bundles(
    result_client,
):
    operator, client = result_client
    before = {path.name for path in operator._result_store.root.iterdir()}

    failed_id = client.start_run("failed_result")
    assert _wait_for_terminal(client, failed_id).status == RunStatus.FAILED
    cancelled_id = client.start_run("nonterminal_result")
    client.cancel_run(cancelled_id)
    assert _wait_for_terminal(client, cancelled_id).status == RunStatus.CANCELLED

    assert {path.name for path in operator._result_store.root.iterdir()} == before
    assert failed_id not in operator._stored_results
    assert cancelled_id not in operator._stored_results


def test_result_retention_and_close_remove_private_storage(tmp_path):
    workflow = tmp_path / "retained.py"
    workflow.write_text(
        """
import avalanche as ava


@ava.workflow
def retained():
    return ava.File(name="retained.bin", content=b"retained")
"""
    )
    operator = Operator(
        [str(workflow)],
        watch=False,
        schedule=False,
        result_retention_seconds=0.1,
    )
    root = operator._result_store.root
    try:
        run_id = operator.start_run("retained")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            run = operator.get_run(run_id)
            if run is not None and run.status == RunStatus.SUCCESS:
                break
            time.sleep(0.02)
        else:
            pytest.fail("Run did not succeed")
        assert operator.get_run_result(run_id).content == b"retained"

        deadline = time.monotonic() + 3
        while run_id in operator._stored_results and time.monotonic() < deadline:
            time.sleep(0.02)
        assert run_id not in operator._stored_results
        with pytest.raises(RunResultUnavailableError, match="unavailable or expired"):
            operator.get_run_result(run_id)
    finally:
        operator.close()

    assert not root.exists()


def test_configured_result_storage_reaps_only_conclusively_stale_roots(tmp_path):
    live = ResultStore(tmp_path)
    live_root = live.root
    observer = ResultStore(tmp_path)
    try:
        assert live_root.exists()
    finally:
        observer.close()

    stale_root = live.root
    os.close(live._owner_fd)
    os.close(live._root_fd)
    live._owner_fd = None
    live._root_fd = None
    live._closed = True

    replacement = ResultStore(tmp_path)
    try:
        assert not stale_root.exists()
        assert replacement.root.exists()
    finally:
        replacement.close()


def test_configured_result_storage_leaves_tampered_stale_root_in_place(tmp_path):
    stale = ResultStore(tmp_path)
    stale_root = stale.root
    os.close(stale._owner_fd)
    os.close(stale._root_fd)
    stale._owner_fd = None
    stale._root_fd = None
    stale._closed = True
    marker = stale_root / result_store_module._OWNER_MARKER
    marker.write_bytes(b"tampered\n")

    replacement = ResultStore(tmp_path)
    try:
        assert stale_root.exists()
        assert marker.read_bytes() == b"tampered\n"
    finally:
        replacement.close()
        shutil.rmtree(stale_root)


def test_configured_result_storage_never_follows_tampered_root_symlink(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.bin"
    sentinel.write_bytes(b"external")
    forged_root = tmp_path / "operator-results-forged"
    forged_root.symlink_to(external, target_is_directory=True)

    store = ResultStore(tmp_path)
    try:
        assert forged_root.is_symlink()
        assert sentinel.read_bytes() == b"external"
        assert list(external.iterdir()) == [sentinel]
    finally:
        store.close()
        forged_root.unlink()


def test_forged_or_expired_opaque_result_handles_are_rejected(result_client):
    operator, client = result_client
    run_id = client.start_run("direct_file_result")
    assert _wait_for_terminal(client, run_id).status == RunStatus.SUCCESS
    stored = operator._stored_results[run_id]
    forged = StoredWorkflowResult(
        storage_key=stored.storage_key,
        manifest_sha256="0" * 64,
        published_at=stored.published_at,
        byte_size=stored.byte_size,
    )

    with pytest.raises(ValueError, match="handle is unavailable"):
        operator._result_store.load(forged)

    operator._result_store.discard(stored)
    with pytest.raises(ValueError, match="handle is unavailable"):
        operator._result_store.load(stored)
