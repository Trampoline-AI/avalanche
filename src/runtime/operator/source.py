"""Live workflow source resolution and watcher policy."""

from __future__ import annotations

from pathlib import Path

from .discovery import ConfiguredRoot
from .models import WorkflowLocator

_EXCLUDED_DIRS = {
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


def resolve_live_source(
    configured_root: ConfiguredRoot, locator: WorkflowLocator
) -> tuple[Path, str]:
    """Return the normal live import root and workflow path within it."""
    configured_path = configured_root.path.resolve()
    source_file = (configured_path / locator.relative_file).resolve()
    if not source_file.is_relative_to(configured_path) or not source_file.is_file():
        raise FileNotFoundError(f"Workflow source is unavailable: {locator.relative_file}")

    parent = source_file.parent
    top_package: Path | None = None
    while (parent / "__init__.py").is_file():
        top_package = parent
        parent = parent.parent

    if top_package is not None:
        import_root = parent.resolve()
    else:
        import_root = (
            configured_root.target
            if configured_root.target.is_dir()
            else configured_path
        ).resolve()
    return import_root, source_file.relative_to(import_root).as_posix()


def is_source_path_included(
    path: str | Path, source_roots: tuple[str | Path, ...]
) -> bool:
    """Return whether a changed path belongs to watched development source."""
    candidate = Path(path).resolve()
    roots = tuple(Path(root).resolve() for root in source_roots)
    containing_root = next(
        (
            root
            for root in roots
            if candidate == root or candidate.is_relative_to(root)
        ),
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
