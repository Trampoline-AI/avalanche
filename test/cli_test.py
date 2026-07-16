from __future__ import annotations

import importlib.util
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
    assert calls == [["--flows", "examples", "--port", "17777", "--ray"]]


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


def test_ava_run_passes_json_context_file_and_s3_inputs(monkeypatch, tmp_path, capsys):
    from ava_cli import app

    file_path = tmp_path / "input.txt"
    file_path.write_bytes(b"cli-bytes")
    captured = {}

    class FakeProvider:
        def start_run(self, flow_name, *, input=None, context=None, files=None, s3_files=None):
            captured["flow_name"] = flow_name
            captured["input"] = input
            captured["context"] = context
            captured["files"] = files
            captured["s3_files"] = s3_files
            return "run_cli"

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())

    assert app.main([
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
        "--s3-file",
        "document_ref=s3://bucket/cli.txt",
    ]) == 0

    assert captured["flow_name"] == "input_workflow"
    assert captured["input"] == {"message": "from-cli"}
    assert captured["context"] == {"request_id": "req_cli"}
    assert captured["files"]["document"].read_bytes() == b"cli-bytes"
    assert captured["s3_files"]["document_ref"].uri == "s3://bucket/cli.txt"
    assert captured["closed"] is True
    assert capsys.readouterr().out.strip() == "run_cli"


def test_ava_run_prints_provider_error_when_run_does_not_start(monkeypatch, capsys):
    from ava_cli import app

    class FakeProvider:
        last_error = "INVALID_ARGUMENT: too many inline file bytes"

        def start_run(self, flow_name, *, input=None, context=None, files=None, s3_files=None):
            return ""

        def close(self):
            pass

    monkeypatch.setattr(app, "_make_provider", lambda address: FakeProvider())

    assert app.main(["run", "input_workflow"]) == 1
    assert capsys.readouterr().err.strip() == (
        "INVALID_ARGUMENT: too many inline file bytes"
    )


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
