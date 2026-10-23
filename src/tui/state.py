"""StateProvider protocol — the contract between TUI widgets and data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, runtime_checkable

from .models import (
    CatalogSnapshot,
    DetailUpdate,
    LogEntry,
    ResetBaseline,
    RunState,
    StreamResetNotice,
    TraceDetail,
    WorkflowInfo,
)


class StateProvider(Protocol):
    """State and action contract consumed by the Avalanche TUI."""

    operator_reachable: bool
    operator_instance_id: str
    stream_state: str
    stream_error: str

    def list_workflows(self) -> list[WorkflowInfo]: ...

    def get_catalog(self) -> CatalogSnapshot: ...

    def list_runs(self, workflow_selector: str) -> list[RunState]: ...

    def get_run(self, run_id: str) -> RunState | None: ...
    def hydrate_trace(self, run_id: str, node_id: str) -> TraceDetail | None:
        """Hydrate one selected agent node's immutable trace body."""
        ...

    def start_run(self, workflow_selector: str, **kwargs: Any) -> str:
        """Start a new run, return run_id."""
        ...

    def cancel_run(self, run_id: str) -> None: ...

    def on_run_update(self, callback: Callable[[RunState], None]) -> None: ...

    def on_catalog_update(self, callback: Callable[[CatalogSnapshot], None]) -> None: ...

    def on_log(self, callback: Callable[[LogEntry], None]) -> None: ...
    def on_detail_update(self, callback: Callable[[DetailUpdate], None]) -> None: ...
    def start_stream(self) -> None:
        """Start update delivery after all callbacks are registered."""
        ...

    def close(self) -> None:
        """Release provider-owned streams and in-flight RPCs."""
        ...

    def on_stream_reset(self, callback: Callable[[StreamResetNotice], None]) -> None: ...

    def load_reset_baseline(self, notice: StreamResetNotice) -> ResetBaseline: ...

    def acknowledge_stream_reset(
        self,
        generation: int,
        operator_instance_id: str,
        reconciled_event_ulid: str,
    ) -> None: ...


@runtime_checkable
class ConnectionAwareStateProvider(Protocol):
    """Optional connection state exposed by remote providers."""

    operator_reachable: bool

    @property
    def connection_label(self) -> str: ...

    @property
    def last_error(self) -> str: ...

    def ping(self) -> bool: ...


def get_operator_reachability(provider: StateProvider) -> bool:
    """Return the provider's explicit operator reachability."""
    return provider.operator_reachable


def get_stream_state(provider: StateProvider) -> str:
    """Return the provider's explicit stream lifecycle value."""
    value = getattr(provider.stream_state, "value", provider.stream_state)
    if not isinstance(value, str):
        raise TypeError("provider.stream_state must be a string-valued state")
    return value


type JSON = None | bool | int | float | str | list[JSON] | tuple[JSON, ...] | Mapping[str, JSON]
type Object = Mapping[str, JSON]
type Schema = Object
type Cursor = str
type Revision = str


def json_plain(value: JSON) -> JSON:
    """Convert immutable provider JSON containers at the serialization boundary."""
    if isinstance(value, Mapping):
        return {key: json_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class TuiContext:
    scope: tuple[tuple[str, str], ...]
    flow: str | None = None
    version: str | None = None
    run: str | None = None
    node: str | None = None
    attempt: str | None = None
    generation: int = 0


@dataclass(frozen=True)
class ScopeDescriptor:
    id: str
    label: str
    context: TuiContext
    selectable: bool
    reason: str | None


@dataclass(frozen=True)
class ActionDescriptor:
    id: str
    label: str
    permission: str | None
    available: bool
    reason: str | None
    mutates: bool
    confirmation: str | None
    input_schema: Schema | None


@dataclass(frozen=True)
class PanelDescriptor:
    id: str
    label: str
    slot: Literal["navigation", "flow", "run", "node", "tools"]
    resource: str
    renderer: Literal["table", "tree", "json", "diff", "widget"]
    columns: tuple[str, ...] = ()
    widget_factory: str | None = None


@dataclass(frozen=True)
class ExtensionDescription:
    context: TuiContext
    protocol_version: tuple[int, int]
    features: frozenset[str]
    revision: Revision
    scopes: tuple[ScopeDescriptor, ...]
    actions: tuple[ActionDescriptor, ...]
    panels: tuple[PanelDescriptor, ...]


@dataclass(frozen=True)
class ExtensionPage:
    context: TuiContext
    items: tuple[Object, ...]
    next_cursor: Cursor | None
    truncated: bool
    revision: Revision


@dataclass(frozen=True)
class ActionResult:
    context: TuiContext
    intent_id: str
    status: Literal["accepted", "completed"]
    revision: Revision | None
    value: Object


@dataclass(frozen=True)
class Invalidation:
    context: TuiContext
    resource: str
    revision: Revision
    cursor: Cursor | None
    kind: Literal["changed", "reset", "permissions"]
    reason: str | None


class TuiExtensionError(Exception):
    """Safe provider error; messages must not contain transport bodies or secrets."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        retry_after_s: float | None = None,
        context: TuiContext | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.context = context


@runtime_checkable
class TuiExtensions(Protocol):
    """Optional v1 interface. Providers own authorization, deadlines and replay.

    Calls run off the UI thread. Returned context must exactly echo the input,
    including generation. Mappings are immutable. Cursors pin resource/revision;
    pages contain 1–500 items and at most 1 MiB encoded JSON. Invalidations are
    hints, not execution truth: resets still use the baseline/ACK handshake.
    """

    protocol_version: tuple[int, int]
    features: frozenset[str]

    def describe(self, context: TuiContext) -> ExtensionDescription: ...

    def read(
        self,
        resource: str,
        context: TuiContext,
        *,
        cursor: Cursor | None = None,
        limit: int = 100,
        byte_limit: int = 262144,
    ) -> ExtensionPage: ...

    def invoke(
        self,
        action: str,
        context: TuiContext,
        payload: Object,
        *,
        intent_id: str,
    ) -> ActionResult: ...

    def subscribe(self, callback: Callable[[Invalidation], None]) -> Callable[[], None]: ...


def get_tui_extensions(provider: StateProvider) -> TuiExtensions | None:
    """Negotiate the optional boundary without expanding the legacy protocol."""
    extension = getattr(provider, "tui_extensions", None)
    if extension is None:
        return None
    if not isinstance(extension, TuiExtensions):
        raise TypeError("tui_extensions must implement TuiExtensions")
    return extension if extension.protocol_version[0] == 1 else None


class ReadOnlyExtensions:
    """Provider-facing write fence, independent of keyboard or palette dispatch."""

    def __init__(self, extension: TuiExtensions) -> None:
        self._extension = extension
        self.protocol_version = extension.protocol_version
        self.features = extension.features

    def describe(self, context: TuiContext) -> ExtensionDescription:
        return self._extension.describe(context)

    def read(
        self,
        resource: str,
        context: TuiContext,
        *,
        cursor: Cursor | None = None,
        limit: int = 100,
        byte_limit: int = 262144,
    ) -> ExtensionPage:
        return self._extension.read(
            resource, context, cursor=cursor, limit=limit, byte_limit=byte_limit
        )

    def invoke(
        self, action: str, context: TuiContext, payload: Object, *, intent_id: str
    ) -> ActionResult:
        descriptor = next(
            (item for item in self.describe(context).actions if item.id == action), None
        )
        if descriptor is None or descriptor.mutates:
            raise TuiExtensionError(
                "read_only", "Read-only provider rejects this action", context=context
            )
        return self._extension.invoke(action, context, payload, intent_id=intent_id)

    def subscribe(self, callback: Callable[[Invalidation], None]) -> Callable[[], None]:
        return self._extension.subscribe(callback)


class ReadOnlyStateProvider:
    """An explicit transport-facing mutation fence around a legacy provider.

    The injected original remains caller-owned. This proxy does not grant server
    authorization: authenticated providers must continue enforcing it themselves.
    """

    def __init__(self, provider: StateProvider) -> None:
        self._provider = provider
        extension = get_tui_extensions(provider)
        self.tui_extensions = ReadOnlyExtensions(extension) if extension is not None else None

    @property
    def operator_reachable(self) -> bool:
        return self._provider.operator_reachable

    @property
    def operator_instance_id(self) -> str:
        return self._provider.operator_instance_id

    @property
    def stream_state(self) -> str:
        return self._provider.stream_state

    @property
    def stream_error(self) -> str:
        return self._provider.stream_error

    @property
    def connection_label(self) -> str:
        if isinstance(self._provider, ConnectionAwareStateProvider):
            return self._provider.connection_label
        return "Provider"

    @property
    def last_error(self) -> str:
        return self._provider.stream_error

    def list_workflows(self) -> list[WorkflowInfo]:
        return self._provider.list_workflows()

    def get_catalog(self) -> CatalogSnapshot:
        return self._provider.get_catalog()

    def list_runs(self, workflow_selector: str) -> list[RunState]:
        return self._provider.list_runs(workflow_selector)

    def get_run(self, run_id: str) -> RunState | None:
        return self._provider.get_run(run_id)

    def hydrate_trace(self, run_id: str, node_id: str) -> TraceDetail | None:
        return self._provider.hydrate_trace(run_id, node_id)

    def start_run(self, workflow_selector: str, **kwargs: JSON) -> str:
        raise TuiExtensionError("read_only", "Read-only provider cannot start runs")

    def cancel_run(self, run_id: str) -> None:
        raise TuiExtensionError("read_only", "Read-only provider cannot cancel runs")

    def on_run_update(self, callback: Callable[[RunState], None]) -> None:
        self._provider.on_run_update(callback)

    def on_catalog_update(self, callback: Callable[[CatalogSnapshot], None]) -> None:
        self._provider.on_catalog_update(callback)

    def on_log(self, callback: Callable[[LogEntry], None]) -> None:
        self._provider.on_log(callback)

    def on_detail_update(self, callback: Callable[[DetailUpdate], None]) -> None:
        self._provider.on_detail_update(callback)

    def on_stream_reset(self, callback: Callable[[StreamResetNotice], None]) -> None:
        self._provider.on_stream_reset(callback)

    def start_stream(self) -> None:
        self._provider.start_stream()

    def close(self) -> None:
        self._provider.close()

    def load_reset_baseline(self, notice: StreamResetNotice) -> ResetBaseline:
        return self._provider.load_reset_baseline(notice)

    def acknowledge_stream_reset(
        self, generation: int, operator_instance_id: str, reconciled_event_ulid: str
    ) -> None:
        self._provider.acknowledge_stream_reset(
            generation, operator_instance_id, reconciled_event_ulid
        )


__all__ = [
    "ActionDescriptor",
    "ActionResult",
    "ExtensionDescription",
    "ExtensionPage",
    "Invalidation",
    "PanelDescriptor",
    "ReadOnlyStateProvider",
    "ScopeDescriptor",
    "TuiContext",
    "TuiExtensionError",
    "TuiExtensions",
    "ConnectionAwareStateProvider",
    "StateProvider",
    "get_operator_reachability",
    "get_stream_state",
]
