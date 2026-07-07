"""
Logger provider for structured logging with automatic context injection.

Implements the ParameterProvider abstract base class for dependency injection of loggers
with automatic metadata like node names, execution IDs, and worker information.

Usage:
    @ava.step
    def my_func(data, *, logger=Logger()):
        logger.info(f"Processing {len(data)} items")
        return process(data)

The framework automatically:
- Detects Logger() markers in default parameter values
- Resolves them to LoggerInstance with context (node name, execution ID, worker ID)
- Injects the resolved logger into the function call
"""

import logging
from typing import Any

from avalanche.types import ParamContext, ParameterProvider

# Get module logger
_module_logger = logging.getLogger(__name__)


class Logger(ParameterProvider):
    """
    Logger provider for structured logging with automatic context.

    Logger() is a provider marker (like Stream, Config) that tells the
    framework to inject a logger with automatic context metadata.

    The framework injects:
    - Node name (which function is logging)
    - Execution ID (unique ID for this workflow run)
    - Worker ID (for distributed execution with Ray)
    - Timestamp and log level

    Example:
        @ava.step
        def process_data(data, *, logger=ava.Logger()):
            logger.info(f"Processing {len(data)} rows")
            # Logs: [node=process_data] Processing 100 rows

            try:
                result = step(data)
                logger.debug("Transformation successful")
                return result
            except Exception as e:
                logger.error(f"Step failed: {e}", exc_info=True)
                raise

    Attributes:
        name: Optional logger name (defaults to node name)
        level: Optional log level override
    """

    name: str | None
    """Optional logger name (defaults to node name)."""

    level: int | None
    """Optional log level override (DEBUG, INFO, WARNING, ERROR, CRITICAL)."""

    def __init__(self, name: str | None = None, level: int | None = None):
        """
        Initialize a logger provider marker.

        Args:
            name: Optional logger name (defaults to node name)
            level: Optional log level override (e.g., logging.DEBUG)

        Example:
            Logger()  # Default logger with node context
            Logger(name="my_workflow.processing")  # Custom logger name
            Logger(level=logging.DEBUG)  # Override log level
        """
        self.name = name
        self.level = level

    def __repr__(self) -> str:
        return f"Logger(name={self.name!r}, level={self.level})"

    # ParameterProvider protocol implementation
    @classmethod
    def can_resolve(cls, param_value: Any) -> bool:
        """Check if this provider can handle the parameter."""
        return isinstance(param_value, Logger)

    @classmethod
    def resolve(cls, param_value: Any, param_context: ParamContext) -> Any:
        """Resolve Logger marker to a LoggerInstance with execution context."""
        logger_marker = param_value
        node_name = param_context.node_name

        logger_name = logger_marker.name or f"avalanche.node.{node_name}"
        base_logger = logging.getLogger(logger_name)

        if logger_marker.level is not None:
            base_logger.setLevel(logger_marker.level)

        if param_context.executor_type == "ray":
            import ray

            worker_id = ray.get_runtime_context().get_worker_id()
        else:
            worker_id = "local"

        context = {
            "node": node_name,
            "run_id": param_context.run_id,
            "worker_id": worker_id,
        }

        return LoggerInstance(base_logger, context)


class LoggerInstance:
    """Injected logger that wraps stdlib logger with context metadata."""

    def __init__(self, logger: logging.Logger, context: dict[str, Any]):
        self._logger = logger
        self._context = context

    def _format_message(self, msg: str) -> str:
        context_parts = []
        if self._context.get("node"):
            context_parts.append(f"node={self._context['node']}")
        if self._context.get("run_id"):
            context_parts.append(f"run={self._context['run_id'][:8]}")
        if self._context.get("worker_id"):
            worker_id = self._context["worker_id"]
            if worker_id != "local" and len(worker_id) > 8:
                worker_id = worker_id[:8]
            context_parts.append(f"worker={worker_id}")

        if context_parts:
            prefix = "[" + ", ".join(context_parts) + "]"
            return f"{prefix} {msg}"
        return msg

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._logger.debug(self._format_message(msg), **kwargs)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._logger.info(self._format_message(msg), **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._logger.warning(self._format_message(msg), **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._logger.error(self._format_message(msg), **kwargs)

    def critical(self, msg: str, **kwargs: Any) -> None:
        self._logger.critical(self._format_message(msg), **kwargs)

    def exception(self, msg: str, **kwargs: Any) -> None:
        self._logger.exception(self._format_message(msg), **kwargs)

    warn = warning
