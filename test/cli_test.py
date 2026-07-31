from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import threading
import tomllib
from pathlib import Path

import pytest


def test_pyproject_exposes_ava_console_script_and_cli_package():
    data = tomllib.loads(Path("pyproject.toml").read_text())

    assert data["project"]["scripts"] == {"ava": "ava_cli:main"}

    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist = data["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "src/ava_cli" in wheel["include"]
    assert "src/ava_cli" in sdist["include"]


def test_ava_cli_package_resolves_to_project_source():
    spec = importlib.util.find_spec("ava_cli")

    assert spec is not None
    assert spec.origin is not None
    assert Path(spec.origin).resolve() == Path("src/ava_cli/__init__.py").resolve()


def test_ava_help_lists_supported_commands_without_init_or_workflows(capsys):
    from ava_cli import app

    with pytest.raises(SystemExit) as exc_info:
        app.main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "operator" in output
    assert "run" in output
    assert "result" in output
    assert "tui" in output
    assert "dev" in output
    assert "init" not in output
    assert "--" + "workflows" not in output


def test_ava_operator_delegates_to_runtime_operator_with_flows(monkeypatch):
    from ava_cli import app

    calls = []

    def fake_operator_main(argv):
        calls.append(argv)
        return 0

    monkeypatch.setattr(app, "_operator_main", fake_operator_main)

    assert app.main(["operator", "--flows", "examples", "--port", "17777", "--ray"]) == 0
    assert calls == [
        [
            "--flows",
            "examples",
            "--host",
            "127.0.0.1",
            "--port",
            "17777",
            "--webhook-port",
            "7434",
            "--ray",
        ]
    ]


def test_ava_operator_delegates_web_listener_configuration(monkeypatch):
    from ava_cli import app

    calls = []
    monkeypatch.setattr(app, "_operator_main", lambda argv: calls.append(argv) or 0)

    assert (
        app.main(
            [
                "operator",
                "--flows",
                "examples",
                "--web",
                "--web-host",
                "0.0.0.0",
                "--web-port",
                "17778",
                "--web-trusted-proxy",
            ]
        )
        == 0
    )
    assert calls[0][-6:] == [
        "--web",
        "--web-host",
        "0.0.0.0",
        "--web-port",
        "17778",
        "--web-trusted-proxy",
    ]


def test_ava_operator_rejects_old_workflows_flag():
    from ava_cli import app

    with pytest.raises(SystemExit) as exc_info:
        app.main(["operator", "--" + "workflows", "examples"])

    assert exc_info.value.code != 0


def test_ava_tui_delegates_to_tui_entrypoint(monkeypatch):
    from ava_cli import app

    captured_argv = []

    def fake_launch_tui(argv):
        captured_argv.extend(argv)
        return None

    monkeypatch.setattr(app, "_launch_tui", fake_launch_tui)

    assert app.main(["tui", "--connect", "localhost:7433"]) == 0
    assert captured_argv == ["--connect", "localhost:7433"]


def test_ava_run_rejects_removed_s3_file_flag():
    from ava_cli import app

    with pytest.raises(SystemExit) as exc_info:
        app.main(
            [
                "run",
                "input_workflow",
                "--s3-file",
                "document=s3://bucket/document.txt",
            ]
        )

    assert exc_info.value.code != 0


def test_ava_run_help_points_to_cli_and_python_result_retrieval(capsys):
    from ava_cli import app

    with pytest.raises(SystemExit) as exc_info:
        app.main(["run", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "ava result RUN_ID --output-dir PATH" in output
    assert "GrpcStateProvider.get_run_result(run_id)" in output


def test_ava_run_captures_workspace_inputs_and_rejects_duplicate_bindings(
    monkeypatch, tmp_path, capsys
):
    from ava_cli import app
    from avalanche import Workspace

    source = tmp_path / "source"
    source.mkdir()
    (source / "nested.txt").write_text("workspace")
    captured = {}

    class FakeProvider:
        def start_run(self, flow, **kwargs):
            captured.update(kwargs)
            return "run_workspace"

        def close(self):
            pass

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())
    assert app.main(["run", "flow", "--workspace", f"workspace={source}"]) == 0
    assert isinstance(captured["input"]["workspace"], Workspace)
    assert {
        entry.path: entry.content
        for entry in captured["input"]["workspace"].entries
        if entry.kind == "file"
    } == {"nested.txt": b"workspace"}
    assert (
        app.main(
            [
                "run",
                "flow",
                "--workspace",
                f"workspace={source}",
                "--workspace",
                f"workspace={source}",
            ]
        )
        == 1
    )
    assert (
        app.main(
            [
                "run",
                "flow",
                "--input",
                '{"workspace":"already-bound"}',
                "--workspace",
                f"workspace={source}",
            ]
        )
        == 1
    )
    assert (
        app.main(
            [
                "run",
                "flow",
                "--file",
                f"workspace={source / 'nested.txt'}",
                "--workspace",
                f"workspace={source}",
            ]
        )
        == 1
    )
    assert capsys.readouterr().err.count("Duplicate input field 'workspace'") == 3


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


def test_ava_result_verifies_workspace_digest_before_publication(tmp_path):
    from ava_cli import app
    from avalanche import Workspace
    from avalanche.workspace import WorkspaceEntry

    corrupt = Workspace.model_construct(
        entries=(
            WorkspaceEntry.model_construct(
                path="report.txt",
                kind="file",
                content=b"report",
                sha256="0" * 64,
            ),
        )
    )
    output = tmp_path / "download"

    with pytest.raises(ValueError, match="sha256"):
        app._materialize_result("run_corrupt_workspace", corrupt, output)

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


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

    with pytest.raises(ValueError, match="failed digest verification"):
        app._materialize_result(
            "run_corrupt_materialization",
            Workspace.from_path(source),
            output,
        )

    assert not output.exists()
    assert list(tmp_path.iterdir()) == [source]


def test_ava_result_recursively_syncs_workspace_directories(monkeypatch, tmp_path):
    from ava_cli import app
    from avalanche import Workspace

    source = tmp_path / "source"
    (source / "nested" / "deep").mkdir(parents=True)
    (source / "nested" / "deep" / "report.txt").write_text("report")
    output = tmp_path / "download"
    fsync = app.os.fsync
    synced_directories: set[tuple[int, int]] = set()

    def record_fsync(descriptor):
        metadata = app.os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            synced_directories.add((metadata.st_dev, metadata.st_ino))
        return fsync(descriptor)

    monkeypatch.setattr(app.os, "fsync", record_fsync)

    metadata = app._materialize_result(
        "run_synced_workspace",
        Workspace.from_path(source),
        output,
    )

    root = output / metadata["workspaces"][0]["path"]
    published_directories = {
        (path.stat().st_dev, path.stat().st_ino)
        for path in (root, root / "nested", root / "nested" / "deep")
    }
    assert published_directories <= synced_directories


def test_ava_result_rejects_workspace_beyond_cleanup_depth_before_staging(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche import Workspace

    entries = []
    parts = []
    for index in range(app._MAX_STAGED_OUTPUT_DEPTH):
        parts.append(f"level-{index}")
        entries.append({"kind": "directory", "path": "/".join(parts)})
    workspace = Workspace.from_manifest({"version": 1, "entries": entries})

    def unexpected_staging():
        raise AssertionError("CLI preflight did not run before staging")

    monkeypatch.setattr(app, "_require_anchored_output_io", unexpected_staging)

    with pytest.raises(ValueError, match="materialization depth limit"):
        app._materialize_result("run_too_deep", workspace, tmp_path / "download")

    assert list(tmp_path.iterdir()) == []


def test_ava_result_rejects_workspace_beyond_cleanup_entry_budget_before_staging(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche import Workspace

    workspace = Workspace.from_manifest(
        {
            "version": 1,
            "entries": [
                {"kind": "directory", "path": f"entry-{index:04d}"}
                for index in range(app._MAX_STAGED_OUTPUT_ENTRIES - 1)
            ],
        }
    )

    def unexpected_staging():
        raise AssertionError("CLI preflight did not run before staging")

    monkeypatch.setattr(app, "_require_anchored_output_io", unexpected_staging)

    with pytest.raises(ValueError, match="cleanup entry limit"):
        app._materialize_result("run_too_wide", workspace, tmp_path / "download")

    assert list(tmp_path.iterdir()) == []


def test_ava_result_materializes_wide_workspace_with_bounded_descriptors(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche import Workspace

    workspace = Workspace.from_manifest(
        {
            "version": 1,
            "entries": [
                {"kind": "directory", "path": f"directory-{index:04d}"} for index in range(256)
            ],
        }
    )
    output = tmp_path / "download"
    open_private_directory = app._open_private_directory
    tracked_descriptors: list[int] = []
    maximum_open = 0

    def limited_open_private_directory(*args, **kwargs):
        nonlocal maximum_open
        still_open = []
        for descriptor in tracked_descriptors:
            try:
                app.os.fstat(descriptor)
            except OSError:
                continue
            still_open.append(descriptor)
        tracked_descriptors[:] = still_open
        if len(tracked_descriptors) >= 12:
            raise OSError(app.errno.EMFILE, "simulated descriptor exhaustion")
        descriptor, identity = open_private_directory(*args, **kwargs)
        tracked_descriptors.append(descriptor)
        maximum_open = max(maximum_open, len(tracked_descriptors))
        return descriptor, identity

    monkeypatch.setattr(
        app,
        "_open_private_directory",
        limited_open_private_directory,
    )

    app._materialize_result("run_wide_workspace", workspace, output)

    assert output.is_dir()
    assert maximum_open < 12


def test_ava_result_help_documents_no_replace_and_local_namespace_contract(capsys):
    from ava_cli import app

    with pytest.raises(SystemExit) as exc_info:
        app.main(["result", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "new destination directory" in output
    assert "must not already exist" in output
    normalized = " ".join(output.split())
    assert "atomic no-replace rename" in normalized
    assert "immediately verifies the destination identity" in normalized
    assert "caller-owned local namespace" in normalized
    assert "Descriptor-authenticated catchable state is cleaned" in normalized
    assert "before descriptor acquisition" in normalized
    assert "safe cleanup cannot distinguish a same-" in normalized
    assert "name replacement" in normalized
    assert "requested destination remains absent" in normalized
    assert "same user" in normalized


def test_ava_result_writes_scalar_metadata_without_binary_output(
    monkeypatch,
    tmp_path,
    capsys,
):
    from ava_cli import app

    class FakeProvider:
        def get_run_result(self, run_id):
            assert run_id == "run_scalar"
            return 42

        def close(self):
            pass

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())
    output = tmp_path / "results"

    assert (
        app.main(
            [
                "result",
                "run_scalar",
                "--connect",
                "localhost:9999",
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )

    printed = json.loads(capsys.readouterr().out)
    assert printed["result"] == 42
    assert printed["files"] == []
    metadata = json.loads((output / printed["metadata_path"]).read_text())
    assert metadata == {
        "files": [],
        "result": 42,
        "run_id": "run_scalar",
    }


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
    assert "digest does not match" in capsys.readouterr().err
    assert not output.exists()


def test_ava_result_rejects_an_existing_destination_without_merging(tmp_path):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    output.mkdir()
    sentinel = output / "existing.bin"
    sentinel.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="must not already exist"):
        app._materialize_result(
            "run_existing_destination",
            File(name="value.bin", content=b"new"),
            output,
        )

    assert sentinel.read_bytes() == b"existing"
    assert list(output.iterdir()) == [sentinel]


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

    with pytest.raises(FileExistsError, match="must not already exist"):
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

    with pytest.raises(ValueError, match="published output identity changed"):
        app._materialize_result(
            "run_substituted_source",
            File(name="value.bin", content=b"validated"),
            output,
        )

    assert rename_returned
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_ava_result_publishes_from_held_directory_after_holding_path_replacement(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    rename_directory_noreplace = app._rename_directory_noreplace
    replacement_name = None

    def replace_holding_path(
        source_name,
        destination_name,
        source_directory_fd,
        destination_directory_fd,
    ):
        nonlocal replacement_name
        holding_names = [
            entry.name
            for entry in app.os.scandir(destination_directory_fd)
            if entry.name.startswith(".avalanche-result-")
        ]
        assert len(holding_names) == 1
        replacement_name = holding_names[0]
        app.os.rename(
            replacement_name,
            "moved-private-holding",
            src_dir_fd=destination_directory_fd,
            dst_dir_fd=destination_directory_fd,
        )
        app.os.mkdir(
            replacement_name,
            mode=0o700,
            dir_fd=destination_directory_fd,
        )
        return rename_directory_noreplace(
            source_name,
            destination_name,
            source_directory_fd,
            destination_directory_fd,
        )

    monkeypatch.setattr(app, "_rename_directory_noreplace", replace_holding_path)

    app._materialize_result(
        "run_replaced_holding",
        File(name="value.bin", content=b"held-authority"),
        output,
    )

    assert next(output.glob("attachment-*")).read_bytes() == b"held-authority"
    assert not (tmp_path / "moved-private-holding").exists()
    replacement = tmp_path / replacement_name
    assert replacement.is_dir()
    assert list(replacement.iterdir()) == []
    replacement.rmdir()


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

    with pytest.raises(type(failure), match=str(failure)):
        app._materialize_result(
            "run_interrupted",
            File(name="large.bin", content=b"x" * (2 * 1024 * 1024)),
            output,
        )

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("completed_writes", [1, 2])
def test_ava_result_interruption_after_each_durable_write_cleans_actual_tree(
    monkeypatch,
    tmp_path,
    completed_writes,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    write_exclusive_file = app._write_exclusive_file
    writes = 0

    def interrupt_after_write(name, content, directory_fd):
        nonlocal writes
        write_exclusive_file(name, content, directory_fd)
        writes += 1
        if writes == completed_writes:
            raise KeyboardInterrupt(f"interrupted after durable write {writes}")

    monkeypatch.setattr(app, "_write_exclusive_file", interrupt_after_write)

    with pytest.raises(KeyboardInterrupt, match="after durable write"):
        app._materialize_result(
            "run_post_write_interrupt",
            File(name="value.bin", content=b"value"),
            output,
        )

    assert writes == completed_writes
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_ava_result_interruption_after_holding_mkdir_leaves_private_empty_residue(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    mkdir = app.os.mkdir
    interrupted = False

    def interrupt_after_holding_mkdir(name, mode=0o777, *, dir_fd=None):
        nonlocal interrupted
        mkdir(name, mode=mode, dir_fd=dir_fd)
        if str(name).startswith(".avalanche-result-"):
            interrupted = True
            raise KeyboardInterrupt("interrupted after holding mkdir")

    monkeypatch.setattr(app.os, "mkdir", interrupt_after_holding_mkdir)
    monkeypatch.setattr(
        app.os,
        "supports_dir_fd",
        {*app.os.supports_dir_fd, interrupt_after_holding_mkdir},
    )

    with pytest.raises(KeyboardInterrupt, match="after holding mkdir"):
        app._materialize_result(
            "run_post_holding_mkdir_interrupt",
            File(name="value.bin", content=b"value"),
            output,
        )

    assert interrupted
    assert not output.exists()
    entries = list(tmp_path.iterdir())
    assert len(entries) == 1
    assert entries[0].name.startswith(".avalanche-result-")
    assert entries[0].is_dir()
    assert list(entries[0].iterdir()) == []
    assert stat.S_IMODE(entries[0].stat().st_mode) == 0o700


def test_ava_result_holding_mkdir_raise_does_not_adopt_same_name_replacement(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    mkdir = app.os.mkdir
    moved_name = "moved-original-holding"
    replacement_name = None

    def replace_after_holding_mkdir(name, mode=0o777, *, dir_fd=None):
        nonlocal replacement_name
        mkdir(name, mode=mode, dir_fd=dir_fd)
        if str(name).startswith(".avalanche-result-"):
            replacement_name = str(name)
            app.os.rename(
                name,
                moved_name,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            mkdir(name, mode=0o700, dir_fd=dir_fd)
            raise KeyboardInterrupt("interrupted after replacing holding directory")

    monkeypatch.setattr(app.os, "mkdir", replace_after_holding_mkdir)
    monkeypatch.setattr(
        app.os,
        "supports_dir_fd",
        {*app.os.supports_dir_fd, replace_after_holding_mkdir},
    )

    with pytest.raises(KeyboardInterrupt, match="after replacing holding directory"):
        app._materialize_result(
            "run_replaced_post_holding_mkdir",
            File(name="value.bin", content=b"value"),
            output,
        )

    assert replacement_name is not None
    replacement = tmp_path / replacement_name
    moved_original = tmp_path / moved_name
    assert not output.exists()
    assert replacement.is_dir()
    assert moved_original.is_dir()
    assert list(replacement.iterdir()) == []
    assert list(moved_original.iterdir()) == []
    assert stat.S_IMODE(replacement.stat().st_mode) == 0o700
    assert replacement.stat().st_uid == app.os.getuid()


def test_ava_result_interruption_immediately_after_successful_rename_cleans_destination(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    rename_directory_noreplace = app._rename_directory_noreplace
    renamed = False

    def interrupt_after_rename(
        source_name,
        destination_name,
        source_directory_fd,
        destination_directory_fd,
    ):
        nonlocal renamed
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
            with app.os.scandir(published_fd) as entries:
                assert next(entries, None) is not None
        finally:
            app.os.close(published_fd)
        renamed = True
        raise KeyboardInterrupt("interrupted after successful rename")

    monkeypatch.setattr(app, "_rename_directory_noreplace", interrupt_after_rename)

    with pytest.raises(KeyboardInterrupt, match="after successful rename"):
        app._materialize_result(
            "run_post_rename_interrupt",
            File(name="value.bin", content=b"value"),
            output,
        )

    assert renamed
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_ava_result_unexpected_staged_entry_fails_closed_and_is_enumerated(
    monkeypatch,
    tmp_path,
):
    from ava_cli import app
    from avalanche.runtime import File

    output = tmp_path / "downloads"
    validate_staged_entries = app._validate_staged_entries

    def inject_unexpected_entry(directory_fd, expected_names):
        descriptor = app.os.open(
            "unexpected.bin",
            app.os.O_WRONLY | app.os.O_CREAT | app.os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
        app.os.close(descriptor)
        return validate_staged_entries(directory_fd, expected_names)

    monkeypatch.setattr(app, "_validate_staged_entries", inject_unexpected_entry)

    with pytest.raises(ValueError, match="unexpected entries"):
        app._materialize_result(
            "run_unexpected_staged_entry",
            File(name="value.bin", content=b"value"),
            output,
        )

    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_ava_result_cleanup_enumeration_stops_at_its_entry_budget(tmp_path):
    from ava_cli import app

    staged = tmp_path / "staged"
    staged.mkdir()
    for index in range(3):
        (staged / f"entry-{index}").write_bytes(b"value")
    descriptor = app.os.open(staged, app.os.O_RDONLY | app.os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="entry limit"):
            app._bounded_clear_directory(descriptor, [2], depth=0)
    finally:
        app.os.close(descriptor)

    assert len(list(staged.iterdir())) == 1


def test_ava_result_rejects_repeated_file_object_before_any_write(tmp_path):
    from ava_cli import app
    from avalanche.runtime import File

    attachment = File(name="one.bin", content=b"one-blob")
    output = tmp_path / "downloads"

    with pytest.raises(ValueError, match="repeats the same file attachment"):
        app._materialize_result(
            "run_duplicate_reference",
            [attachment, attachment],
            output,
        )

    assert not output.exists()


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

    with pytest.raises(ValueError, match="parent changed"):
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

    with pytest.raises(RuntimeError, match="directory-anchored file operations"):
        app._materialize_result(
            "run_unsupported_platform",
            File(name="safe.bin", content=b"result"),
            output,
        )

    assert not output.exists()
    assert sentinel.read_bytes() == b"external"
    assert list(external.iterdir()) == [sentinel]


def test_ava_result_reports_unsupported_anchored_io_without_traceback(
    monkeypatch,
    tmp_path,
    capsys,
):
    from ava_cli import app
    from avalanche.runtime import File

    class FakeProvider:
        def get_run_result(self, run_id):
            return File(name="safe.bin", content=b"result")

        def close(self):
            pass

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())
    monkeypatch.setattr(app.os, "supports_dir_fd", set())
    output = tmp_path / "downloads"

    assert app.main(["result", "run_unsupported", "--output-dir", str(output)]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == (
        "Secure result materialization is unavailable: this platform does not "
        "support directory-anchored file operations"
    )
    assert not output.exists()


def test_ava_result_waits_for_success_before_retrieval(monkeypatch, tmp_path):
    from ava_cli import app
    from avalanche.operator.models import RunStatus

    statuses = iter([RunStatus.RUNNING, RunStatus.SUCCESS])

    class Run:
        def __init__(self, status):
            self.status = status

    class FakeProvider:
        last_error = ""

        def get_run(self, run_id):
            return Run(next(statuses))

        def get_run_result(self, run_id):
            return {"done": True}

        def close(self):
            pass

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())
    monkeypatch.setattr(app.time, "sleep", lambda _seconds: None)

    assert (
        app.main(
            [
                "result",
                "run_wait",
                "--output-dir",
                str(tmp_path / "results"),
                "--wait",
                "--timeout",
                "1",
            ]
        )
        == 0
    )


def test_ava_run_passes_json_context_and_file_inputs(monkeypatch, tmp_path, capsys):
    from ava_cli import app

    file_path = tmp_path / "input.txt"
    file_content = b"x" * (4 * 1024 * 1024 + 1)
    file_path.write_bytes(file_content)
    captured = {}

    class FakeProvider:
        def start_run(self, flow_name, *, input=None, context=None, files=None):
            captured["flow_name"] = flow_name
            captured["input"] = input
            captured["context"] = context
            captured["files"] = files
            return "run_cli"

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())

    assert (
        app.main(
            [
                "run",
                "input_workflow",
                "--connect",
                "localhost:9999",
                "--input",
                '{"message": "from-cli"}',
                "--context",
                '{"request_id": "req_cli"}',
                "--file",
                f"document={file_path}",
            ]
        )
        == 0
    )

    assert captured["flow_name"] == "input_workflow"
    assert captured["input"] == {"message": "from-cli"}
    assert captured["context"] == {"request_id": "req_cli"}
    assert captured["files"]["document"].read_bytes() == file_content
    assert captured["closed"] is True
    assert capsys.readouterr().out.strip() == "run_cli"


def test_ava_run_prints_provider_error_when_run_does_not_start(monkeypatch, capsys):
    from ava_cli import app

    class FakeProvider:
        last_error = "INVALID_ARGUMENT: too many inline file bytes"

        def start_run(self, flow_name, *, input=None, context=None, files=None):
            return ""

        def close(self):
            pass

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())

    assert app.main(["run", "input_workflow"]) == 1
    assert capsys.readouterr().err.strip() == ("INVALID_ARGUMENT: too many inline file bytes")


def test_ava_run_preserves_ambiguity_candidates_on_stderr(monkeypatch, capsys):
    from ava_cli import app

    class FakeProvider:
        last_error = (
            "INVALID_ARGUMENT: 'shared' is ambiguous:\n"
            "  left/flow.py::shared\n"
            "  right/flow.py::shared"
        )

        def start_run(self, workflow_selector, **kwargs):
            return ""

        def close(self):
            pass

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())

    assert app.main(["run", "shared"]) == 1
    error = capsys.readouterr().err
    assert "left/flow.py::shared" in error
    assert "right/flow.py::shared" in error


def test_ava_dev_starts_operator_waits_launches_tui_and_stops_operator(monkeypatch):
    from ava_cli import app

    events = []

    class FakeProcess:
        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append(("wait", timeout))

        def kill(self):
            events.append("kill")

    class FakeProvider:
        def __init__(self):
            self.pings = 0

        def ping(self):
            self.pings += 1
            events.append(("ping", self.pings))
            return self.pings == 2

        def close(self):
            events.append("close")

    fake_provider = FakeProvider()

    def fake_find_free_port():
        return 18888

    def fake_start_operator_process(flows, port, use_ray):
        events.append(("start", flows, port, use_ray))
        return FakeProcess()

    def fake_make_provider(address):
        events.append(("provider", address))
        return fake_provider

    def fake_sleep(seconds):
        events.append(("sleep", seconds))

    def fake_launch_connected_tui(address):
        events.append(("tui", address))

    monkeypatch.setattr(app, "_find_free_port", fake_find_free_port)
    monkeypatch.setattr(app, "_start_operator_process", fake_start_operator_process)
    monkeypatch.setattr(app, "_make_provider", fake_make_provider)
    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    monkeypatch.setattr(app, "_launch_connected_tui", fake_launch_connected_tui)

    assert app.main(["dev", "--flows", "examples", "--ray"]) == 0
    assert events == [
        ("start", ["examples"], 18888, True),
        ("provider", "localhost:18888"),
        ("ping", 1),
        ("sleep", 0.1),
        ("ping", 2),
        ("tui", "localhost:18888"),
        "close",
        "terminate",
        ("wait", 5),
    ]


def test_ava_dev_stops_operator_when_tui_raises(monkeypatch):
    from ava_cli import app

    events = []

    class FakeProcess:
        def terminate(self):
            events.append("terminate")

        def wait(self, timeout=None):
            events.append(("wait", timeout))

        def kill(self):
            events.append("kill")

    class FakeProvider:
        def ping(self):
            return True

        def close(self):
            events.append("close")

    monkeypatch.setattr(app, "_find_free_port", lambda: 18889)
    monkeypatch.setattr(
        app,
        "_start_operator_process",
        lambda flows, port, use_ray: FakeProcess(),
    )
    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())

    def fake_launch_connected_tui(address):
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "_launch_connected_tui", fake_launch_connected_tui)

    with pytest.raises(KeyboardInterrupt):
        app.main(["dev", "--flows", "examples"])

    assert events == ["close", "terminate", ("wait", 5)]
