from pathlib import Path

from runtime.operator.discovery import configure_roots
from runtime.operator.models import WorkflowLocator
from runtime.operator.source import resolve_import_root, resolve_watch_roots


def test_package_file_import_root_is_parent_of_top_package(tmp_path: Path):
    project = tmp_path / "project"
    package = project / "pkg" / "nested"
    package.mkdir(parents=True)
    (project / "pkg" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    workflow = package / "flow.py"
    workflow.write_text("")

    (configured_root,) = configure_roots([str(workflow)])

    assert resolve_import_root(configured_root, workflow) == project


def test_standalone_file_import_root_is_its_parent(tmp_path: Path):
    workflow = tmp_path / "flow.py"
    workflow.write_text("")

    (configured_root,) = configure_roots([str(workflow)])

    assert resolve_import_root(configured_root, workflow) == tmp_path


def test_directory_import_root_preserves_configured_directory(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()

    (configured_root,) = configure_roots([str(source)])

    assert resolve_import_root(configured_root) == source


def test_watch_roots_deduplicate_nested_import_roots(tmp_path: Path):
    project = tmp_path / "project"
    package = project / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    workflow = package / "flow.py"
    workflow.write_text("")

    roots = configure_roots([f"project={project}", f"flow={workflow}"])
    locators = (WorkflowLocator("flow", workflow.name, "flow"),)

    assert resolve_watch_roots(roots, locators) == (project,)
