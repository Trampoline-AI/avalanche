"""Result data integrity, publication authority, and real gRPC materialization."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import queue
import shutil
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import grpc
import pytest

from avalanche.runtime import File
from runtime.operator import Operator
from runtime.operator import operator as operator_module
from runtime.operator import result_store as result_store_module
from runtime.operator.client import GrpcStateProvider
from runtime.operator.models import LogLevel, RunState, RunStatus
from runtime.operator.operator import RunAlreadyExistsError, RunResultUnavailableError
from runtime.operator.result_store import (
    MAX_RESULT_MANIFEST_BYTES,
    ResultBundleMissingError,
    ResultPublicationCancelledError,
    ResultStore,
    ResultStoreLockedError,
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
from runtime.operator.state import OperatorStateStore, StateRowError
from runtime.operator.state.results import RunResultTable


@contextmanager
def _open_store(tmp_path: Path) -> Iterator[ResultStore]:
    state_store = OperatorStateStore.open(tmp_path / "operator" / "state.db")
    try:
        store = ResultStore(state_store)
        try:
            yield store
        finally:
            store.close()
    finally:
        state_store.close()


@pytest.fixture
def result_store(tmp_path) -> Iterator[ResultStore]:
    with _open_store(tmp_path) as store:
        yield store


def _indexed(operator: Operator, run_id: str) -> bool:
    with operator._state_store.transaction() as connection:
        return RunResultTable.get(connection, run_id) is not None


def _attachment_payload(content: bytes, name: str | None = None):
    """Exercise low-level legacy bundle storage, independently of public File encoding."""
    return EncodedWorkflowResult(
        value_json='{"version":1,"value":{"kind":"file","attachment_id":"file_0"}}',
        files=(
            ResultFileAttachment(
                attachment_id="file_0",
                content=content,
                name=name,
                media_type=None,
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        ),
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
            _attachment_payload(b"inode-bound"),
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
    operator = Operator(
        [str(workflow)], watch=False, schedule=False, cancel_grace=0.1, state_db=":memory:"
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


def test_spawned_results_materialize_nested_files_workspaces_and_large_payloads(result_client):
    operator, client = result_client
    run_id = client.start_run("result_bundle")
    assert _wait_for_terminal(client, run_id, timeout=20).status == RunStatus.SUCCESS
    payload = operator._get_run_result_payload(run_id)
    assert payload.files == ()
    assert len(payload.value_json) < 4096
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
    before = set(operator._result_store.root.rglob("*"))
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
    while set(operator._result_store.root.rglob("*")) != before:
        assert time.monotonic() < deadline, "Pending result files were not removed"
        time.sleep(0.03)


def test_result_codec_rejects_corrupt_file_content():
    content = b"expected"
    encoded = _attachment_payload(content, "data.bin")
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
    with _open_store(tmp_path) as store:
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX escaped descriptor attack")
def test_accept_is_immutable_after_namespace_discovery_and_same_user_mutation(result_store):
    store = result_store
    pending = store.prepare()
    digest = publish_workflow_result(
        _attachment_payload(b"stable"),
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
        assert Path(recovered_root) == store.root / "pending"

        stored = store.accept(pending, digest)
        mutate.set()
        status, discovered_names = result_queue.get(timeout=10)
        process.join(timeout=10)

        assert (status, process.exitcode) == ("mutated", 0), discovered_names
        assert not Path(pending.path).exists()
        # The accepted bundle is a new directory the escaped descriptor never reached.
        assert discovered_names == []
        assert stored.bundle_name != pending.name
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


def test_result_store_uses_private_modes_and_rejects_symlink_substitution(
    tmp_path, result_store
):
    store = result_store
    pending = store.prepare()
    encoded = _attachment_payload(b"safe", "safe.bin")
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
def test_spawned_publication_cannot_be_relocated_by_path_replacement(
    tmp_path, result_store, replace
):
    store = result_store
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
            published_bundle = moved / "pending" / pending.name if replace == "root" else moved
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


def test_result_publication_cancellation_never_publishes_partial_manifest(result_store):
    store = result_store
    pending = store.prepare()
    encoded = _attachment_payload(b"x" * (5 * 1024 * 1024))
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
    operator = Operator([], watch=False, schedule=False, state_db=":memory:")
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
        assert not _indexed(operator, run_id)
        assert operator._result_store.check_index().consistent
        assert not Path(pending.path).exists()
    finally:
        operator.close()


@pytest.mark.parametrize("rejection", ["unfinished-invocation", "publication-error"])
def test_rejected_success_publications_leave_no_stored_result(monkeypatch, rejection):
    operator = Operator([], watch=False, schedule=False, state_db=":memory:")
    monkeypatch.setattr(operator_module, "_teardown_process_group", lambda *_a, **_k: True)
    publish = operator._publish_run_locked

    def publish_unless_rejected(run, **kwargs):
        if rejection == "publication-error" and run.run_id.startswith("rejected-"):
            raise RuntimeError("injected publication failure")
        return publish(run, **kwargs)

    monkeypatch.setattr(operator, "_publish_run_locked", publish_unless_rejected)
    try:
        for index in range(3):
            run_id = f"rejected-{index}"
            _, handle, event = _provisional_success_run(operator, run_id)
            if rejection == "unfinished-invocation":
                operator._classifier_invocations[run_id] = {
                    "call": operator_module._ClassifierInvocationLifecycle(
                        node_id="classify",
                        invocation_index=0,
                        started_at=1.0,
                        declaration_digest=b"",
                        terminal=False,
                    )
                }
                operator._drain_run_events(run_id, handle, [event])
                run = operator.get_run(run_id)
                assert run.status == RunStatus.FAILED
            else:
                with pytest.raises(RuntimeError, match="injected publication failure"):
                    operator._drain_run_events(run_id, handle, [event])
            with pytest.raises(RunResultUnavailableError):
                operator.get_run_result(run_id)
            assert not _indexed(operator, run_id)
            assert operator._result_store.check_index().consistent

        run_id = "accepted-after-rejections"
        _, handle, event = _provisional_success_run(operator, run_id)
        operator._drain_run_events(run_id, handle, [event])
        assert operator.get_run(run_id).status == RunStatus.SUCCESS
        assert operator.get_run_result(run_id).content == b"stable"
    finally:
        operator.close()


def test_failed_result_index_write_fails_the_run_with_a_reason(monkeypatch):
    operator = Operator([], watch=False, schedule=False, state_db=":memory:")
    monkeypatch.setattr(operator_module, "_teardown_process_group", lambda *_a, **_k: True)

    def failing_insert(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(operator_module.RunResultTable, "insert", failing_insert)
    run_id = "run_index_write_fails"
    _, handle, event = _provisional_success_run(operator, run_id)
    try:
        operator._drain_run_events(run_id, handle, [event])
        assert operator.get_run(run_id).status == RunStatus.FAILED
        assert any(
            entry.node_id == "operator" and entry.level == LogLevel.ERROR
            for entry in operator.get_run(run_id).logs
        )
        with pytest.raises(RunResultUnavailableError):
            operator.get_run_result(run_id)
        assert not _indexed(operator, run_id)
        assert operator._result_store.check_index().consistent
    finally:
        operator.close()


def test_provisional_success_waits_for_delayed_exit_before_notification(monkeypatch):
    operator = Operator([], watch=False, schedule=False, state_db=":memory:")
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
        assert not _indexed(operator, run_id)
        release.set()
        drain.join(timeout=5)
        assert not drain.is_alive()
        assert operator.get_run(run_id).status == RunStatus.SUCCESS
        assert operator.get_run_result(run_id).content == b"stable"
    finally:
        release.set()
        drain.join(timeout=5)
        operator.close()


_RESTART_WORKFLOW = """
import avalanche as ava


@ava.workflow
def kept():
    return {
        "label": "kept",
        "file": ava.File(name="kept.bin", content=b"kept-bytes", content_type="text/plain"),
    }
"""


def _wait_for_success(operator: Operator, run_id: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = operator.get_run(run_id)
        if run is not None and run.status == RunStatus.SUCCESS:
            return
        assert run is None or run.status in {
            RunStatus.REQUESTING,
            RunStatus.PENDING,
            RunStatus.RUNNING,
        }, run
        time.sleep(0.02)
    pytest.fail(f"Run {run_id} did not succeed")


def _accept(store: ResultStore, content: bytes) -> StoredWorkflowResult:
    pending = store.prepare()
    digest = publish_workflow_result(
        _attachment_payload(content, "data.bin"),
        pending.descriptor,
        (pending.device, pending.inode),
        _NeverCancelled(),
    )
    return store.accept(pending, digest)


def _index(store: ResultStore, run_id: str, stored: StoredWorkflowResult) -> None:
    with store._state_store.transaction() as connection:
        RunResultTable.insert(connection, run_id, stored)


def test_results_survive_an_operator_restart_on_the_same_database(tmp_path):
    workflow = tmp_path / "kept.py"
    workflow.write_text(_RESTART_WORKFLOW)
    state_db = tmp_path / "operator" / "state.db"

    first = Operator([str(workflow)], watch=False, schedule=False, state_db=state_db)
    try:
        run_id = first.start_run("kept")
        _wait_for_success(first, run_id)
        before = first._get_run_result_payload(run_id)
        assert first._result_store.root == tmp_path / "operator" / "results"
    finally:
        first.close()

    second = Operator([str(workflow)], watch=False, schedule=False, state_db=state_db)
    try:
        # The run is stored too, so the restarted operator knows it succeeded.
        assert second.get_run(run_id).status is RunStatus.SUCCESS
        assert second._get_run_result_payload(run_id) == before
        result = second.get_run_result(run_id)
        assert result["label"] == "kept"
        assert result["file"].read_bytes() == b"kept-bytes"
        assert result["file"].name == "kept.bin"
        with pytest.raises(KeyError):
            second.get_run_result("run_unknown")
        # The stored result keeps its run ID taken after the restart.
        with pytest.raises(RunAlreadyExistsError):
            second.start_run("kept", run_id=run_id)
    finally:
        second.close()


def test_memory_database_results_live_in_a_temporary_root_removed_on_close(tmp_path):
    workflow = tmp_path / "kept.py"
    workflow.write_text(_RESTART_WORKFLOW)
    operator = Operator([str(workflow)], watch=False, schedule=False, state_db=":memory:")
    root = operator._result_store.root
    try:
        run_id = operator.start_run("kept")
        _wait_for_success(operator, run_id)
        assert operator.get_run_result(run_id)["file"].read_bytes() == b"kept-bytes"
        assert root.is_dir() and not root.is_relative_to(tmp_path)
    finally:
        operator.close()
    assert not root.exists()


def test_result_root_survives_reopen_and_clears_unfinished_pending_bundles(tmp_path):
    with _open_store(tmp_path) as store:
        root = store.root
        kept = _accept(store, b"kept")
        _index(store, "run_kept", kept)
        abandoned = store.prepare()
    assert Path(abandoned.path).is_dir()

    with _open_store(tmp_path) as reopened:
        assert reopened.root == root
        assert reopened.recovered_pending_bundles == 1
        assert not Path(abandoned.path).exists()
        assert reopened.load(kept).files[0].content == b"kept"
        assert reopened.check_index().consistent


def test_stale_pending_handle_never_touches_a_bundle_that_reused_its_fd(result_store):
    first = result_store.prepare()
    digest = publish_workflow_result(
        _attachment_payload(b"first", "first.bin"),
        first.descriptor,
        (first.device, first.inode),
        _NeverCancelled(),
    )
    stored = result_store.accept(first, digest)
    second = result_store.prepare()
    assert second.descriptor == first.descriptor  # the fd number was reused

    result_store.discard(first)  # stale handle from a cleanup path
    with pytest.raises(ValueError):
        result_store.accept(first, digest)

    assert Path(second.path).is_dir()
    os.fstat(second.descriptor)
    assert result_store.load(stored).files[0].content == b"first"
    result_store.discard(second)
    assert not Path(second.path).exists()


def test_result_store_close_releases_every_descriptor_when_one_close_fails(tmp_path):
    state_store = OperatorStateStore.open(tmp_path / "operator" / "state.db")
    try:
        store = ResultStore(state_store)
        broken = store.prepare()
        healthy = store.prepare()
        os.close(broken.descriptor)  # the store's own close of it now fails
        with pytest.raises(OSError):
            store.close()
        with pytest.raises(OSError):
            os.fstat(healthy.descriptor)
        # The root lock was released too, so the folder can be opened again.
        reopened = ResultStore(state_store)
        assert reopened.recovered_pending_bundles == 2
        reopened.close()
    finally:
        state_store.close()


def test_failed_temporary_root_removal_still_closes_every_descriptor(monkeypatch):
    state_store = OperatorStateStore.open(":memory:")
    store = ResultStore(state_store)
    root = store.root
    try:
        pending = store.prepare()
        root_descriptors = (store._root_fd, store._owner_fd, store._pending_fd)
        removal_failure = OSError("injected removal failure")

        def failing_removal():
            raise removal_failure

        monkeypatch.setattr(store, "_remove_temporary_root", failing_removal)
        with pytest.raises(OSError) as raised:
            store.close()
        assert raised.value is removal_failure
        for descriptor in (pending.descriptor, *root_descriptors):
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        shutil.rmtree(root)
        state_store.close()


@pytest.mark.parametrize("database", ["file", "memory"])
def test_failed_store_startup_releases_the_root_and_its_lock(tmp_path, monkeypatch, database):
    state_store = OperatorStateStore.open(
        tmp_path / "operator" / "state.db" if database == "file" else ":memory:"
    )
    created_roots: list[str] = []
    make_temporary_root = result_store_module.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        created_roots.append(make_temporary_root(*args, **kwargs))
        return created_roots[-1]

    try:
        startup_failure = OSError("injected startup failure")

        def failing_cleanup(*_args, **_kwargs):
            raise startup_failure

        with monkeypatch.context() as patched:
            patched.setattr(result_store_module, "_remove_bundles_in", failing_cleanup)
            patched.setattr(result_store_module.tempfile, "mkdtemp", recording_mkdtemp)
            with pytest.raises(OSError) as raised:
                ResultStore(state_store)
        assert raised.value is startup_failure
        if database == "memory":
            # The private temporary root is removed with the failed store.
            assert len(created_roots) == 1 and not Path(created_roots[0]).exists()
        else:
            # The failed store released its folder lock, so a new store opens it.
            ResultStore(state_store).close()
    finally:
        state_store.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions and symlinks")
def test_result_root_rejects_shared_broad_or_linked_roots(tmp_path):
    with _open_store(tmp_path):
        # Another database in the same folder would share results/.
        other_state = OperatorStateStore.open(tmp_path / "operator" / "other.db")
        try:
            with pytest.raises(ResultStoreLockedError):
                ResultStore(other_state)
        finally:
            other_state.close()

    broad = tmp_path / "broad"
    (broad / "results").mkdir(parents=True, mode=0o700)
    os.chmod(broad / "results", 0o755)
    linked = tmp_path / "linked"
    linked.mkdir()
    external = tmp_path / "external"
    external.mkdir(mode=0o700)
    (linked / "results").symlink_to(external, target_is_directory=True)
    for folder, refusal in ((broad, ValueError), (linked, OSError)):
        state_store = OperatorStateStore.open(folder / "state.db")
        try:
            with pytest.raises(refusal):
                ResultStore(state_store)
        finally:
            state_store.close()
    assert os.stat(broad / "results").st_mode & 0o777 == 0o755
    assert list(external.iterdir()) == []


def test_inconsistent_result_index_is_reported_at_startup_and_never_served(tmp_path, caplog):
    with _open_store(tmp_path) as store:
        kept = _accept(store, b"kept")
        _index(store, "run_kept", kept)
        lost = _accept(store, b"lost")
        _index(store, "run_lost", lost)
        orphan = _accept(store, b"orphan")
        root = store.root
    shutil.rmtree(root / lost.bundle_name)
    (root / "stray.txt").write_bytes(b"stray")

    with caplog.at_level("WARNING", logger=result_store_module.__name__):
        operator = Operator(
            [], watch=False, schedule=False, state_db=tmp_path / "operator" / "state.db"
        )
    try:
        assert caplog.records
        report = operator._result_store.check_index()
        assert not report.consistent
        assert [row.run_id for row in report.missing_bundles] == ["run_lost"]
        assert report.unindexed_bundles == (orphan.bundle_name,)
        assert report.unexpected_entries == ("stray.txt",)
        assert operator.get_run_result("run_kept").content == b"kept"
        with pytest.raises(ResultBundleMissingError):
            operator.get_run_result("run_lost")
        assert (root / orphan.bundle_name).is_dir()
    finally:
        operator.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [("bundle_name", "../escape"), ("manifest_sha256", "not-a-digest"), ("byte_size", -1)],
)
def test_malformed_result_rows_fail_loudly_instead_of_being_skipped(tmp_path, column, value):
    with _open_store(tmp_path) as store:
        stored = _accept(store, b"kept")
        _index(store, "run_bad", stored)
        connection = store._state_store._connection
        # Bypass the table's CHECK only for the column it guards, as a corrupt file would.
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE run_results SET {column} = ? WHERE run_id = ?", (value, "run_bad")
        )
        connection.execute("PRAGMA ignore_check_constraints = OFF")
        with pytest.raises(StateRowError):
            store.check_index()
        with store._state_store.transaction() as transaction, pytest.raises(StateRowError):
            RunResultTable.get(transaction, "run_bad")


def test_delete_run_result_removes_the_index_row_and_its_bundle(tmp_path):
    with _open_store(tmp_path) as store:
        stored = _accept(store, b"deleted")
        _index(store, "run_deleted", stored)
        lost = _accept(store, b"lost")
        _index(store, "run_lost", lost)
        shutil.rmtree(store.root / lost.bundle_name)

        deleted = store.delete_run_result("run_deleted")
        assert deleted is not None and deleted.run_id == "run_deleted"
        assert deleted.bundle_name == stored.bundle_name
        assert not (store.root / stored.bundle_name).exists()
        with pytest.raises(ResultBundleMissingError):
            store.load(stored)
        assert store.delete_run_result("run_deleted") is None

        # A row whose bundle is already gone is still deleted.
        assert store.delete_run_result("run_lost") is not None
        report = store.check_index()
        assert report.consistent


def test_forged_or_deleted_result_handles_are_rejected(result_client):
    operator, client = result_client
    run_id = client.start_run("result_bundle")
    assert _wait_for_terminal(client, run_id).status == RunStatus.SUCCESS
    with operator._state_store.transaction() as connection:
        stored = RunResultTable.get(connection, run_id)
    assert stored is not None
    forged = stored.model_copy(update={"manifest_sha256": "0" * 64})

    with pytest.raises(ValueError, match="sha256 does not match"):
        operator._result_store.load(forged)

    # A stored file changed on disk after acceptance is refused, never served.
    value_path = operator._result_store.root / stored.bundle_name / "value.json"
    original = value_path.read_bytes()
    value_path.write_bytes(original.replace(b"weekly", b"WEEKLY"))
    with pytest.raises(grpc.RpcError) as tampered:
        client.get_run_result(run_id)
    assert tampered.value.code() == grpc.StatusCode.DATA_LOSS
    value_path.write_bytes(original)
    assert client.get_run_result(run_id)["report"]["title"] == "weekly"

    operator._result_store.delete_run_result(run_id)
    with pytest.raises(ResultBundleMissingError):
        operator._result_store.load(stored)
    with pytest.raises(grpc.RpcError) as unavailable:
        client.get_run_result(run_id)
    assert unavailable.value.code() == grpc.StatusCode.FAILED_PRECONDITION


@pytest.mark.parametrize(
    "outcome", ["mutation", "cancel", "unreaped", "late-event", "failed-exit"]
)
def test_provisional_success_is_not_authority_to_publish(monkeypatch, outcome):
    operator = Operator([], watch=False, schedule=False, state_db=":memory:")
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


def test_result_documents_reject_ambiguous_json_keys():
    with pytest.raises(ValueError):
        decode_workflow_result(
            EncodedWorkflowResult(
                value_json='{"version":1,"value":{"kind":"scalar","value":"safe","value":"replaced"}}'
            )
        )
