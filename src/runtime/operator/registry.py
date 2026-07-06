"""WorkflowRegistry — discovers @workflow functions from Python files."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Callable

from avalanche.dag import Workflow

from .models import WorkflowDiscoveryDiagnostic, WorkflowInfo, display_name_from_id


def workflow_to_info(p: Workflow, file_path: str) -> WorkflowInfo:
    """Convert a Workflow object to a flat WorkflowInfo for the TUI."""
    node_ids = p._topological_sort()
    node_types = {nid: p.nodes[nid].node.node_type.value for nid in node_ids}
    display_names = {nid: display_name_from_id(nid) for nid in node_ids}
    return WorkflowInfo(
        name=p.name,
        file_path=file_path,
        node_ids=node_ids,
        graph=dict(p.graph),
        node_types=node_types,
        display_names=display_names,
        cron=p.cron,
    )


class WorkflowRegistry:
    """Discover and register @workflow-decorated functions from Python files."""

    def __init__(self) -> None:
        self._workflows: dict[str, tuple[Callable[[], Workflow], WorkflowInfo]] = {}
        self._scan_paths: list[str] = []
        self._manual: dict[str, tuple[Callable[[], Workflow], WorkflowInfo]] = {}
        self._diagnostics: list[WorkflowDiscoveryDiagnostic] = []

    def scan(self, paths: list[str]) -> None:
        """Scan files/directories for @workflow-decorated functions.

        For each .py file, imports the module and looks for zero-arg callables
        that return Workflow objects.
        """
        self._scan_paths = list(paths)
        self._workflows = dict(self._manual)  # keep manual registrations
        self._diagnostics = []
        for path_str in paths:
            path = Path(path_str)
            if path.is_file() and path.suffix == ".py":
                self._scan_file(path)
            elif path.is_dir():
                for py_file in sorted(path.rglob("*.py")):
                    if py_file.name.startswith("_"):
                        continue
                    self._scan_file(py_file)

    def rescan(self) -> None:
        """Re-scan previously configured paths, picking up file changes."""
        if self._scan_paths:
            self.scan(self._scan_paths)

    @staticmethod
    def _package_module_name(file_path: Path) -> tuple[str, Path] | None:
        """Resolve a file inside a package to (dotted module name, sys.path root)."""
        parts = [file_path.stem]
        parent = file_path.parent
        while (parent / "__init__.py").exists():
            parts.append(parent.name)
            parent = parent.parent
        if len(parts) == 1:
            return None
        return ".".join(reversed(parts)), parent

    def _scan_file(self, file_path: Path) -> None:
        """Import a Python file and discover @workflow functions in it.

        Files inside a package (a chain of __init__.py up from the file) are
        imported under their canonical dotted name so relative imports work;
        standalone files are loaded under a synthetic module name.
        """
        file_path = file_path.resolve()
        package_info = self._package_module_name(file_path)

        if package_info is not None:
            module_name, root = package_info
            root_str = str(root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)
            # Remove cached module so re-scans pick up file changes
            sys.modules.pop(module_name, None)
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:
                self._diagnostics.append(
                    WorkflowDiscoveryDiagnostic(
                        path=str(file_path),
                        kind="import_error",
                        message=self._format_exception(exc),
                    )
                )
                return
        else:
            module_name = f"_avalanche_discovered_.{file_path.stem}"

            # Remove cached module so re-scans pick up file changes
            sys.modules.pop(module_name, None)

            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                self._add_skipped_diagnostic(
                    file_path,
                    "Python import machinery could not load this file.",
                )
                return

            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                self._diagnostics.append(
                    WorkflowDiscoveryDiagnostic(
                        path=str(file_path),
                        kind="import_error",
                        message=self._format_exception(exc),
                    )
                )
                return
            finally:
                sys.modules.pop(module_name, None)

        found_workflow = False
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            obj = getattr(module, attr_name)
            if not callable(obj):
                continue
            try:
                result = obj()
                if isinstance(result, Workflow):
                    found_workflow = True
                    info = workflow_to_info(result, str(file_path))
                    builder = obj  # zero-arg callable that builds a fresh Workflow
                    self._workflows[result.name] = (builder, info)
            except Exception:
                continue

        if not found_workflow:
            self._add_skipped_diagnostic(file_path, "No workflows discovered in this file.")

    def _add_skipped_diagnostic(self, file_path: Path, message: str) -> None:
        if file_path.name.startswith("_"):
            return
        self._diagnostics.append(
            WorkflowDiscoveryDiagnostic(path=str(file_path), kind="skipped", message=message)
        )

    def _format_exception(self, exc: Exception) -> str:
        message = str(exc)
        if message:
            return f"{type(exc).__name__}: {message}"
        return type(exc).__name__

    def register(self, builder: Callable[[], Workflow], file_path: str = "<manual>") -> None:
        """Manually register a workflow builder function."""
        p = builder()
        info = workflow_to_info(p, file_path)
        self._workflows[p.name] = (builder, info)
        self._manual[p.name] = (builder, info)

    def list_workflows(self) -> list[WorkflowInfo]:
        return [info for _, info in self._workflows.values()]

    def list_diagnostics(self) -> list[WorkflowDiscoveryDiagnostic]:
        return list(self._diagnostics)

    def get_builder(self, name: str) -> Callable[[], Workflow]:
        """Get a callable that builds a fresh Workflow for execution."""
        entry = self._workflows.get(name)
        if entry is None:
            raise KeyError(f"Unknown workflow: {name}")
        return entry[0]
