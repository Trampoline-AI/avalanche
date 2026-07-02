from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_export_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "export_release_tree.py"
    spec = importlib.util.spec_from_file_location("export_release_tree", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


export_module = _load_export_module()
INCLUDE_PATHS = export_module.INCLUDE_PATHS
export_release_tree = export_module.export_release_tree
check_release_surface = export_module.check_release_surface


def test_release_tree_export_copies_allowlisted_paths(tmp_path):
    dest = tmp_path / "release-tree"

    files = export_release_tree(dest)

    for rel in INCLUDE_PATHS:
        assert (dest / rel).exists(), rel
    assert (dest / ".release-tree-files.txt").exists()
    assert "README.md" in {path.as_posix() for path in files}
    assert (dest / ".release-tree-files.txt").read_text().splitlines() == [
        path.as_posix() for path in files
    ]


def test_release_tree_export_excludes_local_and_historical_paths(tmp_path):
    dest = tmp_path / "release-tree"

    export_release_tree(dest)

    assert not (dest / "tasks").exists()
    assert not (dest / ".hermes").exists()
    assert not (dest / "dist").exists()
    assert not list(dest.rglob("__pycache__"))
    assert not list(dest.rglob("*.pyc"))


def test_release_tree_export_has_no_old_surface_terms(tmp_path):
    dest = tmp_path / "release-tree"

    export_release_tree(dest)

    assert check_release_surface(dest) == []
