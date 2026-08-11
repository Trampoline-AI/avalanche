"""Shared directory filtering for workflow discovery and source watching."""

from __future__ import annotations

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
