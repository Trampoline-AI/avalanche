"""Bounded off-thread execution for optional provider-described UI resources."""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from .state import (
    ActionDescriptor,
    ActionResult,
    ExtensionDescription,
    ExtensionPage,
    Invalidation,
    Object,
    PanelDescriptor,
    TuiContext,
    TuiExtensionError,
    TuiExtensions,
    json_plain,
)


class ExtensionSession:
    """At most two calls, no executor backlog, one bounded invalidation handoff.

    Providers own transport deadlines and cancellation. A cancelled Python future
    cannot interrupt a running synchronous transport; its completion is fenced by
    exact context equality. No stale response can repaint a newer selection.
    """

    def __init__(
        self,
        extension: TuiExtensions,
        *,
        read_only: bool,
        post: Callable[[Callable[[], None]], None],
    ) -> None:
        self.extension = extension
        self.read_only = read_only
        self.context = TuiContext(())
        self.description: ExtensionDescription | None = None
        self.error = ""
        self.loading = False
        self._post = post
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tui-extensions")
        self._futures: list[Future] = []
        self._lock = threading.Lock()
        self._notices: deque[Invalidation] = deque(maxlen=64)
        self._overflow = False
        self._closed = False
        self._describe_pending = True
        self.awaiting_baseline = False
        self.changed_resources: frozenset[str] = frozenset()
        self._unsubscribe = extension.subscribe(self._enqueue)

    def _enqueue(self, notice: Invalidation) -> None:
        with self._lock:
            if self._closed:
                return
            if len(self._notices) == self._notices.maxlen:
                self._overflow = True
            self._notices.append(notice)

    def select(self, context: TuiContext) -> None:
        if context == self.context:
            return
        if context.scope != self.context.scope:
            self.awaiting_baseline = False
        self.context = context
        self.description = None
        self.error = ""
        self.loading = False
        self._describe_pending = True
        for future in self._futures:
            future.cancel()

    def _submit(
        self,
        call: Callable[[], ExtensionDescription | ExtensionPage | ActionResult],
        accept: Callable[[ExtensionDescription | ExtensionPage | ActionResult], None],
        fail: Callable[[TuiExtensionError], None],
    ) -> bool:
        for future in self._futures:
            if future.done() and not future.cancelled():
                future.result()  # Surface contract bugs instead of permanent loading.
        self._futures = [future for future in self._futures if not future.done()]
        if self._closed or len(self._futures) >= 2:
            return False
        context = self.context

        def run() -> None:
            try:
                result = call()
                if result.context != context:
                    raise TuiExtensionError(
                        "integrity", "Provider returned a mismatched context"
                    )
            except TuiExtensionError as error:

                def failed(error=error) -> None:
                    if not self._closed and self.context == context:
                        fail(error)

                self._post(failed)
                return

            def completed() -> None:
                if not self._closed and self.context == context:
                    accept(result)

            self._post(completed)

        self._futures.append(self._executor.submit(run))
        return True

    def tick(self) -> bool:
        """Drain hints without network polling. Return whether resources changed."""
        with self._lock:
            notices = tuple(self._notices)
            self._notices.clear()
            overflow, self._overflow = self._overflow, False
        relevant = [notice for notice in notices if notice.context == self.context]
        changed = bool(relevant) or overflow
        self.changed_resources = frozenset(n.resource for n in relevant)
        if changed:
            self._describe_pending = True
            if overflow or any(n.kind == "reset" for n in relevant):
                # Only StateProvider's reset notice can supply an ACK-able baseline.
                self.description = None
                self.awaiting_baseline = True
                self.error = "Stale: awaiting provider baseline/reset reconciliation"
            elif any(n.kind == "permissions" for n in relevant):
                self.description = None
                self.error = "Permissions changed; reloading capabilities"
        if self._describe_pending and not self.loading and not self.awaiting_baseline:
            context = self.context

            def accept(result) -> None:
                if not isinstance(result, ExtensionDescription):
                    raise TypeError("describe must return ExtensionDescription")
                self.loading = False
                if result.protocol_version[0] != 1:
                    self.error = "Unsupported extension major version"
                    return
                self.description = result
                self.error = ""

            def failed(error: TuiExtensionError) -> None:
                self.loading = False
                self.error = f"{error.code}: {error}"

            if self._submit(lambda: self.extension.describe(context), accept, failed):
                self.loading = True
                self._describe_pending = False
        return changed

    def reconciled(self) -> None:
        self.awaiting_baseline = False
        self._describe_pending = True

    def read(
        self,
        panel: PanelDescriptor,
        cursor: str | None,
        accept: Callable[[ExtensionPage], None],
        fail: Callable[[TuiExtensionError], None],
    ) -> bool:
        context = self.context

        def read() -> ExtensionPage:
            page = self.extension.read(
                panel.resource, context, cursor=cursor, limit=100, byte_limit=262144
            )
            if (
                len(page.items) > 100
                or len(json.dumps(json_plain(page.items)).encode("utf-8")) > 262144
            ):
                raise TuiExtensionError("integrity", "Provider exceeded requested page bounds")
            return page

        def received(page) -> None:
            if not isinstance(page, ExtensionPage):
                raise TypeError("read must return ExtensionPage")
            accept(page)

        return self._submit(read, received, fail)

    def invoke(
        self,
        action: ActionDescriptor,
        payload: Object,
        intent_id: str,
        accept: Callable[[ActionResult], None],
        fail: Callable[[TuiExtensionError], None],
    ) -> bool:
        context = self.context
        if self.read_only and action.mutates:
            fail(TuiExtensionError("read_only", "Read-only mode", context=context))
            return False
        description = self.description
        current = (
            next((item for item in description.actions if item.id == action.id), None)
            if description is not None
            else None
        )
        if current is not None and self.read_only and current.mutates:
            fail(TuiExtensionError("read_only", "Read-only mode", context=context))
            return False
        if current is None or not current.available:
            fail(
                TuiExtensionError(
                    "forbidden",
                    (current.reason or "Action is unavailable")
                    if current
                    else "Action is no longer available",
                    context=context,
                )
            )
            return False

        def received(result) -> None:
            if not isinstance(result, ActionResult) or result.intent_id != intent_id:
                raise TuiExtensionError("integrity", "Action returned a different intent")
            accept(result)

        return self._submit(
            lambda: self.extension.invoke(action.id, context, payload, intent_id=intent_id),
            received,
            fail,
        )

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            closed = self._closed
            self._closed = True
            self._notices.clear()
        try:
            if not closed:
                self._unsubscribe()
        finally:
            for future in self._futures:
                future.cancel()
            self._executor.shutdown(wait=wait, cancel_futures=True)
