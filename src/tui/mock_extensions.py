"""TuiExtensions v1 for the local simulation, without transport emulation."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import MappingProxyType
from typing import TYPE_CHECKING

from .models import RunStatus
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
)

if TYPE_CHECKING:
    from .mock import MockStateProvider


class MockTuiExtensions:
    protocol_version = (1, 0)
    features = frozenset({"paged-resources", "json-schema-forms", "invalidations"})

    def __init__(self, provider: MockStateProvider) -> None:
        self.provider = provider
        self._callbacks: list[Callable[[Invalidation], None]] = []
        self._context = TuiContext(())
        self._revision = 0
        self._intents: dict[str, tuple[str, TuiContext, Object, ActionResult]] = {}

    def describe(self, context: TuiContext) -> ExtensionDescription:
        self._context = context
        run = self.provider.get_run(context.run) if context.run else None
        return ExtensionDescription(
            context,
            self.protocol_version,
            self.features,
            str(self._revision),
            (),
            (
                ActionDescriptor(
                    "run.start",
                    "Start run",
                    None,
                    context.flow is not None,
                    None if context.flow else "Select a workflow",
                    True,
                    "Confirm run input",
                    {"type": "object", "properties": {}},
                ),
                ActionDescriptor(
                    "run.cancel",
                    "Cancel run",
                    None,
                    run is not None
                    and run.status
                    in (RunStatus.PENDING, RunStatus.RUNNING, RunStatus.REQUESTING),
                    "Select an active run",
                    True,
                    "Confirm cancellation",
                    None,
                ),
            ),
            (
                PanelDescriptor(
                    "schedules",
                    "Schedules",
                    "tools",
                    "schedules",
                    "table",
                    ("id", "cron", "control"),
                ),
            ),
        )

    def read(
        self,
        resource: str,
        context: TuiContext,
        *,
        cursor: str | None = None,
        limit: int = 100,
        byte_limit: int = 262144,
    ) -> ExtensionPage:
        if not 1 <= limit <= 500 or not 1 <= byte_limit <= 1048576:
            raise TuiExtensionError("invalid_input", "Invalid page bounds", context=context)
        if resource != "schedules":
            raise TuiExtensionError("unsupported", "Unknown mock resource", context=context)
        revision = str(self._revision)
        offset = 0
        if cursor is not None:
            binding, offset_text = cursor.rsplit(":", 1)
            if binding != f"schedules:{context!r}:{revision}":
                raise TuiExtensionError(
                    "cursor_expired", "Page context changed", context=context
                )
            offset = int(offset_text)
        rows = tuple(
            MappingProxyType(
                {"id": flow.selector, "cron": flow.cron, "control": "inspection_only"}
            )
            for flow in self.provider.list_workflows()
            if flow.cron
        )
        items: list[Object] = []
        size = 2
        for row in rows[offset : offset + limit]:
            row_size = len(json.dumps(dict(row)).encode()) + 1
            if size + row_size > byte_limit:
                raise TuiExtensionError(
                    "invalid_input", "Page byte limit too small", context=context
                )
            size += row_size
            items.append(row)
        end = offset + len(items)
        next_cursor = f"schedules:{context!r}:{revision}:{end}" if end < len(rows) else None
        return ExtensionPage(context, tuple(items), next_cursor, False, revision)

    def invoke(
        self, action: str, context: TuiContext, payload: Object, *, intent_id: str
    ) -> ActionResult:
        previous = self._intents.get(intent_id)
        if previous is not None:
            if previous[:3] != (action, context, payload):
                raise TuiExtensionError("conflict", "Intent payload changed", context=context)
            return previous[3]
        descriptor = next((a for a in self.describe(context).actions if a.id == action), None)
        if descriptor is None or not descriptor.available:
            raise TuiExtensionError("unsupported", "Action unavailable", context=context)
        if action == "run.start":
            assert context.flow is not None
            value = MappingProxyType(
                {"run_id": self.provider.start_run(context.flow, **payload)}
            )
        else:
            assert context.run is not None
            self.provider.cancel_run(context.run)
            value = MappingProxyType({"run_id": context.run})
        result = ActionResult(context, intent_id, "accepted", str(self._revision), value)
        self._intents[intent_id] = (action, context, MappingProxyType(dict(payload)), result)
        self.invalidate()
        return result

    def subscribe(self, callback: Callable[[Invalidation], None]) -> Callable[[], None]:
        self._callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return unsubscribe

    def invalidate(self) -> None:
        self._revision += 1
        notice = Invalidation(self._context, "runs", str(self._revision), None, "changed", None)
        for callback in tuple(self._callbacks):
            callback(notice)

    def close(self) -> None:
        self._callbacks.clear()
        self._intents.clear()
