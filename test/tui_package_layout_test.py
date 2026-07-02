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
            "delta.example:443",
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
            ("delta.example:443",),
            {"token": "secret", "tls": True, "root_certificates": b"ca"},
        )
    ]


def test_core_operator_models_do_not_depend_on_optional_tui_package():
    models = importlib.import_module("avalanche.operator.models")

    assert models.WorkflowInfo.__name__ == "WorkflowInfo"
    assert models.RunState.__name__ == "RunState"
