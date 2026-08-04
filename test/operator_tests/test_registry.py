"""Tests for WorkflowRegistry — workflow discovery from Python files."""

import importlib
import json
import os
import signal
import sys
import time

import pytest

import avalanche as ava
from avalanche.dag import Workflow
from avalanche.operator.models import WorkflowDiscoveryDiagnostic
from avalanche.operator.registry import (
    AmbiguousWorkflow,
    WorkflowRegistry,
    workflow_to_info,
)
from runtime.operator.discovery import configure_roots
from runtime.operator.registry import (
    agent_field_schemas_for_workflow,
    agent_instruction_lines_for_workflow,
)

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
        assert registry.list_workflows()[0].workflow_id == "flow.py::package_workflow"
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

    def test_path_qualified_ids_allow_duplicate_short_names(self, tmp_path):
        left = tmp_path / "left"
        right = tmp_path / "right"
        left.mkdir()
        right.mkdir()
        source = (
            "import avalanche as ava\n" "@ava.workflow\n" "def shared():\n" "    return None\n"
        )
        (left / "flow.py").write_text(source)
        (right / "flow.py").write_text(source)

        registry = WorkflowRegistry()
        registry.scan([str(left), str(right)])

        ids = [workflow.workflow_id for workflow in registry.list_workflows()]
        assert ids == ["left/flow.py::shared", "right/flow.py::shared"]
        assert registry.resolve(ids[0]).workflow_id == ids[0]
        with pytest.raises(AmbiguousWorkflow) as exc_info:
            registry.resolve("shared")
        assert exc_info.value.candidate_ids == tuple(ids)

    def test_unique_short_name_resolves_and_single_root_id_is_relative(self, tmp_path):
        workflow_file = tmp_path / "nested" / "flow.py"
        workflow_file.parent.mkdir()
        workflow_file.write_text(
            "import avalanche as ava\n"
            "@ava.workflow\n"
            "def only_here():\n"
            "    return None\n"
        )

        registry = WorkflowRegistry()
        registry.scan([str(tmp_path)])

        descriptor = registry.resolve("only_here")
        assert descriptor.workflow_id == "nested/flow.py::only_here"
        assert str(tmp_path) not in descriptor.workflow_id

    def test_root_aliases_and_ids_are_deterministic_across_input_order(self, tmp_path):
        roots = [tmp_path / "zeta", tmp_path / "alpha"]
        for root in roots:
            root.mkdir()
            (root / "flow.py").write_text(
                "import avalanche as ava\n"
                "@ava.workflow\n"
                "def build():\n"
                "    return None\n"
            )

        first = WorkflowRegistry()
        first.scan([str(path) for path in roots])
        second = WorkflowRegistry()
        second.scan([str(path) for path in reversed(roots)])

        assert set(first.view.by_id) == set(second.view.by_id)

    def test_only_local_marked_builders_are_called(self, tmp_path):
        (tmp_path / "definitions.py").write_text(
            "import avalanche as ava\n"
            "@ava.workflow\n"
            "def local_flow():\n"
            "    return None\n"
        )
        (tmp_path / "consumer.py").write_text(
            "from definitions import local_flow\n"
            "def arbitrary_public_callable():\n"
            "    raise AssertionError('discovery called an arbitrary function')\n"
        )

        registry = WorkflowRegistry()
        registry.scan([str(tmp_path)])

        assert [item.workflow_id for item in registry.descriptors()] == [
            "definitions.py::local_flow"
        ]

    def test_discovery_subprocess_does_not_contaminate_parent_modules(self, tmp_path):
        module_name = "avalanche_discovery_isolation_probe"
        (tmp_path / f"{module_name}.py").write_text("VALUE = 42\n")
        (tmp_path / "flow.py").write_text(
            "import avalanche as ava\n"
            f"from {module_name} import VALUE\n"
            "@ava.workflow\n"
            "def isolated():\n"
            "    assert VALUE == 42\n"
        )
        sys.modules.pop(module_name, None)

        registry = WorkflowRegistry()
        registry.scan([str(tmp_path / "flow.py")])

        assert registry.resolve("isolated").workflow_id == "flow.py::isolated"
        assert module_name not in sys.modules

    def test_two_roots_with_same_package_are_discovered_and_runnable_independently(
        self, tmp_path
    ):
        roots = []
        for alias, cron in (("left", "1 * * * *"), ("right", "2 * * * *")):
            root = tmp_path / alias
            package = root / "pkg"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "metadata.py").write_text(f"CRON = {cron!r}\n")
            (package / "flow.py").write_text(
                "import avalanche as ava\n"
                "from pkg.metadata import CRON\n"
                f"@ava.source\ndef {alias}_node():\n    return {alias!r}\n"
                f"@ava.workflow(cron=CRON)\ndef {alias}_build():\n"
                f"    return {alias}_node()\n"
            )
            roots.append(f"{alias}={root}")

        registry = WorkflowRegistry()
        registry.scan(roots)

        descriptors = registry.descriptors()
        assert [(item.display_name, item.cron) for item in descriptors] == [
            ("left_build", "1 * * * *"),
            ("right_build", "2 * * * *"),
        ]
        assert [item.locator.builder_symbol for item in descriptors] == [
            "left_build",
            "right_build",
        ]
        assert registry.get_builder(descriptors[0].workflow_id)().name == "left_build"
        assert registry.get_builder(descriptors[1].workflow_id)().name == "right_build"

    def test_rescan_of_unchanged_catalog_preserves_revision_and_view(self, tmp_path):
        workflow_file = tmp_path / "flow.py"
        workflow_file.write_text(
            "import avalanche as ava\n"
            "@ava.workflow\n"
            "def unchanged():\n"
            "    return None\n"
        )
        registry = WorkflowRegistry()
        initial = registry.scan([str(workflow_file)])

        rescanned = registry.rescan()

        assert rescanned is initial
        assert rescanned.revision == initial.revision

    def test_refresh_invalid_file_retains_current_descriptor(self, tmp_path):
        workflow_file = tmp_path / "flow.py"
        workflow_file.write_text(
            "import avalanche as ava\n"
            "@ava.workflow(cron='* * * * *')\n"
            "def scheduled():\n"
            "    return None\n"
        )
        registry = WorkflowRegistry()
        registry.scan([str(workflow_file)])
        assert [item.workflow_id for item in registry.descriptors()] == ["flow.py::scheduled"]

        workflow_file.write_text("this is not valid Python !!!\n")
        registry.rescan()

        assert [item.workflow_id for item in registry.descriptors()] == ["flow.py::scheduled"]
        assert registry.list_diagnostics()[0].kind == "import_error"

    def test_discovery_timeout_retains_current_view(self, tmp_path):
        workflow_file = tmp_path / "flow.py"
        workflow_file.write_text(
            "import avalanche as ava\n"
            "@ava.workflow\n"
            "def scheduled():\n"
            "    return None\n"
        )
        registry = WorkflowRegistry(discovery_timeout=5.0)
        registry.scan([str(workflow_file)])
        assert registry.descriptors()

        workflow_file.write_text("while True:\n    pass\n")
        registry._discovery_timeout = 0.2
        started = time.monotonic()
        registry.rescan()

        assert time.monotonic() - started < 2.0
        assert [item.workflow_id for item in registry.descriptors()] == ["flow.py::scheduled"]
        assert "exceeded 0.2s" in registry.list_diagnostics()[0].message

    def test_discovery_stdout_and_delayed_background_output_do_not_corrupt_result(
        self, tmp_path
    ):
        workflow_file = tmp_path / "flow.py"
        workflow_file.write_text(
            "import subprocess, sys\n"
            "import avalanche as ava\n"
            "print('import noise')\n"
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(0.1); print(\\\"late noise\\\")'])\n"
            "@ava.workflow\n"
            "def noisy():\n"
            "    print('builder noise')\n"
        )

        registry = WorkflowRegistry(discovery_timeout=10.0)
        registry.scan([str(workflow_file)])

        assert [item.workflow_id for item in registry.descriptors()] == ["flow.py::noisy"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process-group assertion")
    def test_successful_discovery_terminates_import_spawned_descendant(self, tmp_path):
        pid_file = tmp_path / "child.pid"
        workflow_file = tmp_path / "flow.py"
        workflow_file.write_text(
            "import subprocess, sys\n"
            "from pathlib import Path\n"
            "import avalanche as ava\n"
            f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
            "@ava.workflow\n"
            "def spawned():\n"
            "    return None\n"
        )

        registry = WorkflowRegistry(discovery_timeout=10.0)
        registry.scan([str(workflow_file)])

        assert registry.resolve("spawned")
        child_pid = int(pid_file.read_text())
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            os.kill(child_pid, signal.SIGKILL)
            raise AssertionError("discovery descendant remained alive")

    def test_repeated_target_and_implicit_basename_collisions_are_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(ValueError, match="Repeated configured"):
            configure_roots([str(root), str(root)])

        left = tmp_path / "left" / "flows"
        right = tmp_path / "right" / "flows"
        left.mkdir(parents=True)
        right.mkdir(parents=True)
        with pytest.raises(ValueError, match="explicit stable aliases"):
            configure_roots([str(left), str(right)])

    def test_explicit_alias_ids_are_stable_after_root_relocation(self, tmp_path):
        source = (
            "import avalanche as ava\n" "@ava.workflow\n" "def build():\n" "    return None\n"
        )
        first_root = tmp_path / "checkout-a" / "flows"
        second_root = tmp_path / "checkout-b" / "renamed"
        first_root.mkdir(parents=True)
        second_root.mkdir(parents=True)
        (first_root / "flow.py").write_text(source)
        (second_root / "flow.py").write_text(source)

        first = WorkflowRegistry()
        second = WorkflowRegistry()
        first.scan([f"stable={first_root}"])
        second.scan([f"stable={second_root}"])

        assert tuple(first.view.by_id) == tuple(second.view.by_id)

    def test_duplicate_canonical_ids_publish_invalid_catalog_diagnostic(self, monkeypatch):
        from avalanche.operator.models import WorkflowDescriptor, WorkflowLocator

        descriptor = WorkflowDescriptor(
            workflow_id="flow.py::build",
            display_name="build",
            locator=WorkflowLocator("root", "flow.py", "build"),
            node_ids=(),
            graph=(),
            node_types=(),
            display_names=(),
        )
        monkeypatch.setattr(
            "runtime.operator.registry.discover",
            lambda roots, timeout: ((descriptor, descriptor), ()),
        )
        registry = WorkflowRegistry()
        registry.scan(["root=/tmp"])
        assert registry.descriptors() == ()
        assert [item.kind for item in registry.list_diagnostics()] == ["invalid_catalog"]

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

        assert [item.display_name for item in registry.descriptors()] == ["deployment_workflow"]
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


def test_agent_steps_are_identified_without_changing_node_type():
    class Analyze(ava.Signature):
        text: str = ava.InputField()
        result: str = ava.OutputField()

    @ava.source
    def load() -> str:
        return "input"

    @ava.agent_step(Analyze)
    async def analyze(text: str, *, agent: ava.Agent) -> str:
        return (await agent(text=text)).result

    @ava.workflow
    def agent_flow():
        return analyze(load())

    info = workflow_to_info(agent_flow(), "<test>")
    assert info.agent_node_ids == ["analyze_1"]
    assert info.node_types["analyze_1"] == "step"


def test_agent_metadata_failure_does_not_hide_workflow(monkeypatch):
    class Analyze(ava.Signature):
        """Analyze text."""

        text: str = ava.InputField(desc="text to analyze")
        result: str = ava.OutputField(desc="analysis")

    @ava.agent_step(Analyze, max_iterations=4)
    async def analyze(text: str, *, agent: ava.Agent) -> str:
        return (await agent(text=text)).result

    @ava.workflow(agent_defaults={"max_iterations": 2})
    def agent_flow():
        return analyze("input")

    workflow = agent_flow()
    info = workflow_to_info(workflow, "<test>")
    metadata = json.loads(info.agent_metadata_json["analyze_1"])
    assert metadata["signature"]["name"] == "Analyze"
    assert metadata["runtime"]["max_iterations"] == 4
    field_schemas = json.loads(
        agent_field_schemas_for_workflow(workflow, ["analyze_1"])["analyze_1"]
    )
    assert field_schemas == {
        "inputs": [{"name": "text", "type": "str", "description": "text to analyze"}],
        "outputs": [{"name": "result", "type": "str", "description": "analysis"}],
    }
    assert agent_instruction_lines_for_workflow(workflow, ["analyze_1"]) == {
        "analyze_1": "Analyze text."
    }

    spec = workflow.nodes["analyze_1"].node.fn.__agent_step__

    def fail_metadata(_defaults):
        raise ValueError("invalid metadata")

    monkeypatch.setattr(spec, "declaration_metadata", fail_metadata)
    fallback = workflow_to_info(workflow, "<test>")
    assert fallback.agent_node_ids == ["analyze_1"]
    assert json.loads(fallback.agent_metadata_json["analyze_1"]) == {
        "error": "invalid metadata"
    }
