import builtins
import importlib
import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest


def test_tui_implementation_lives_in_optional_source_root():
    tui = importlib.import_module("avalanche.tui")
    app = importlib.import_module("avalanche.tui.app")

    assert tui.__file__ is not None
    assert Path(tui.__file__).parts[-2:] == ("tui", "__init__.py")
    assert callable(tui.launch_tui)
    assert app.AvalancheApp.__name__ == "AvalancheApp"


def test_avalanche_dot_tui_import_delegates_to_tui_source_package():
    compat = importlib.import_module("avalanche.tui")
    source_package = importlib.import_module("tui")

    assert compat.launch_tui is source_package.launch_tui
    assert compat.StateProvider is source_package.StateProvider
    assert compat.ConnectionAwareStateProvider is source_package.ConnectionAwareStateProvider


def test_public_provider_contract_keeps_connection_state_optional():
    from avalanche.tui import ConnectionAwareStateProvider, StateProvider

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
        connected = False
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


def test_avalanche_dot_tui_shim_reports_extra_when_impl_is_missing(monkeypatch):
    shim_path = Path("src/avalanche/tui/__init__.py")
    spec = importlib.util.spec_from_file_location("_avalanche_dot_tui_missing_test", shim_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    real_import_module = importlib.import_module

    def fake_import_module(name: str, package: str | None = None):
        if name == "tui":
            raise ModuleNotFoundError("No module named 'tui'", name="tui")
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)

    with pytest.raises(ModuleNotFoundError, match=r"avalanche-ai\[tui\]"):
        spec.loader.exec_module(module)


def test_tui_launch_reports_extra_when_textual_is_missing(monkeypatch):
    tui = importlib.import_module("avalanche.tui")
    sys.modules.pop("tui.app", None)
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("textual"):
            raise ModuleNotFoundError("No module named 'textual'", name="textual")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ModuleNotFoundError, match=r"avalanche-ai\[tui\]"):
        tui.launch_tui()


def test_packaging_includes_tui_source_root_as_optional_extra():
    data = tomllib.loads(Path("pyproject.toml").read_text())

    optional = data["project"]["optional-dependencies"]
    assert "tui" in optional
    assert any(dep.startswith("textual") for dep in optional["tui"])

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

    class FakeProvider:
        def __init__(self, *args, **kwargs):
            providers.append((args, kwargs))

    class FakeApp:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self):
            return None

    fake_app_module = ModuleType("tui.app")
    fake_app_module.AvalancheApp = FakeApp
    monkeypatch.setitem(sys.modules, "tui.app", fake_app_module)
    operator_client = importlib.import_module("avalanche.operator.client")
    monkeypatch.setattr(operator_client, "GrpcStateProvider", FakeProvider)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "avalanche.tui",
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

    assert launched == [{"provider": provider, "workflow": "orders", "node": "prepare"}]
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

    class FakeApp:
        def __init__(self, **kwargs):
            launched.append(kwargs)

        def run(self):
            return None

    fake_app_module = ModuleType("tui.app")
    fake_app_module.AvalancheApp = FakeApp
    monkeypatch.setitem(sys.modules, "tui.app", fake_app_module)
    operator_client = importlib.import_module("avalanche.operator.client")
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

    assert launched == [{"provider": None, "workflow": "orders", "node": None}]


def test_core_operator_models_do_not_depend_on_optional_tui_package():
    models = importlib.import_module("avalanche.operator.models")

    assert models.WorkflowInfo.__name__ == "WorkflowInfo"
    assert models.RunState.__name__ == "RunState"
