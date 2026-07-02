"""Compatibility shim for the optional Avalanche TUI package."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import NoReturn

_INSTALL_MESSAGE = (
    "avalanche.tui is optional. Install it with `avalanche-ai[tui]` "
    "or run `uv sync --extra tui`."
)


def _raise_missing_tui(exc: ModuleNotFoundError) -> NoReturn:
    raise ModuleNotFoundError(_INSTALL_MESSAGE, name="tui") from exc


def _load_impl() -> ModuleType:
    try:
        return importlib.import_module("tui")
    except ModuleNotFoundError as exc:
        if exc.name in {"tui", "textual"}:
            _raise_missing_tui(exc)
        raise


_impl = _load_impl()
sys.modules[__name__] = _impl
