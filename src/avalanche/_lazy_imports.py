"""Deferred imports for heavy optional-at-runtime dependencies.

Polars and PyArrow are hard dependencies of Avalanche, but most of the public
API only needs them when a caller actually hands in or asks for a dataframe.
Importing them eagerly costs ~0.3 s and starts native worker threads (OpenBLAS,
jemalloc, the Polars pool), which matters for short-lived node processes and
rules out forking a warmed-up interpreter.

``lazy_module(name)`` returns a proxy that imports ``name`` on first attribute
access, so ``pl.DataFrame`` / ``pa.schema(...)`` keep working unchanged in
function bodies while nothing is imported until one of them runs. Pair it with
``from __future__ import annotations`` and a ``TYPE_CHECKING`` import so type
checkers still see the real modules.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any


class _LazyModule:
    __slots__ = ("_module", "_name")

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_module", None)

    def _load(self) -> ModuleType:
        module = object.__getattribute__(self, "_module")
        if module is None:
            module = importlib.import_module(object.__getattribute__(self, "_name"))
            object.__setattr__(self, "_module", module)
        return module

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._load(), attr)

    def __repr__(self) -> str:
        name = object.__getattribute__(self, "_name")
        state = "loaded" if object.__getattribute__(self, "_module") is not None else "unloaded"
        return f"<lazy module {name!r} ({state})>"


def lazy_module(name: str) -> Any:
    """Return a proxy that imports ``name`` on first attribute access."""
    return _LazyModule(name)
