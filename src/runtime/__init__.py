"""Avalanche runtime implementation.

Contains the control plane (`operator`) and execution plane (`executor`).
"""

from .executor import Executor, LocalExecutor, RayExecutor, get_default_executor

__all__ = [
    "Executor",
    "LocalExecutor",
    "RayExecutor",
    "get_default_executor",
]
