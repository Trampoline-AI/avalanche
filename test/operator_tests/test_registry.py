"""Tests for WorkflowRegistry — workflow discovery from Python files."""

import importlib
import os
import sys

import pytest

import avalanche as ava
from avalanche.dag import Workflow
from avalanche.operator.models import WorkflowDiscoveryDiagnostic
from avalanche.operator.registry import WorkflowRegistry, workflow_to_info

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")


class TestWorkflowRegistry:
    def test_scan_discovers_workflows_from_file(self):
        registry = WorkflowRegistry()
        registry.scan([os.path.join(FIXTURES_DIR, "sample_workflows.py")])
        names = [p.name for p in registry.list_workflows()]
        assert "simple_workflow" in names
        assert "slow_workflow" in names

    def test_scan_discovers_workflows_from_directory(self):
        registry = WorkflowRegistry()
        registry.scan([FIXTURES_DIR])
        names = [p.name for p in registry.list_workflows()]
        assert "simple_workflow" in names

    def test_workflow_info_has_correct_structure(self):
        registry = WorkflowRegistry()
        registry.scan([os.path.join(FIXTURES_DIR, "sample_workflows.py")])
        info = next(p for p in registry.list_workflows() if p.name == "simple_workflow")

        assert len(info.node_ids) == 3
        assert "source" in info.node_types.values()
        assert "step" in info.node_types.values()
        assert "dest" in info.node_types.values()
        assert info.file_path.endswith("sample_workflows.py")

    def test_get_builder_returns_callable(self):
        registry = WorkflowRegistry()
        registry.scan([os.path.join(FIXTURES_DIR, "sample_workflows.py")])
        builder = registry.get_builder("simple_workflow")
        p = builder()
        assert isinstance(p, Workflow)

    def test_get_builder_unknown_raises(self):
        registry = WorkflowRegistry()
        with pytest.raises(KeyError, match="Unknown workflow"):
            registry.get_builder("nonexistent")

    def test_register_manual_workflow(self):
        from avalanche import source, step, workflow

        @source
        def a():
            return 1

        @step
        def b(x):
            return x + 1

        @workflow
        def manual():
            a() >> b()

        registry = WorkflowRegistry()
        registry.register(manual)
        assert "manual" in [p.name for p in registry.list_workflows()]

    def test_scan_skips_private_files(self):
        """Files starting with _ should be skipped."""
        registry = WorkflowRegistry()
        # __init__.py in fixtures (if it existed) should be skipped
        registry.scan([FIXTURES_DIR])
        # Should still find our workflows (non-underscore files)
        assert len(registry.list_workflows()) >= 1

    def test_scan_records_import_error_diagnostic(self, tmp_path):
        workflow_file = tmp_path / "broken_workflow.py"
        workflow_file.write_text('raise RuntimeError("broken import")\n')

        registry = WorkflowRegistry()
        registry.scan([str(workflow_file)])

        assert registry.list_workflows() == []
        assert registry.list_diagnostics() == [
            WorkflowDiscoveryDiagnostic(
                path=str(workflow_file),
                kind="import_error",
                message="RuntimeError: broken import",
            )
        ]

    def test_scan_discovers_workflow_in_package_with_relative_imports(self, tmp_path):
        pkg = tmp_path / "my_pkg" / "my_belt"
        pkg.mkdir(parents=True)
        (tmp_path / "my_pkg" / "__init__.py").write_text("")
        (pkg / "__init__.py").write_text("")
        (pkg / "helpers.py").write_text("VALUE = 41\n")
        (pkg / "flow.py").write_text(
            "import avalanche as ava\n"
            "from .helpers import VALUE\n"
            "\n"
            "@ava.source\n"
            "def load():\n"
            "    return VALUE + 1\n"
            "\n"
            "@ava.workflow\n"
            "def package_workflow():\n"
            "    return load()\n"
        )

        registry = WorkflowRegistry()
        registry.scan([str(pkg / "flow.py")])

        assert [w.name for w in registry.list_workflows()] == ["package_workflow"]
        assert registry.list_diagnostics() == []

    def test_scan_records_skipped_diagnostic_for_file_with_no_workflows(self, tmp_path):
        no_workflow_file = tmp_path / "no_workflows.py"
        no_workflow_file.write_text("VALUE = 1\n")
        ignored_file = tmp_path / "_ignored.py"
        ignored_file.write_text("VALUE = 1\n")

        registry = WorkflowRegistry()
        registry.scan([str(tmp_path)])

        assert registry.list_workflows() == []
        assert registry.list_diagnostics() == [
            WorkflowDiscoveryDiagnostic(
                path=str(no_workflow_file),
                kind="skipped",
                message="No workflows discovered in this file.",
            )
        ]

    def test_sequential_package_scans_isolate_identical_module_names(self, tmp_path):
        first = tmp_path / "first"
        second = tmp_path / "second"
        _write_deployment(first, field_name="first_value", deployment="first")
        _write_deployment(second, field_name="second_value", deployment="second")

        first_registry = WorkflowRegistry()
        first_registry.scan([str(first / "shared" / "flow.py")])
        first_workflow = first_registry.get_builder("deployment_workflow")()
        first_output = first_workflow.run(
            executor=ava.LocalExecutor(),
            input={"first_value": 1},
            run_id="run_first",
        ).result()

        second_registry = WorkflowRegistry()
        second_registry.scan([str(second / "shared" / "flow.py")])
        second_workflow = second_registry.get_builder("deployment_workflow")()
        second_output = second_workflow.run(
            executor=ava.LocalExecutor(),
            input={"second_value": "two"},
            run_id="run_second",
        ).result()

        assert first_output == {"deployment": "first", "value": 1}
        assert second_output == {"deployment": "second", "value": "two"}

    def test_package_scan_isolates_and_restores_preloaded_package(self, tmp_path, monkeypatch):
        package_name = "registry_shared"
        first = tmp_path / "first"
        second = tmp_path / "second"
        _write_deployment(
            first,
            field_name="first_value",
            deployment="first",
            package_name=package_name,
        )
        _write_deployment(
            second,
            field_name="second_value",
            deployment="second",
            package_name=package_name,
        )
        preloaded = _preload_deployment(monkeypatch, first, package_name)
        original_path = list(sys.path)

        registry = WorkflowRegistry()
        registry.scan([str(second / package_name / "flow.py")])
        workflow = registry.get_builder("deployment_workflow")()
        output = workflow.run(
            executor=ava.LocalExecutor(),
            input={"second_value": "two"},
            run_id="run_second",
        ).result()

        assert output == {"deployment": "second", "value": "two"}
        assert registry.list_diagnostics() == []
        assert sys.path == original_path
        assert all(sys.modules[name] is module for name, module in preloaded.items())

    def test_package_scan_restores_preloaded_package_after_import_error(
        self, tmp_path, monkeypatch
    ):
        package_name = "registry_broken_shared"
        first = tmp_path / "first"
        second = tmp_path / "second"
        _write_deployment(
            first,
            field_name="first_value",
            deployment="first",
            package_name=package_name,
        )
        _write_deployment(
            second,
            field_name="second_value",
            deployment="second",
            package_name=package_name,
        )
        workflow_file = second / package_name / "flow.py"
        workflow_file.write_text(
            "from .schema import DeploymentInput\n\n"
            'raise RuntimeError("broken deployment import")\n'
        )
        preloaded = _preload_deployment(monkeypatch, first, package_name)
        original_path = list(sys.path)

        registry = WorkflowRegistry()
        registry.scan([str(workflow_file)])

        assert registry.list_workflows() == []
        assert registry.list_diagnostics() == [
            WorkflowDiscoveryDiagnostic(
                path=str(workflow_file),
                kind="import_error",
                message="RuntimeError: broken deployment import",
            )
        ]
        assert sys.path == original_path
        assert all(sys.modules[name] is module for name, module in preloaded.items())


class TestWorkflowToInfo:
    def test_converts_workflow_to_info(self):
        from avalanche import dest, source, step, workflow

        @source
        def load():
            pass

        @step
        def proc():
            pass

        @dest
        def save():
            pass

        @workflow
        def test_pipe():
            load() >> proc() >> save()

        p = test_pipe()
        info = workflow_to_info(p, "/some/path.py")

        assert info.name == "test_pipe"
        assert info.file_path == "/some/path.py"
        assert len(info.node_ids) == 3
        assert all(nid in info.node_types for nid in info.node_ids)
        assert all(nid in info.display_names for nid in info.node_ids)


def _write_deployment(root, *, field_name, deployment, package_name="shared"):
    package = root / package_name
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    annotation = "int" if field_name == "first_value" else "str"
    (package / "schema.py").write_text(
        "from avalanche import BaseInput\n\n"
        "class DeploymentInput(BaseInput):\n"
        f"    {field_name}: {annotation}\n"
    )
    (package / "flow.py").write_text(
        "from avalanche import source, workflow\n"
        "from .schema import DeploymentInput\n\n"
        "@source\n"
        "def load(inputs: DeploymentInput):\n"
        f"    return {{'deployment': '{deployment}', 'value': inputs.{field_name}}}\n\n"
        "@workflow(input=DeploymentInput)\n"
        "def deployment_workflow():\n"
        "    return load()\n"
    )


def _preload_deployment(monkeypatch, root, package_name):
    module_names = [
        package_name,
        f"{package_name}.schema",
        f"{package_name}.flow",
    ]
    for name in reversed(module_names):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(root))
    return {name: importlib.import_module(name) for name in module_names}
