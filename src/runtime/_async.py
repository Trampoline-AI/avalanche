"""Helpers for executing synchronous or asynchronous callables from sync APIs."""

from __future__ import annotations

import asyncio
import inspect
import threading
from contextvars import copy_context
from typing import Any, Awaitable, Callable


async def _await_value(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


def _run_awaitable_in_thread(awaitable: Awaitable[Any]) -> Any:
    """Run an awaitable in a new thread when this thread already owns a loop."""
    context = copy_context()
    result: list[Any] = []
    error: list[BaseException] = []

    def target() -> None:
        try:
            result.append(context.run(asyncio.run, _await_value(awaitable)))
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller thread
            error.append(exc)

    thread = threading.Thread(target=target, name="avalanche-async-runner")
    thread.start()
    thread.join()

    if error:
        raise error[0]
    return result[0] if result else None


def resolve_awaitable(value: Any) -> Any:
    """Return an awaitable's resolved value, or the value unchanged."""
    if not inspect.isawaitable(value):
        return value

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await_value(value))
    return _run_awaitable_in_thread(value)


def call_sync_or_async(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a function and synchronously resolve async results."""
    return resolve_awaitable(fn(*args, **kwargs))
