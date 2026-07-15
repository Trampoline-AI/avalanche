"""Process-local lifecycle handle for a workflow run."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import CancelledError, Future
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class RunHandle(Generic[T]):
    """Awaitable, process-local state for one workflow execution."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._future: Future[T] = Future()
        self._cancel_event = threading.Event()
        self._state_lock = threading.Lock()
        self._started = False

    def running(self) -> bool:
        """Return whether the driver thread is active."""
        with self._state_lock:
            return self._started and not self._future.done()

    def done(self) -> bool:
        """Return whether the run has a terminal outcome."""
        return self._future.done()

    def cancel_requested(self) -> bool:
        """Return whether cooperative cancellation has been requested."""
        return self._cancel_event.is_set()

    def cancelled(self) -> bool:
        """Return whether cancellation became the terminal outcome."""
        return self._future.cancelled()

    def cancel(self) -> bool:
        """Request cooperative cancellation.

        The active node or remote task is allowed to finish. The workflow driver
        observes this request before submitting a later node.
        """
        with self._state_lock:
            if self._future.done() or self._cancel_event.is_set():
                return False
            self._cancel_event.set()
            return True

    def result(self, timeout: float | None = None) -> T:
        """Return the cached output, waiting up to ``timeout`` seconds."""
        return self._future.result(timeout=timeout)

    def exception(self, timeout: float | None = None) -> BaseException | None:
        """Return the cached failure, waiting up to ``timeout`` seconds."""
        return self._future.exception(timeout=timeout)

    def __await__(self):
        return self._wait().__await__()

    async def _wait(self) -> T:
        """Wait without blocking the event loop or coupling waiter cancellation."""
        loop = asyncio.get_running_loop()
        completed = loop.create_future()

        def notify_done(_future: Future[T]) -> None:
            try:
                loop.call_soon_threadsafe(_mark_completed)
            except RuntimeError:
                # The waiter's event loop may have closed after it stopped waiting.
                pass

        def _mark_completed() -> None:
            if not completed.done():
                completed.set_result(None)

        self._future.add_done_callback(notify_done)
        await asyncio.shield(completed)
        return self.result()

    def _compose_cancel_requested(
        self, caller_callback: Callable[[], bool] | None
    ) -> Callable[[], bool]:
        def cancel_requested() -> bool:
            caller_requested = bool(caller_callback and caller_callback())
            if caller_requested:
                self._cancel_event.set()
            return caller_requested or self._cancel_event.is_set()

        return cancel_requested

    def _start(self, driver: Callable[[], T]) -> None:
        """Start exactly one workflow driver thread."""

        def execute() -> None:
            try:
                result = driver()
            except CancelledError:
                with self._state_lock:
                    self._future.cancel()
            except BaseException as exc:
                with self._state_lock:
                    self._future.set_exception(exc)
            else:
                with self._state_lock:
                    self._future.set_result(result)

        with self._state_lock:
            if self._started:
                raise RuntimeError("run handle has already been started")
            self._started = True

        try:
            thread = threading.Thread(
                target=execute,
                name=f"avalanche-run-{self.run_id}",
                daemon=False,
            )
            thread.start()
        except BaseException as exc:
            with self._state_lock:
                self._future.set_exception(exc)
