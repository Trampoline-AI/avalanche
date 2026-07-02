"""
Runtime primitives for Avalanche tasks.

Provides Cursor, Stream, and Logger for dependency injection in task parameters.
"""

from .cursor import Cursor
from .providers import Logger, LoggerInstance, Stream, consume_stream

__all__ = [
    "Cursor",
    "Stream",
    "consume_stream",
    "Logger",
    "LoggerInstance",
]
