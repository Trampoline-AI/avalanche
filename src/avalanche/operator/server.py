"""Compatibility shim for runtime.operator.server."""

from __future__ import annotations

_OPTIONAL_RUNTIME_DEPS = {"runtime", "croniter", "grpc", "watchfiles"}

try:
    from runtime.operator.server import *  # noqa: F403
except ModuleNotFoundError as exc:
    if exc.name in _OPTIONAL_RUNTIME_DEPS:
        raise ModuleNotFoundError(
            "avalanche.operator is optional. Install it with `avalanche-ai[runtime]` "
            "or run `uv sync --extra runtime`.",
            name=exc.name,
        ) from exc
    raise
