"""
Runtime primitives for Avalanche tasks.

Provides Cursor, Stream, Logger, and run input/context types.
"""

from .context import (
    MAX_INLINE_FILE_BYTES,
    MAX_INLINE_REQUEST_BYTES,
    BaseContext,
    BaseInput,
    File,
    RunContext,
    S3File,
    get_current_run_context,
    run_with_context,
)
from .cursor import Cursor
from .providers import Logger, LoggerInstance, Stream, consume_stream

__all__ = [
    "BaseContext",
    "BaseInput",
    "Cursor",
    "File",
    "MAX_INLINE_FILE_BYTES",
    "MAX_INLINE_REQUEST_BYTES",
    "RunContext",
    "S3File",
    "get_current_run_context",
    "run_with_context",
    "Stream",
    "consume_stream",
    "Logger",
    "LoggerInstance",
]
