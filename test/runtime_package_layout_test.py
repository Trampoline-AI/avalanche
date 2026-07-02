import importlib
import tomllib
from pathlib import Path


def test_runtime_implementation_lives_in_optional_source_root():
    executor = importlib.import_module("runtime.executor")
    operator_models = importlib.import_module("runtime.operator.models")

    assert executor.__file__ is not None
    assert Path(executor.__file__).parts[-2:] == ("runtime", "executor.py")
    assert operator_models.__file__ is not None
    assert Path(operator_models.__file__).parts[-3:] == (
        "runtime",
        "operator",
        "models.py",
    )


def test_avalanche_runtime_compat_imports_delegate_to_runtime_source_package():
    avalanche_executor = importlib.import_module("avalanche.executor")
    runtime_executor = importlib.import_module("runtime.executor")
    avalanche_operator_models = importlib.import_module("avalanche.operator.models")
    runtime_operator_models = importlib.import_module("runtime.operator.models")

    assert avalanche_executor.LocalExecutor is runtime_executor.LocalExecutor
    assert avalanche_operator_models.RunState is runtime_operator_models.RunState


def test_existing_core_runtime_primitives_remain_on_avalanche_runtime():
    core_runtime = importlib.import_module("avalanche.runtime")

    assert core_runtime.Cursor.__name__ == "Cursor"
    assert core_runtime.Stream.__name__ == "Stream"


def test_packaging_includes_runtime_source_root_as_optional_extra():
    data = tomllib.loads(Path("pyproject.toml").read_text())

    optional = data["project"]["optional-dependencies"]
    assert "runtime" in optional
    assert "tui" in optional
    assert "all" in optional
    assert any(dep.startswith("grpcio") for dep in optional["runtime"])
    assert any(dep.startswith("watchfiles") for dep in optional["runtime"])
    assert any(dep.startswith("textual") for dep in optional["tui"])

    core_deps = data["project"]["dependencies"]
    assert not any(dep.startswith("grpcio") for dep in core_deps)
    assert not any(dep.startswith("watchfiles") for dep in core_deps)
    assert not any(dep.startswith("croniter") for dep in core_deps)

    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist = data["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "src/runtime" in wheel["include"]
    assert "src/runtime" in sdist["include"]
    assert wheel["sources"]["src"] == ""
