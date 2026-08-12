import importlib
import tomllib
from pathlib import Path


def test_runtime_implementation_lives_in_source_root():
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


def test_root_executor_exports_delegate_to_runtime_source_package():
    import avalanche as ava

    runtime_executor = importlib.import_module("runtime.executor")

    assert ava.LocalExecutor is runtime_executor.LocalExecutor
    assert ava.RayExecutor is runtime_executor.RayExecutor


def test_public_runtime_primitives_remain_on_avalanche_runtime():
    core_runtime = importlib.import_module("avalanche.runtime")

    assert core_runtime.Cursor.__name__ == "Cursor"
    assert core_runtime.Stream.__name__ == "Stream"


def test_packaging_includes_standard_runtime_dependencies():
    data = tomllib.loads(Path("pyproject.toml").read_text())

    dependencies = data["project"]["dependencies"]
    for name in ("croniter", "grpcio", "predict-rlm", "protobuf", "textual", "watchfiles"):
        assert any(dep.startswith(name) for dep in dependencies)

    optional = data["project"]["optional-dependencies"]
    assert set(optional) == {"lance", "ray"}

    dev_dependencies = data["dependency-groups"]["dev"]
    assert any(dep.startswith("grpcio-tools") for dep in dev_dependencies)
    assert not any(dep.startswith("grpcio-tools") for dep in dependencies)

    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist = data["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "src/runtime" in wheel["include"]
    assert "src/runtime" in sdist["include"]
    assert wheel["sources"]["src"] == ""
