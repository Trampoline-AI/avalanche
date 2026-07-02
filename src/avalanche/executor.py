"""Compatibility shim for the optional Avalanche runtime executor package."""

from __future__ import annotations

try:
    from runtime.executor import Executor, LocalExecutor, RayExecutor, get_default_executor
except ModuleNotFoundError as exc:
    if exc.name == "runtime":
        raise ModuleNotFoundError(
            "avalanche.executor is optional. Install it with `avalanche-ai[runtime]` "
            "or run `uv sync --extra runtime`.",
            name="runtime",
        ) from exc
    raise

__all__ = [
    "Executor",
    "LocalExecutor",
    "RayExecutor",
    "get_default_executor",
]
