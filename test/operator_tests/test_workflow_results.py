"""Result data integrity, publication authority, and real gRPC materialization."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import queue
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest

from avalanche.runtime import File
from runtime.operator import Operator
from runtime.operator import operator as operator_module
from runtime.operator import result_store as result_store_module
from runtime.operator.client import GrpcStateProvider
from runtime.operator.models import RunState, RunStatus
from runtime.operator.operator import RunResultUnavailableError
from runtime.operator.result_store import (
    MAX_RESULT_MANIFEST_BYTES,
    ResultPublicationCancelledError,
    ResultStore,
    StoredWorkflowResult,
    detach_transferred_bundle_descriptor,
    duplicate_bundle_descriptor_for_spawn,
    publish_workflow_result,
)
from runtime.operator.results import (
    EncodedWorkflowResult,
    ResultFileAttachment,
    decode_workflow_result,
    encode_workflow_result,
)
from runtime.operator.server import serve


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


@pytest.fixture
def result_client(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.txt").write_bytes(b"workspace-content")
    workflow = tmp_path / "results.py"
    workflow.write_text(
        "import time\nfrom pathlib import Path\n"
        "import avalanche as ava\nfrom pydantic import BaseModel\n"
        "class Report(BaseModel):\n    title: str\n    files: list[ava.File]\n"
        "@ava.source\ndef bundle():\n"
        "    return {\n"
        "        'report': Report(title='weekly', files=[\n"
        "            ava.File(name='', content=b'empty-name', content_type=''),\n"
        "            ava.File(content=b'unnamed'),\n"
        "            ava.File(name='large.bin', content=b'x' * (4 * 1024 * 1024 + 1)),\n"
        "        ]),\n"
        "        'pair': (\n"
        "            ava.File(name='note.txt', content=b'note', content_type='text/plain'),\n"
        "            'done',\n"
        "        ),\n"
        "        'json': {'count': 3, 'ok': True, 'items': [None, 'value', 2.5]},\n"
        "        'workspace': ava.Workspace.from_path(Path(__file__).parent / 'workspace'),\n"
        "    }\n"
        "@ava.workflow\ndef result_bundle():\n    return bundle()\n"
        "@ava.source\ndef fail():\n    raise ValueError('failed result')\n"
        "@ava.workflow\ndef failed_result():\n    return fail()\n"
        "@ava.source\ndef slow():\n    time.sleep(10)\n    return 'finished'\n"
        "@ava.workflow\ndef slow_result():\n    return slow()\n"
    )
    operator = Operator([str(workflow)], watch=False, schedule=False, cancel_grace=0.1)
    port = _unused_port()
    server = serve(operator, port=port, block=False)
    client = GrpcStateProvider(f"localhost:{port}")
    try:
        yield operator, client
    finally:
        client.close()
        server.stop(grace=1)
        operator.close()


def test_spawned_results_materialize_nested_files_workspaces_and_large_payloads(result_client):
    operator, client = result_client
    run_id = client.start_run("result_bundle")
    assert _wait_for_terminal(client, run_id, timeout=20).status == RunStatus.SUCCESS
    for result in (operator.get_run_result(run_id), client.get_run_result(run_id)):
        assert result["json"] == {"count": 3, "ok": True, "items": [None, "value", 2.5]}
        assert result["report"]["title"] == "weekly"
        empty, unnamed, large = result["report"]["files"]
        assert (empty.name, empty.content_type, empty.content) == ("", "", b"empty-name")
        assert (unnamed.name, unnamed.content_type, unnamed.content) == (None, None, b"unnamed")
        assert large.content == b"x" * (4 * 1024 * 1024 + 1)
        assert large.sha256 == hashlib.sha256(large.content).hexdigest()
        assert isinstance(result["pair"], tuple)
        note, label = result["pair"]
        assert (note.name, note.content_type, note.content, label) == (
            "note.txt",
            "text/plain",
            b"note",
            "done",
        )
        assert {
            entry.path: entry.content
            for entry in result["workspace"].entries
            if entry.kind == "file"
        } == {"report.txt": b"workspace-content"}


def test_failed_and_cancelled_runs_never_expose_results_or_leave_pending_files(result_client):
    operator, client = result_client
    before = set(operator._result_store.root.iterdir())
    cancelled = client.start_run("slow_result")
    with pytest.raises(grpc.RpcError) as unavailable:
        client.get_run_result(cancelled)
    assert unavailable.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    client.cancel_run(cancelled)
    assert _wait_for_terminal(client, cancelled).status == RunStatus.CANCELLED
    failed = client.start_run("failed_result")
    assert _wait_for_terminal(client, failed).status == RunStatus.FAILED
    for run_id in (cancelled, failed):
        with pytest.raises(grpc.RpcError) as unavailable:
            client.get_run_result(run_id)
        assert unavailable.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    deadline = time.monotonic() + 5
    while set(operator._result_store.root.iterdir()) != before:
        assert time.monotonic() < deadline, "Pending result files were not removed"
        time.sleep(0.03)


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX escaped descriptor attack")
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
        _, recovered_root = detail
        assert Path(recovered_root) == store.root

        stored = store.accept(pending, digest)
        mutate.set()
        status, discovered_names = result_queue.get(timeout=10)
        process.join(timeout=10)

        assert (status, process.exitcode) == ("mutated", 0), discovered_names
        assert not Path(pending.path).exists()
        assert discovered_names == [result_store_module._OWNER_MARKER]
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


@pytest.mark.parametrize("replace", ["root", "bundle"])
def test_spawned_publication_cannot_be_relocated_by_path_replacement(tmp_path, replace):
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
    try:
        assert ready.wait(timeout=10)
        original = store.root if replace == "root" else Path(pending.path)
        moved = tmp_path / "moved"
        original.rename(moved)
        original.mkdir(mode=0o700)
        try:
            start.set()
            status, detail = result_queue.get(timeout=10)
            process.join(timeout=10)
            assert (status, process.exitcode) == ("ok", 0), detail
            published_bundle = moved / pending.name if replace == "root" else moved
            assert (published_bundle / "attachment_00000000.bin").read_bytes() == b"inode-bound"
            assert list(original.iterdir()) == []
        finally:
            original.rmdir()
            moved.rename(original)
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=5)
        result_queue.close()
        store.discard(pending)
        store.close()


def test_result_publication_cancellation_never_publishes_partial_manifest(tmp_path):
    store = ResultStore(tmp_path)
    pending = store.prepare()
    encoded = encode_workflow_result(File(content=b"x" * (5 * 1024 * 1024)))
    try:
        with pytest.raises(ResultPublicationCancelledError):
            publish_workflow_result(
                encoded,
                pending.descriptor,
                (pending.device, pending.inode),
                _CancelDuringWrite(allowed_checks=3),
            )
        assert not (Path(pending.path) / "manifest.json").exists()
    finally:
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


@pytest.mark.parametrize(
    "outcome", ["mutation", "cancel", "unreaped", "late-event", "failed-exit"]
)
def test_provisional_success_is_not_authority_to_publish(monkeypatch, outcome):
    operator = Operator([], watch=False, schedule=False)
    run_id = "run-untrusted-success"
    pending, handle, event = _provisional_success_run(operator, run_id)

    first_quiescence = True

    def quiesce(*_args, **_kwargs):
        nonlocal first_quiescence
        if not first_quiescence:
            return True
        first_quiescence = False
        if outcome == "mutation":
            (Path(pending.path) / "attachment_00000000.bin").write_bytes(b"corrupted")
        elif outcome == "cancel":
            handle.cancel_event.set()
        elif outcome == "unreaped":
            return False
        elif outcome == "late-event":
            handle.event_queue.put({"type": "terminal", "status": "cancelled"})
        elif outcome == "failed-exit":
            return operator_module._ProcessGroupTeardown(True, natural_exitcode=1)
        return True

    monkeypatch.setattr(operator_module, "_teardown_process_group", quiesce)
    try:
        operator._drain_run_events(run_id, handle, [event])
        expected = RunStatus.CANCELLED if outcome == "cancel" else RunStatus.FAILED
        assert operator.get_run(run_id).status == expected
        with pytest.raises(RunResultUnavailableError):
            operator.get_run_result(run_id)
        assert not Path(pending.path).exists()
    finally:
        operator.close()


def test_forged_or_expired_opaque_result_handles_are_rejected(result_client):
    operator, client = result_client
    run_id = client.start_run("result_bundle")
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


def test_result_documents_reject_ambiguous_json_keys():
    with pytest.raises(ValueError):
        decode_workflow_result(
            EncodedWorkflowResult(
                value_json='{"version":1,"value":{"kind":"scalar","value":"safe","value":"replaced"}}'
            )
        )
