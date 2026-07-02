"""Compatibility shim for the optional Avalanche runtime operator package."""

from __future__ import annotations

_OPTIONAL_RUNTIME_DEPS = {"runtime", "croniter", "grpc", "watchfiles"}

try:
    from runtime.operator import (
        Operator,
        WorkflowDiscoveryDiagnostic,
        WorkflowRegistry,
        serve,
        workflow_to_info,
    )
except ModuleNotFoundError as exc:
    if exc.name in _OPTIONAL_RUNTIME_DEPS:
        raise ModuleNotFoundError(
            "avalanche.operator is optional. Install it with `avalanche-ai[runtime]` "
            "or run `uv sync --extra runtime`.",
            name=exc.name,
        ) from exc
    raise

__all__ = [
    "Operator",
    "WorkflowDiscoveryDiagnostic",
    "WorkflowRegistry",
    "workflow_to_info",
    "serve",
]
