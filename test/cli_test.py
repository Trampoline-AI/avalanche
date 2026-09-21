from __future__ import annotations

import hashlib
import json
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest


def test_ava_web_exits_cleanly_after_interrupt():
    with socket.socket() as target_socket:
        target_socket.bind(("127.0.0.1", 0))
        target_port = target_socket.getsockname()[1]
        with socket.socket() as web_socket:
            web_socket.bind(("127.0.0.1", 0))
            web_port = web_socket.getsockname()[1]

        process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "ava_cli",
                "web",
                "--connect",
                f"127.0.0.1:{target_port}",
                "--port",
                str(web_port),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None
                try:
                    with socket.create_connection(("127.0.0.1", web_port), timeout=0.1):
                        break
                except OSError:
                    assert time.monotonic() < deadline, "browser proxy did not become ready"
                    time.sleep(0.05)

            process.send_signal(signal.SIGINT)

            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_ava_result_materializes_nested_workspace_tree(monkeypatch, tmp_path, capsys):
    from ava_cli import app
    from avalanche import Workspace

    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "report.txt").write_text("report")

    class FakeProvider:
        def get_run_result(self, run_id):
            return {"output": Workspace.from_path(source)}

        def close(self):
            pass

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())
    output = tmp_path / "download"
    assert app.main(["result", "run_workspace", "--output-dir", str(output)]) == 0
    metadata = json.loads(capsys.readouterr().out)
    root = metadata["workspaces"][0]["path"]
    assert (output / root / "nested" / "report.txt").read_text() == "report"


def test_ava_result_rehashes_materialized_workspace_files(monkeypatch, tmp_path):
    from ava_cli import app
    from avalanche import Workspace

    source = tmp_path / "source"
    source.mkdir()
    (source / "report.txt").write_text("report")
    output = tmp_path / "download"
    write_exclusive_file = app._write_exclusive_file

    def corrupt_after_write(name, content, directory_fd):
        write_exclusive_file(name, content, directory_fd)
        if content == b"report":
            descriptor = app.os.open(
                name,
                app.os.O_WRONLY | app.os.O_TRUNC | app.os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                app.os.write(descriptor, b"tamper")
                app.os.fsync(descriptor)
            finally:
                app.os.close(descriptor)

    monkeypatch.setattr(app, "_write_exclusive_file", corrupt_after_write)

    with pytest.raises(ValueError):
        app._materialize_result(
            "run_corrupt_materialization",
            Workspace.from_path(source),
            output,
        )

    assert not output.exists()
    assert list(tmp_path.iterdir()) == [source]


def test_ava_result_safely_materializes_direct_and_nested_files(
    monkeypatch,
    tmp_path,
    capsys,
):
    from ava_cli import app
    from avalanche.runtime import File

    content = b"\x00\xffbinary"

    class FakeProvider:
        def get_run_result(self, run_id):
            return {
                "direct": File(name="../../escape.bin", content=content),
                "nested": [File(name=r"..\escape.bin", content=b"nested")],
            }

        def close(self):
            pass

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())
    output = tmp_path / "downloads"

    assert app.main(["result", "run_files", "--output-dir", str(output)]) == 0

    printed_text = capsys.readouterr().out
    assert "binary" not in printed_text
    printed = json.loads(printed_text)
    assert len(printed["files"]) == 2
    paths = [item["path"] for item in printed["files"]]
    assert len(set(paths)) == 2
    assert all("/" not in name and "\\" not in name and ".." not in name for name in paths)
    assert not (tmp_path / "escape.bin").exists()
    assert (output / paths[0]).read_bytes() == content
    assert printed["files"][0]["sha256"] == hashlib.sha256(content).hexdigest()


def test_ava_result_rejects_digest_mismatch_without_writing_file(
    monkeypatch,
    tmp_path,
    capsys,
):
    from ava_cli import app
    from avalanche.runtime import File

    invalid = File.model_construct(
        content=b"actual",
        name="value.bin",
        content_type=None,
        sha256="0" * 64,
    )

    class FakeProvider:
        def get_run_result(self, run_id):
            return invalid

        def close(self):
            pass

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())
    output = tmp_path / "downloads"

    assert app.main(["result", "run_bad", "--output-dir", str(output)]) == 1
    assert capsys.readouterr().err
    assert not output.exists()


def test_ava_result_atomic_publish_does_not_replace_a_racing_destination(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    rename_directory_noreplace = app._rename_directory_noreplace

    def race_destination(
        source_name,
        destination_name,
        source_directory_fd,
        destination_directory_fd,
    ):
        app.os.mkdir(
            destination_name,
            mode=0o700,
            dir_fd=destination_directory_fd,
        )
        return rename_directory_noreplace(
            source_name,
            destination_name,
            source_directory_fd,
            destination_directory_fd,
        )

    monkeypatch.setattr(
        app,
        "_rename_directory_noreplace",
        race_destination,
    )

    with pytest.raises(FileExistsError):
        app._materialize_result(
            "run_racing_destination",
            File(name="value.bin", content=b"new"),
            output,
        )

    assert output.is_dir()
    assert list(output.iterdir()) == []
    assert list(tmp_path.iterdir()) == [output]


def test_ava_result_rejects_source_name_substitution_and_removes_destination(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    rename_directory_noreplace = app._rename_directory_noreplace
    rename_returned = False

    def substitute_source(
        source_name,
        destination_name,
        source_directory_fd,
        destination_directory_fd,
    ):
        nonlocal rename_returned
        app.os.rename(
            source_name,
            "validated-output",
            src_dir_fd=source_directory_fd,
            dst_dir_fd=source_directory_fd,
        )
        app.os.mkdir(source_name, mode=0o700, dir_fd=source_directory_fd)
        replacement_fd = app.os.open(
            source_name,
            app.os.O_RDONLY | app.os.O_DIRECTORY | app.os.O_NOFOLLOW,
            dir_fd=source_directory_fd,
        )
        try:
            attacker_fd = app.os.open(
                "attacker.txt",
                app.os.O_WRONLY | app.os.O_CREAT | app.os.O_EXCL,
                0o600,
                dir_fd=replacement_fd,
            )
            app.os.close(attacker_fd)
        finally:
            app.os.close(replacement_fd)
        rename_directory_noreplace(
            source_name,
            destination_name,
            source_directory_fd,
            destination_directory_fd,
        )
        published_fd = app.os.open(
            destination_name,
            app.os.O_RDONLY | app.os.O_DIRECTORY | app.os.O_NOFOLLOW,
            dir_fd=destination_directory_fd,
        )
        try:
            app.os.stat("attacker.txt", dir_fd=published_fd, follow_symlinks=False)
        finally:
            app.os.close(published_fd)
        rename_returned = True

    monkeypatch.setattr(app, "_rename_directory_noreplace", substitute_source)

    with pytest.raises(ValueError):
        app._materialize_result(
            "run_substituted_source",
            File(name="value.bin", content=b"validated"),
            output,
        )

    assert rename_returned
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_ava_result_does_not_expose_destination_during_chunked_write(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    first_chunk_written = threading.Event()
    release = threading.Event()
    original_write = app.os.write
    paused = False

    def pause_after_first_chunk(descriptor, content):
        nonlocal paused
        written = original_write(descriptor, content)
        if not paused and len(content) == 1024 * 1024:
            paused = True
            first_chunk_written.set()
            assert release.wait(timeout=10)
        return written

    monkeypatch.setattr(app.os, "write", pause_after_first_chunk)
    failures = []

    def materialize():
        try:
            app._materialize_result(
                "run_chunked",
                File(name="large.bin", content=b"x" * (2 * 1024 * 1024)),
                output,
            )
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=materialize)
    thread.start()
    try:
        assert first_chunk_written.wait(timeout=10)
        assert not output.exists()
    finally:
        release.set()
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert failures == []
    attachment = next(output.glob("attachment-*"))
    assert attachment.read_bytes() == b"x" * (2 * 1024 * 1024)


@pytest.mark.parametrize("failure", [OSError("write failed"), KeyboardInterrupt()])
def test_ava_result_chunked_write_failure_never_exposes_destination(
    monkeypatch,
    tmp_path,
    failure,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    original_write = app.os.write
    chunk_writes = 0

    def fail_during_second_chunk(descriptor, content):
        nonlocal chunk_writes
        if len(content) == 1024 * 1024:
            chunk_writes += 1
            if chunk_writes == 2:
                raise failure
        return original_write(descriptor, content)

    monkeypatch.setattr(app.os, "write", fail_during_second_chunk)

    with pytest.raises(type(failure)):
        app._materialize_result(
            "run_interrupted",
            File(name="large.bin", content=b"x" * (2 * 1024 * 1024)),
            output,
        )

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_ava_result_rejects_output_directory_replacement_without_escape(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche.runtime import File

    parent = tmp_path / "parent"
    parent.mkdir()
    output = parent / "downloads"
    moved = tmp_path / "moved-parent"
    escape = tmp_path / "escape"
    escape.mkdir()
    write_exclusive_file = app._write_exclusive_file
    replaced = False

    def replace_before_write(name, content, directory_fd):
        nonlocal replaced
        if not replaced:
            replaced = True
            parent.rename(moved)
            parent.symlink_to(escape, target_is_directory=True)
        return write_exclusive_file(name, content, directory_fd)

    monkeypatch.setattr(app, "_write_exclusive_file", replace_before_write)

    with pytest.raises(ValueError):
        app._materialize_result(
            "run_replaced_directory",
            File(name="safe.bin", content=b"safe"),
            output,
        )

    assert list(escape.iterdir()) == []
    assert list(moved.iterdir()) == []


def test_ava_result_fails_closed_without_anchored_io_before_writing(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep.bin"
    sentinel.write_bytes(b"external")
    monkeypatch.setattr(app.os, "supports_dir_fd", set())

    with pytest.raises(RuntimeError):
        app._materialize_result(
            "run_unsupported_platform",
            File(name="safe.bin", content=b"result"),
            output,
        )

    assert not output.exists()
    assert sentinel.read_bytes() == b"external"
    assert list(external.iterdir()) == [sentinel]


@pytest.mark.parametrize("signum", (signal.SIGINT, signal.SIGTERM))
@pytest.mark.parametrize("command", ("dev", "operator"))
def test_startup_interrupts_blocking_discovery(monkeypatch, signum, command):
    import runtime.operator as operator_package
    from ava_cli import app
    from runtime.operator import operator as operator_module

    installed_handlers = {}

    def record_handler(registered_signum, handler):
        installed_handlers[registered_signum] = handler

    class BlockingOperator:
        def __init__(self, *_args, **_kwargs):
            installed_handlers[signum](signum, None)
            pytest.fail("Discovery continued after its shutdown signal")

    monkeypatch.setattr(app, "_configure_terminal_logging", lambda _level: None)
    monkeypatch.setattr(app.signal, "signal", record_handler)
    monkeypatch.setattr(operator_module, "Operator", BlockingOperator)
    monkeypatch.setattr(operator_package, "Operator", BlockingOperator)

    assert app.main([command, "examples"]) == 0


def test_ava_dev_reports_discovery_failure_without_starting_services(monkeypatch, capsys):
    from ava_cli import app
    from runtime.operator import operator as operator_module
    from runtime.operator.discovery import WorkflowDiscoveryError
    from runtime.operator.models import WorkflowDiscoveryDiagnostic

    starts = []

    class FailingOperator:
        def __init__(self, *_args, **_kwargs):
            raise WorkflowDiscoveryError(
                (
                    WorkflowDiscoveryDiagnostic(
                        path="flow.py",
                        kind="import_error",
                        message="No module named 'missing_helper'",
                    ),
                )
            )

    monkeypatch.setattr(app, "_configure_terminal_logging", lambda level: None)
    monkeypatch.setattr(operator_module, "Operator", FailingOperator)
    monkeypatch.setattr(
        "runtime.operator.server.serve", lambda *_args, **_kwargs: starts.append("grpc")
    )
    monkeypatch.setattr(
        "runtime.operator.web.start_browser_server",
        lambda *_args, **_kwargs: starts.append("web"),
    )

    assert app.main(["dev", "examples"]) == 1
    assert starts == []
    error = capsys.readouterr().err
    assert "No module named 'missing_helper'" in error


@pytest.mark.parametrize("command", ("dev", "operator"))
def test_http_start_failure_releases_operator_listener(monkeypatch, tmp_path, command):
    from ava_cli import app
    from runtime.operator import server as operator_server

    bound_ports = []
    serve = operator_server.serve

    def record_server(*args, **kwargs):
        server = serve(*args, **kwargs)
        bound_ports.append(server._avalanche_bound_port)
        return server

    monkeypatch.setattr(operator_server, "serve", record_server)
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        argv = [
            command,
            str(tmp_path),
            "--port",
            "0",
            "--web-port",
            str(occupied.getsockname()[1]),
        ]
        if command == "dev":
            assert app.main(argv) == 1
        else:
            with pytest.raises(OSError):
                app.main(argv)

    assert len(bound_ports) == 1
    with socket.socket() as rebound:
        rebound.bind(("127.0.0.1", bound_ports[0]))
    for sig, previous in previous_handlers.items():
        assert signal.getsignal(sig) == previous


def test_operator_fatal_error_closes_both_listeners_despite_http_cleanup_error(monkeypatch):
    from urllib.request import urlopen

    import runtime.operator as operator_module
    from runtime.operator import server as operator_server
    from runtime.operator import web

    fatal_error = RuntimeError("operator failed")
    bound_ports = []
    serve = operator_server.serve
    start_browser_server = web.start_browser_server
    close_browser = web.BrowserServer.close
    endpoints = []

    def record_server(*args, **kwargs):
        server = serve(*args, **kwargs)
        bound_ports.append(server._avalanche_bound_port)
        return server

    def record_browser(*args, **kwargs):
        browser = start_browser_server(*args, **kwargs)
        bound_ports.append(browser.port)
        endpoints.append(browser.endpoint)
        return browser

    def fail_after_request(self, *, timeout):
        with urlopen(f"{endpoints[0]}/api/v1/flows", timeout=2) as response:
            assert response.status == 200
            assert json.load(response)["flows"] == []
        return fatal_error

    def fail_after_close(self):
        close_browser(self)
        raise RuntimeError("HTTP cleanup failed")

    monkeypatch.setattr(operator_server, "serve", record_server)
    monkeypatch.setattr(web, "start_browser_server", record_browser)
    monkeypatch.setattr(web.BrowserServer, "close", fail_after_close)
    monkeypatch.setattr(operator_module.Operator, "wait_for_failure", fail_after_request)

    with pytest.raises(RuntimeError) as caught:
        operator_module.serve(
            [],
            host="0.0.0.0",
            port=0,
            web_port=0,
            webhook_port=0,
            watch=False,
            schedule=False,
        )
    assert caught.value is fatal_error
    assert endpoints[0].startswith("http://127.0.0.1:")
    assert len(bound_ports) == 2
    for port in bound_ports:
        with socket.socket() as rebound:
            rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rebound.bind(("127.0.0.1", port))


@pytest.mark.parametrize(
    "argv",
    (
        ["operator", "--port", "7435"],
        ["operator", "--web-port", "7434"],
        ["operator", "--port", "7434", "--no-web"],
        ["dev", "--web-port", "7434"],
    ),
)
def test_conflicting_listener_ports_fail_before_discovery(monkeypatch, argv):
    from ava_cli import app

    def unexpected_discovery(*args, **kwargs):
        pytest.fail("Conflicting ports reached discovery")

    monkeypatch.setattr(app, "select_workflow_targets", unexpected_discovery)
    with pytest.raises(SystemExit) as caught:
        app.main(argv)
    assert caught.value.code == 2


def test_no_web_allows_an_occupied_http_port(monkeypatch, tmp_path):
    import grpc

    from ava_cli import app
    from runtime.operator import Operator
    from runtime.operator import server as operator_server
    from runtime.operator.proto import operator_pb2 as pb
    from runtime.operator.proto import operator_pb2_grpc as pb_grpc

    bound_ports = []
    serve = operator_server.serve
    wait_for_failure = Operator.wait_for_failure

    def record_server(*args, **kwargs):
        server = serve(*args, **kwargs)
        bound_ports.append(server._avalanche_bound_port)
        return server

    def interrupt_after_rpc(self, *, timeout):
        if not bound_ports:
            return wait_for_failure(self, timeout=timeout)
        with grpc.insecure_channel(f"127.0.0.1:{bound_ports[0]}") as channel:
            response = pb_grpc.OperatorServiceV2Stub(channel).DiscoverFlows(
                pb.DiscoverFlowsRequestV2(), timeout=2
            )
            assert list(response.flows) == []
        raise KeyboardInterrupt

    monkeypatch.setattr(operator_server, "serve", record_server)
    monkeypatch.setattr(Operator, "wait_for_failure", interrupt_after_rpc)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        assert (
            app.main(
                [
                    "operator",
                    str(tmp_path),
                    "--port",
                    "0",
                    "--no-web",
                    "--web-port",
                    str(occupied.getsockname()[1]),
                ]
            )
            == 0
        )
    with socket.socket() as rebound:
        rebound.bind(("127.0.0.1", bound_ports[0]))
