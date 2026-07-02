"""
Parameter providers for dependency injection.

This module contains all parameter providers that implement the ParameterProvider
protocol. Providers can be registered in the DAG for automatic resolution.
"""

from .logger import Logger, LoggerInstance
from .stream import Stream, consume_stream

__all__ = [
    "Stream",
    "consume_stream",
    "Logger",
    "LoggerInstance",
]

# Provider registry for DAG execution
# Add new providers here as they're implemented
PROVIDERS = [
    Stream,
    Logger,
]
