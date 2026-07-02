"""Optional Avalanche runtime implementation.

Contains the control plane (`operator`) and execution plane (`executor`). Public
compatibility imports live under `avalanche.operator` and `avalanche.executor`.
"""

from .executor import Executor, LocalExecutor, RayExecutor, get_default_executor

__all__ = [
    "Executor",
    "LocalExecutor",
    "RayExecutor",
    "get_default_executor",
]
