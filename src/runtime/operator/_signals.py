"""CLI shutdown interrupts and their nested cleanup boundary."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import FrameType

_cleaning_up: ContextVar[bool] = ContextVar("operator_cleaning_up", default=False)


def request_shutdown(_signum: int, _frame: FrameType | None) -> None:
    if not _cleaning_up.get():
        raise KeyboardInterrupt


@contextmanager
def shutdown_cleanup() -> Iterator[None]:
    """Keep CLI shutdown signals from interrupting resource release."""
    token = _cleaning_up.set(True)
    try:
        yield
    finally:
        _cleaning_up.reset(token)
