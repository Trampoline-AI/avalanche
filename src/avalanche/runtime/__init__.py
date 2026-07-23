"""
Runtime primitives for Avalanche tasks.

Provides Cursor, Stream, Logger, and run input/context types.
"""

from .context import (
    BaseContext,
    BaseInput,
    File,
    Rerun,
    RunContext,
    get_current_run_context,
    run_with_context,
)
from .cursor import Cursor
from .providers import Logger, LoggerInstance, ModelStream, Stream, consume_stream

__all__ = [
    "BaseContext",
    "BaseInput",
    "Cursor",
    "File",
    "Rerun",
    "RunContext",
    "get_current_run_context",
    "run_with_context",
    "Stream",
    "ModelStream",
    "consume_stream",
    "Logger",
    "LoggerInstance",
]
