"""Live workflow source resolution and watcher policy."""

from __future__ import annotations

import os
from pathlib import Path

from .discovery import ConfiguredRoot
from .models import WorkflowLocator

_EXCLUDED_DIRS = {
    ".avalanche",
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
_CREDENTIAL_NAMES = {
    ".npmrc",
    ".netrc",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
}
_CREDENTIAL_SUFFIXES = {
    ".db",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".tfstate",
}


def resolve_import_root(
    configured_root: ConfiguredRoot, source_file: str | Path | None = None
) -> Path:
    """Return the live import root for a configured target or source file."""
    target = configured_root.target.resolve()
    if source_file is None:
        if target.is_dir():
            package_marker = target / "__init__.py"
            if not package_marker.is_file():
                return target
            source = package_marker
        else:
            source = target
    else:
        source = Path(source_file).resolve()

    package = source.parent
    top_package: Path | None = None
    while (package / "__init__.py").is_file():
        top_package = package
        package = package.parent
    if top_package is not None:
        return package.resolve()
    return (target if target.is_dir() else configured_root.path).resolve()


def resolve_watch_roots(
    configured_roots: tuple[ConfiguredRoot, ...],
    locators: tuple[WorkflowLocator, ...],
) -> tuple[Path, ...]:
    """Return deterministic, non-overlapping roots covering live imports."""
    roots_by_alias = {root.alias: root for root in configured_roots}
    resolved = {resolve_import_root(root) for root in configured_roots}
    for locator in locators:
        configured_root = roots_by_alias[locator.root_alias]
        source_file = configured_root.path / locator.relative_file
        resolved.add(resolve_import_root(configured_root, source_file))
    candidates = sorted(
        resolved,
        key=lambda path: (len(path.parts), path.as_posix()),
    )
    roots: list[Path] = []
    for candidate in candidates:
        if any(candidate == root or candidate.is_relative_to(root) for root in roots):
            continue
        roots.append(candidate)
    return tuple(roots)


def iter_source_paths(source_roots: tuple[str | Path, ...]) -> tuple[Path, ...]:
    """Return watched source files while pruning generated and sensitive directories."""
    paths: list[Path] = []
    for source_root in source_roots:
        root = Path(source_root).resolve()
        if not root.is_dir():
            continue
        for directory, subdirectories, file_names in os.walk(root):
            subdirectories[:] = sorted(
                name for name in subdirectories if not _exclude_directory(name)
            )
            paths.extend(
                candidate
                for name in sorted(file_names)
                if (candidate := Path(directory, name)).is_file()
                and not _exclude_file(candidate.name)
            )
    return tuple(sorted(paths))


def resolve_live_source(
    configured_root: ConfiguredRoot, locator: WorkflowLocator
) -> tuple[Path, str]:
    """Return the normal live import root and workflow path within it."""
    configured_path = configured_root.path.resolve()
    source_file = (configured_path / locator.relative_file).resolve()
    if not source_file.is_relative_to(configured_path) or not source_file.is_file():
        raise FileNotFoundError(f"Workflow source is unavailable: {locator.relative_file}")

    import_root = resolve_import_root(configured_root, source_file)
    return import_root, source_file.relative_to(import_root).as_posix()


def is_source_path_included(path: str | Path, source_roots: tuple[str | Path, ...]) -> bool:
    """Return whether a changed path belongs to watched development source."""
    candidate = Path(path).resolve()
    roots = tuple(Path(root).resolve() for root in source_roots)
    containing_root = next(
        (root for root in roots if candidate == root or candidate.is_relative_to(root)),
        None,
    )
    if containing_root is None:
        return False
    relative = candidate.relative_to(containing_root)
    if not relative.parts:
        return False
    if any(_exclude_directory(part) for part in relative.parts[:-1]):
        return False
    return not _exclude_file(relative.name)


def _exclude_directory(name: str) -> bool:
    lowered = name.lower()
    return lowered in _EXCLUDED_DIRS or lowered in {".aws", ".ssh"}


def _exclude_file(name: str) -> bool:
    lowered = name.lower()
    stem = Path(lowered).stem
    return (
        lowered.startswith(".env")
        or lowered.startswith("secrets.")
        or stem == "secrets"
        or lowered.endswith(".tfstate.backup")
        or lowered in _CREDENTIAL_NAMES
        or Path(lowered).suffix in _CREDENTIAL_SUFFIXES
    )
