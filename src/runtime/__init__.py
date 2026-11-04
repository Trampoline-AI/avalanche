"""Avalanche runtime implementation.

Contains the control plane (`operator`) and execution plane (`executor`).
"""

from .executor import Executor, LocalExecutor, get_default_executor

__all__ = [
    "Executor",
    "LocalExecutor",
    "get_default_executor",
]
