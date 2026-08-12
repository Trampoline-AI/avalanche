"""Shared directory filtering for workflow discovery and source watching."""

from __future__ import annotations

from pathlib import Path

_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)


def is_excluded_directory(name: str) -> bool:
    """Return whether a directory is outside workflow source scope."""
    return name.startswith(".") or name.lower() in _EXCLUDED_DIRECTORY_NAMES


def is_path_in_excluded_directory(relative_path: Path) -> bool:
    """Return whether a relative file path lies under an excluded directory."""
    return any(is_excluded_directory(part) for part in relative_path.parts[:-1])
