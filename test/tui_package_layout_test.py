import importlib
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest


def test_tui_implementation_package_exports_application():
    tui = importlib.import_module("tui")
    app = importlib.import_module("tui.app")

    assert tui.__file__ is not None
    assert Path(tui.__file__).parts[-2:] == ("tui", "__init__.py")
    assert callable(tui.launch_tui)
    assert app.AvalancheApp.__name__ == "AvalancheApp"


def test_public_provider_contract_keeps_connection_state_optional():
    from tui import ConnectionAwareStateProvider, StateProvider

    assert {
        "list_workflows",
        "list_runs",
        "get_run",
        "start_run",
        "cancel_run",
        "on_run_update",
        "on_log",
    } <= StateProvider.__dict__.keys()
    assert "ping" not in StateProvider.__dict__

    class ConnectedProvider:
        operator_reachable = False
        connection_label = "operator.example:7433"
        last_error = "unavailable"

        def list_workflows(self):
            return []

        def list_runs(self, workflow_selector):
            return []

        def get_run(self, run_id):
            return None

        def start_run(self, workflow_selector, **kwargs):
            return ""

        def cancel_run(self, run_id):
            return None

        def on_run_update(self, callback):
            return None

        def on_log(self, callback):
            return None

        def ping(self):
            return False

    assert isinstance(ConnectedProvider(), ConnectionAwareStateProvider)


def test_packaging_includes_tui_source_root_and_standard_dependency():
    data = tomllib.loads(Path("pyproject.toml").read_text())

    dependencies = data["project"]["dependencies"]
    assert any(dep.startswith("textual") for dep in dependencies)

    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist = data["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "src/tui" in wheel["include"]
    assert "src/tui" in sdist["include"]
    assert wheel["sources"]["src"] == ""


def test_tui_launch_passes_grpc_auth_and_tls_options(monkeypatch, tmp_path):
    tui = importlib.import_module("tui")
    ca_cert = tmp_path / "ca.pem"
    ca_cert.write_bytes(b"ca")
    providers = []
    closed = []

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            providers.append((args, kwargs))

        def close(self):
            closed.append(True)

    class FakeApp:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self):
            return None

    fake_app_module = ModuleType("tui.app")
    fake_app_module.AvalancheApp = FakeApp
    monkeypatch.setitem(sys.modules, "tui.app", fake_app_module)
    operator_client = importlib.import_module("runtime.operator.client")
    monkeypatch.setattr(operator_client, "GrpcStateProvider", FakeProvider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ava tui",
            "--connect",
            "operator.example:443",
            "--token",
            "secret",
            "--tls",
            "--tls-ca-cert",
            str(ca_cert),
        ],
    )

    tui.launch_tui()

    assert providers == [
        (
            ("operator.example:443",),
            {"token": "secret", "tls": True, "root_certificates": b"ca"},
        )
    ]
    assert closed == [True]


def test_tui_launch_accepts_caller_owned_provider(monkeypatch):
    tui = importlib.import_module("tui")
    launched = []

    class InjectedProvider:
        closed = False

        def close(self):
            self.closed = True

    class FakeApp:
        def __init__(self, **kwargs):
            launched.append(kwargs)

        def run(self):
            return None

    fake_app_module = ModuleType("tui.app")
    fake_app_module.AvalancheApp = FakeApp
    monkeypatch.setitem(sys.modules, "tui.app", fake_app_module)
    provider = InjectedProvider()

    tui.launch_tui(["orders/prepare"], provider=provider)

    assert launched == [
        {
            "provider": provider,
            "workflow": "orders",
            "node": "prepare",
            "close_provider_on_unmount": False,
        }
    ]
    assert provider.closed is False


@pytest.mark.parametrize(
    "argv",
    [
        ["--connect", "operator.example:7433"],
        ["--connect=operator.example:7433"],
    ],
)
def test_tui_launch_rejects_injected_provider_with_connect(monkeypatch, argv):
    tui = importlib.import_module("tui")
    fake_app_module = ModuleType("tui.app")
    fake_app_module.AvalancheApp = object
    monkeypatch.setitem(sys.modules, "tui.app", fake_app_module)

    with pytest.raises(
        ValueError, match="an injected provider cannot be combined with --connect"
    ):
        tui.launch_tui(argv, provider=object())


def test_tui_launch_accepts_connect_equals_syntax(monkeypatch):
    tui = importlib.import_module("tui")
    providers = []
    launched = []

    class FakeProvider:
        def __init__(self, address, **kwargs):
            providers.append((self, address, kwargs))

        def close(self):
            return None

    class FakeApp:
        def __init__(self, **kwargs):
            launched.append(kwargs)

        def run(self):
            return None

    fake_app_module = ModuleType("tui.app")
    fake_app_module.AvalancheApp = FakeApp
    monkeypatch.setitem(sys.modules, "tui.app", fake_app_module)
    operator_client = importlib.import_module("runtime.operator.client")
    monkeypatch.setattr(operator_client, "GrpcStateProvider", FakeProvider)

    tui.launch_tui(["--connect=operator.example:7433", "orders/prepare"])

    assert [(address, kwargs) for _, address, kwargs in providers] == [
        ("operator.example:7433", {})
    ]
    assert launched == [
        {
            "provider": providers[0][0],
            "workflow": "orders",
            "node": "prepare",
            "close_provider_on_unmount": False,
        }
    ]


def test_tui_launch_without_provider_preserves_mock_path(monkeypatch):
    tui = importlib.import_module("tui")
    launched = []

    class FakeApp:
        def __init__(self, **kwargs):
            launched.append(kwargs)

        def run(self):
            return None

    fake_app_module = ModuleType("tui.app")
    fake_app_module.AvalancheApp = FakeApp
    monkeypatch.setitem(sys.modules, "tui.app", fake_app_module)

    tui.launch_tui(["orders"])

    assert launched == [
        {
            "provider": None,
            "workflow": "orders",
            "node": None,
            "close_provider_on_unmount": True,
        }
    ]


def test_operator_models_do_not_depend_on_tui_package():
    models = importlib.import_module("runtime.operator.models")

    assert models.WorkflowInfo.__name__ == "WorkflowInfo"
    assert models.RunState.__name__ == "RunState"
