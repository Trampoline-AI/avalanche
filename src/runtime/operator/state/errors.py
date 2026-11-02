"""Errors raised by the operator state store."""

from __future__ import annotations

from pathlib import Path


class StateStoreError(Exception):
    """Base class for operator state store failures."""


class StateStoreLockedError(StateStoreError):
    """Another process or connection already owns the state database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"Operator state database {path} is already in use by another process, "
            "such as another operator. Stop it, or pass a different --state-db."
        )


class StateSchemaTooNewError(StateStoreError):
    """The database was written by a newer Avalanche version."""

    def __init__(self, location: str, found: int, supported: int) -> None:
        self.found = found
        self.supported = supported
        super().__init__(
            f"Operator state database {location} has schema version {found}, but this "
            f"Avalanche version supports up to {supported}. Upgrade Avalanche, or pass "
            "a different --state-db."
        )


class StateSchemaMismatchError(StateStoreError):
    """The recorded migration history does not match this code's migrations."""


class StateRowError(StateStoreError):
    """A stored row does not match the typed model its adapter reads it into."""
