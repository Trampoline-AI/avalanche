"""UIStore — single source of truth for all mutable TUI state."""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import dataclass, field
from queue import Empty
from typing import Any, Callable, Literal

from .dag_layout import DagNode, SeqGroup, build_nav_grid, nav_move, workflow_to_layout
from .models import (
    AgentEventDetailAppended,
    DetailDelta,
    LogDetailAppended,
    LogEntry,
    NodeState,
    NodeStatus,
    ResetBaseline,
    RunState,
    RunStatus,
    StreamResetNotice,
    WorkflowInfo,
)
from .state import StateProvider, get_stream_state

RESET_RECONCILIATION_INITIAL_BACKOFF_SECONDS = 0.1
RESET_RECONCILIATION_MAX_BACKOFF_SECONDS = 2.0
DETAIL_HYDRATION_INITIAL_BACKOFF_SECONDS = 0.1
DETAIL_HYDRATION_MAX_BACKOFF_SECONDS = 2.0
BACKGROUND_UPDATE_CAPACITY = 256
BACKGROUND_UPDATES_PER_TICK = 64

TraceInspectorTab = Literal["trace", "output", "metadata"]
_TRACE_INSPECTOR_TABS: tuple[TraceInspectorTab, ...] = (
    "trace",
    "output",
    "metadata",
)
TraceHydrationKey = tuple[str, str, int]
RunDetailKey = tuple[str, str, int]


@dataclass(frozen=True)
class TraceDetailCompletion:
    """One trace body read, isolated from mutable structural run state."""

    attempt: int
    operator_instance_id: str
    run_id: str
    created_sequence: int
    node_id: str
    descriptor_revision: int
    trace_body: dict[str, Any] | None


@dataclass
class _DetailHydrationRequirements:
    """Highest missing detail watermarks for one immutable run identity."""

    log_sequence: int = 0
    event_sequences: dict[str, int] = field(default_factory=dict)
    version: int = 0

    def merge(
        self,
        *,
        log_sequence: int,
        event: tuple[str, int] | None,
    ) -> bool:
        previous_log_sequence = self.log_sequence
        self.log_sequence = max(self.log_sequence, log_sequence)
        stronger = self.log_sequence > previous_log_sequence
        if event is not None:
            node_id, event_sequence = event
            previous_event_sequence = self.event_sequences.get(node_id, 0)
            self.event_sequences[node_id] = max(previous_event_sequence, event_sequence)
            stronger = self.event_sequences[node_id] > previous_event_sequence or stronger
        if stronger:
            self.version += 1
        return stronger


@dataclass(frozen=True)
class _DetailHydrationRetry:
    """One UI-loop deadline guarded by an opaque retry generation."""

    generation: int
    requirements_version: int
    deadline: float


@dataclass(frozen=True)
class _DetailRepairWatermark:
    """Highest dropped append for one immutable run detail stream."""

    key: RunDetailKey
    node_id: str | None
    sequence: int


class _BoundedBackgroundUpdates:
    """Thread-safe UI handoff with bounded stream loss accounting."""

    def __init__(self, capacity: int = BACKGROUND_UPDATE_CAPACITY) -> None:
        self._capacity = capacity
        self._items: deque[tuple[str, Any]] = deque()
        self._lock = threading.Lock()
        self._structure_lost = False
        self._all_details_lost = False
        self._detail_repairs: OrderedDict[tuple[RunDetailKey, str | None], int] = OrderedDict()

    @staticmethod
    def _run_key(item: tuple[str, Any]) -> RunDetailKey | None:
        kind, payload = item
        if kind != "run" or not isinstance(payload, RunState):
            return None
        return (payload.operator_instance_id, payload.run_id, payload.created_sequence)

    @staticmethod
    def _detail_watermark(item: tuple[str, Any]) -> _DetailRepairWatermark | None:
        kind, payload = item
        if kind != "detail":
            return None
        key = (payload.operator_instance_id, payload.run_id, payload.created_sequence)
        if isinstance(payload, LogDetailAppended):
            return _DetailRepairWatermark(key, None, payload.log_sequence)
        if isinstance(payload, AgentEventDetailAppended):
            return _DetailRepairWatermark(
                key,
                payload.node_id,
                payload.event.event_sequence,
            )
        return None

    def _record_detail_loss_locked(self, item: tuple[str, Any]) -> None:
        watermark = self._detail_watermark(item)
        if watermark is None:
            return
        repair_key = (watermark.key, watermark.node_id)
        current = self._detail_repairs.get(repair_key, 0)
        self._detail_repairs[repair_key] = max(current, watermark.sequence)
        self._detail_repairs.move_to_end(repair_key)
        if len(self._detail_repairs) > self._capacity:
            self._detail_repairs.popitem(last=False)
            self._all_details_lost = True

    def _record_stream_loss_locked(self, item: tuple[str, Any]) -> None:
        if self._run_key(item) is not None:
            self._structure_lost = True
        else:
            self._record_detail_loss_locked(item)

    def put(self, item: tuple[str, Any]) -> None:
        """Enqueue without blocking, coalescing structural updates by run identity."""
        with self._lock:
            run_key = self._run_key(item)
            if run_key is not None:
                for index in range(len(self._items) - 1, -1, -1):
                    if self._run_key(self._items[index]) == run_key:
                        self._items[index] = item
                        return
            if len(self._items) < self._capacity:
                self._items.append(item)
                return
            if self._detail_watermark(item) is not None:
                self._record_detail_loss_locked(item)
                return

            evict_index = next(
                (
                    index
                    for index, queued in enumerate(self._items)
                    if self._run_key(queued) is not None
                    or self._detail_watermark(queued) is not None
                ),
                0,
            )
            evicted = self._items[evict_index]
            del self._items[evict_index]
            self._record_stream_loss_locked(evicted)
            self._items.append(item)

    def get(self) -> tuple[str, Any]:
        """Return overflow repair before ordinary queued work."""
        with self._lock:
            if self._structure_lost or self._all_details_lost or self._detail_repairs:
                repairs = tuple(
                    _DetailRepairWatermark(key, node_id, sequence)
                    for (key, node_id), sequence in self._detail_repairs.items()
                )
                payload = (self._structure_lost, self._all_details_lost, repairs)
                self._structure_lost = False
                self._all_details_lost = False
                self._detail_repairs.clear()
                return ("stream_handoff_overflow", payload)
            if not self._items:
                raise Empty
            return self._items.popleft()

    def empty(self) -> bool:
        with self._lock:
            return not (
                self._items
                or self._structure_lost
                or self._all_details_lost
                or self._detail_repairs
            )

    def qsize(self) -> int:
        with self._lock:
            overflow = int(
                self._structure_lost or self._all_details_lost or bool(self._detail_repairs)
            )
            return len(self._items) + overflow


def _fmt_time(secs: float) -> str:
    mins, s = divmod(secs, 60)
    if mins > 0:
        return f"{int(mins)}m{s:.0f}s"
    return f"{s:.1f}s"


class UIStore:
    """Centralized store holding all mutable UI state.

    Widgets read from this store.  Mutations go through store methods.
    Access from any widget via ``self.app.store``.
    """

    def __init__(
        self,
        provider: StateProvider,
        *,
        defer_initial_catalog: bool = False,
        reset_baseline_loader: Callable[[StreamResetNotice], ResetBaseline] | None = None,
    ) -> None:
        self.provider = provider

        # ── Workflow / Run ──────────────────────────────────────────
        self.workflows: list[WorkflowInfo] = (
            [] if defer_initial_catalog else provider.list_workflows()
        )
        self.current_workflow: WorkflowInfo | None = None
        self.current_run: RunState | None = None
        self.run_pinned: bool = False  # True = user picked a run; False = follow latest
        self._runs_cache: list[RunState] = []
        self._run_cache_indexes: dict[tuple[str, str, int], int] = {}
        self._background_updates = _BoundedBackgroundUpdates()
        self._trace_hydration_completions: list[tuple[TraceDetailCompletion, bool]] = []
        self._log_details: dict[tuple[str, str, int], list[LogEntry]] = {}
        self._log_detail_sequences: dict[tuple[str, str, int], int] = {}
        self._agent_event_details: dict[tuple[str, str, int, str], list[dict[str, Any]]] = {}
        self._agent_event_sequences: dict[tuple[str, str, int, str], int] = {}
        self._detail_hydrations_in_flight: dict[RunDetailKey, int] = {}
        self._detail_hydration_requirements: dict[
            RunDetailKey, _DetailHydrationRequirements
        ] = {}
        self._detail_hydration_retries: dict[RunDetailKey, _DetailHydrationRetry] = {}
        self._detail_hydration_failures: dict[RunDetailKey, int] = {}
        self._invalid_agent_event_details: set[tuple[str, str, int, str]] = set()
        self._runs_refresh_in_flight: set[str] = set()
        self._status_refresh_in_flight = False
        self._catalog_refresh_in_flight = False
        self._start_run_in_flight = False
        self._start_request_generation = 0
        self._run_interaction_generation = 0
        self._detail_hydration_generation = 0
        self._detail_hydration_retry_generation = 0
        self._detail_hydration_now: Callable[[], float] = time.monotonic
        self._workflow_context_epoch = 0
        self._run_data_revisions: dict[str, int] = {}
        self.run_error: str = ""
        self.catalog_revision = 0
        self._reset_baseline_loader = reset_baseline_loader or provider.load_reset_baseline
        self._reset_reconciliations_in_flight: set[int] = set()
        self._latest_reset_generation = 0
        self._shutdown = threading.Event()
        self._detail_hydration_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="avalanche-detail-hydration",
        )
        self._detail_hydration_executor_closed = False
        provider.on_stream_reset(self._on_stream_reset)

        # ── DAG layout (derived from current_workflow) ─────────────
        self.dag: SeqGroup | None = None
        self.all_nodes: list[DagNode] = []
        self.nav_grid: list[list[DagNode]] = []

        # ── Selection / Navigation ─────────────────────────────────
        self.selected_node: DagNode | None = None
        self.preferred_row: int = 0

        # ── Pane focus ─────────────────────────────────────────────
        self.focused_pane: str = "dag"  # "sidebar" | "dag" | "run-history" | "log"
        self.trace_inspector_open: bool = False
        self.trace_inspector_tab: TraceInspectorTab = "trace"
        self.trace_turn_index: int = 0
        self.trace_collapsed_turns: set[int] = set()
        self.trace_show_full_output: bool = False

        # ── Search ─────────────────────────────────────────────────
        self.searching: bool = False
        self.search_query: str = ""
        self.search_index: int = -1
        self.match_count: int = 0

        # ── Animation / Time ───────────────────────────────────────
        self.frame: int = 0
        self.start_time: float = time.monotonic()

        # ── Sidebar ────────────────────────────────────────────────
        self.sidebar_visible: bool = False
        self.sidebar_width: int = 30
        self.sidebar_expanded: set[str] = set()
        self.sidebar_cursor: int = 0
        self.sidebar_selected_id: str = ""
        self.workflow_statuses: dict[str, RunStatus] = {}

        # Auto-expand all folders on init
        for p in self.workflows:
            parts = self._tree_source_file(p).split("/")[:-1]
            path = ""
            for part in parts:
                path = f"{path}/{part}" if path else part
                self.sidebar_expanded.add(path)

        # Set sidebar width to fit content
        self.sidebar_width = self._compute_sidebar_width()

        # Default to first workflow
        if self.workflows:
            self.switch_workflow(self.workflows[0])
        if defer_initial_catalog:
            self._refresh_workflow_catalog()

    def request_shutdown(self) -> None:
        """Stop accepting background work before provider teardown."""
        self._shutdown.set()
        self._cancel_detail_hydration_repairs()

    def shutdown(self) -> None:
        """Stop background work and join the owned detail hydration worker."""
        self.request_shutdown()
        if self._detail_hydration_executor_closed:
            return
        self._detail_hydration_executor_closed = True
        self._detail_hydration_executor.shutdown(wait=True, cancel_futures=True)

    # ── Derived properties (read-only, always consistent) ──────────

    @property
    def node_statuses(self) -> dict[str, NodeStatus]:
        if self.current_run is None:
            return {}
        return {nid: ns.status for nid, ns in self.current_run.nodes.items()}

    @property
    def node_elapsed(self) -> dict[str, float | None]:
        if self.current_run is None:
            return {}
        return {nid: ns.elapsed for nid, ns in self.current_run.nodes.items()}

    @property
    def logs(self) -> list[LogEntry]:
        if self.current_run is None:
            return []
        return self.current_run.logs

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def elapsed_str(self) -> str:
        return _fmt_time(self.elapsed)

    @property
    def run_state_label(self) -> str:
        if self.current_run is None:
            return "IDLE"
        return {
            RunStatus.RUNNING: "RUNNING",
            RunStatus.FAILED: "FAILED",
            RunStatus.SUCCESS: "DONE",
        }.get(self.current_run.status, "IDLE")

    @property
    def selected_run_id(self) -> str | None:
        return self.current_run.run_id if self.current_run else None

    @property
    def sidebar_selected_name(self) -> str:
        """Compatibility alias; the stored value is now a workflow ID."""
        return self.sidebar_selected_id

    @sidebar_selected_name.setter
    def sidebar_selected_name(self, value: str) -> None:
        self.sidebar_selected_id = value

    @property
    def runs_for_current_workflow(self) -> list[RunState]:
        """Returns cached runs list — refreshed in background every ~1s."""
        if self.current_workflow is None:
            return []
        # Guard: if cache is from a different workflow, return empty until refreshed
        if self._runs_cache and not self._run_matches(
            self._runs_cache[0], self.current_workflow
        ):
            return []
        return self._runs_cache

    def _refresh_runs_cache(self) -> None:
        """Refresh runs cache in a background thread."""
        if self.current_workflow is None:
            return
        provider = self.provider
        workflow_selector = self.current_workflow.selector
        if workflow_selector in self._runs_refresh_in_flight:
            return
        self._runs_refresh_in_flight.add(workflow_selector)
        data_revision = self._run_data_revision(workflow_selector)
        context_epoch = self._workflow_context_epoch

        import threading

        def _do_refresh():
            try:
                runs = provider.list_runs(workflow_selector)
            except Exception:
                runs = None
            self._background_updates.put(
                ("runs", (workflow_selector, data_revision, context_epoch, runs))
            )

        threading.Thread(target=_do_refresh, daemon=True).start()

    @property
    def selected_node_elapsed_str(self) -> str:
        if self.selected_node and self.current_run:
            ns = self.current_run.nodes.get(self.selected_node.name)
            if ns and ns.elapsed is not None:
                return _fmt_time(ns.elapsed)
        return ""

    @property
    def selected_agent_node_id(self) -> str | None:
        workflow = self.current_workflow
        node = self.selected_node
        if workflow is None or node is None or node.name not in workflow.agent_node_ids:
            return None
        return node.name

    @property
    def selected_agent_metadata_json(self) -> str | None:
        workflow = self.current_workflow
        node_id = self.selected_agent_node_id
        if workflow is None or node_id is None:
            return None
        return workflow.agent_metadata_json.get(node_id)

    @property
    def selected_agent_metadata(self) -> dict | None:
        metadata_json = self.selected_agent_metadata_json
        if not metadata_json:
            return None
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, ValueError):
            return None
        return metadata if isinstance(metadata, dict) else None

    @property
    def selected_agent_trace_envelope(self) -> dict | None:
        node_id = self.selected_agent_node_id
        if node_id is None or self.current_run is None:
            return None
        state = self.current_run.nodes.get(node_id)
        if state is None or not state.agent_trace_json:
            return None
        try:
            envelope = json.loads(state.agent_trace_json)
        except (TypeError, ValueError):
            return None
        return envelope if isinstance(envelope, dict) else None

    @property
    def selected_agent_events(self) -> list[dict]:
        run = self.current_run
        node_id = self.selected_agent_node_id
        if run is None or node_id is None:
            return []
        key = (
            run.operator_instance_id,
            run.run_id,
            run.created_sequence,
            node_id,
        )
        cached = self._agent_event_details.get(key)
        if cached is not None:
            return cached
        envelope = self.selected_agent_trace_envelope
        events: list[dict[str, Any]] = []
        if envelope is not None:
            trace = envelope.get("trace")
            if isinstance(trace, dict):
                evidence = trace.get("evidence")
                if isinstance(evidence, dict) and isinstance(evidence.get("events"), list):
                    events = [event for event in evidence["events"] if isinstance(event, dict)]
            if not events:
                raw_events = envelope.get("events")
                if isinstance(raw_events, list):
                    events = [event for event in raw_events if isinstance(event, dict)]
        self._agent_event_details[key] = events
        self._agent_event_sequences[key] = max(
            (
                event.get("sequence", 0)
                for event in events
                if isinstance(event.get("sequence"), int)
            ),
            default=0,
        )
        return events

    @property
    def selected_agent_outputs(self) -> dict[str, Any] | None:
        envelope = self.selected_agent_trace_envelope
        trace = envelope.get("trace") if envelope is not None else None
        if not isinstance(trace, dict):
            return None
        evidence = trace.get("evidence")
        if not isinstance(evidence, dict):
            return None
        events = evidence.get("events")
        if not isinstance(events, list):
            return None
        for event in reversed(events):
            if not isinstance(event, dict):
                continue
            if (event.get("event_kind") or event.get("kind")) != "run.succeeded":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                return None
            outputs = data.get("outputs")
            return outputs if isinstance(outputs, dict) else None
        return None

    @property
    def selected_agent_steps(self) -> list[dict]:
        envelope = self.selected_agent_trace_envelope
        trace = envelope.get("trace") if envelope is not None else None
        if not isinstance(trace, dict):
            return self.selected_agent_live_steps
        steps = trace.get("steps")
        if not isinstance(steps, list):
            return []
        return [step for step in steps if isinstance(step, dict)]

    @property
    def selected_agent_live_steps(self) -> list[dict]:
        """Build partial turn records from ordered live agent evidence."""
        steps_by_iteration: dict[int, dict] = {}
        for event in self.selected_agent_events:
            kind = event.get("event_kind") or event.get("kind")
            data = event.get("data")
            if not isinstance(kind, str) or not isinstance(data, dict):
                continue
            recorded_step = data.get("step")
            iteration = data.get("iteration")
            if iteration is None and isinstance(recorded_step, dict):
                iteration = recorded_step.get("iteration")
            if not isinstance(iteration, int):
                continue
            step = steps_by_iteration.setdefault(
                iteration,
                {
                    "iteration": iteration,
                    "code": "",
                    "output": None,
                    "error": None,
                    "duration_ms": 0,
                    "tool_calls": [],
                    "predict_calls": [],
                    "lm": {},
                    "usage": {},
                },
            )
            if kind == "code.generated":
                step["code"] = data.get("code") or ""
            elif kind == "code.executed":
                step["output"] = data.get("output")
                step["error"] = data.get("error")
            elif kind == "iteration.recorded":
                source = recorded_step if isinstance(recorded_step, dict) else data
                for field in ("duration_ms", "error", "tool_count", "predict_count"):
                    if field in source:
                        step[field] = source[field]
        return [steps_by_iteration[key] for key in sorted(steps_by_iteration)]

    @staticmethod
    def _detail_key(run: RunState) -> RunDetailKey:
        return (run.operator_instance_id, run.run_id, run.created_sequence)

    def _set_runs_cache(self, runs: list[RunState]) -> None:
        """Replace run history and rebuild its constant-time identity index."""
        self._runs_cache = runs
        self._run_cache_indexes = {
            self._detail_key(run): index for index, run in enumerate(runs)
        }

    def _replace_run_references(self, run: RunState) -> None:
        """Replace current/cache references for one exact run identity."""
        key = self._detail_key(run)
        if self.current_run is not None and self._detail_key(self.current_run) == key:
            self.current_run = run
        cache_index = self._run_cache_indexes.get(key)
        if cache_index is not None:
            self._runs_cache[cache_index] = run

    @staticmethod
    def _events_from_node(run: RunState, node_id: str) -> list[dict[str, Any]]:
        node = run.nodes.get(node_id)
        if node is None or not node.agent_trace_json:
            return []
        try:
            envelope = json.loads(node.agent_trace_json)
        except (TypeError, ValueError):
            return []
        if not isinstance(envelope, dict):
            return []
        trace = envelope.get("trace")
        if isinstance(trace, dict):
            evidence = trace.get("evidence")
            if isinstance(evidence, dict) and isinstance(evidence.get("events"), list):
                return [event for event in evidence["events"] if isinstance(event, dict)]
        events = envelope.get("events")
        if not isinstance(events, list):
            return []
        return [event for event in events if isinstance(event, dict)]

    @staticmethod
    def _event_sequence(events: list[dict[str, Any]]) -> int:
        return max(
            (
                event.get("sequence", 0)
                for event in events
                if isinstance(event.get("sequence"), int)
            ),
            default=0,
        )

    @staticmethod
    def _node_has_trace_body(node: NodeState) -> bool:
        if not node.agent_trace_json:
            return False
        try:
            envelope = json.loads(node.agent_trace_json)
        except (TypeError, ValueError):
            return False
        return isinstance(envelope, dict) and isinstance(envelope.get("trace"), dict)

    def _remember_run_details(self, run: RunState) -> set[str]:
        """Adopt explicit detail containers only when their watermark advances."""
        adopted_event_nodes: set[str] = set()
        if not run.details_hydrated:
            return adopted_event_nodes
        key = self._detail_key(run)
        known_log_sequence = self._log_detail_sequences.get(key, -1)
        if key not in self._log_details or run.latest_log_sequence > known_log_sequence:
            self._log_details[key] = run.logs
            self._log_detail_sequences[key] = run.latest_log_sequence

        for node_id, node in run.nodes.items():
            if not node.agent_trace_json:
                continue
            agent_key = (*key, node_id)
            events = self._events_from_node(run, node_id)
            event_sequence = self._event_sequence(events)
            known_event_sequence = self._agent_event_sequences.get(agent_key, -1)
            if (
                agent_key not in self._agent_event_details
                or agent_key in self._invalid_agent_event_details
                or event_sequence > known_event_sequence
            ):
                self._agent_event_details[agent_key] = events
                self._agent_event_sequences[agent_key] = event_sequence
                self._invalid_agent_event_details.discard(agent_key)
                adopted_event_nodes.add(node_id)
        return adopted_event_nodes

    def _merge_cached_details(self, run: RunState, previous: RunState | None) -> RunState:
        adopted_event_nodes = self._remember_run_details(run)
        result = run
        key = self._detail_key(result)
        cached_logs = self._log_details.get(key)
        if cached_logs is not None and run.logs is not cached_logs:
            result = copy(result)
            result.logs = cached_logs
        if cached_logs is not None:
            result.details_hydrated = (
                self._log_detail_sequences.get(key, 0) >= run.latest_log_sequence
            )
        if previous is not None and self._detail_key(previous) == key:
            nodes: dict[str, NodeState] | None = None
            for node_id, node in result.nodes.items():
                prior = previous.nodes.get(node_id)
                if prior is None or prior.agent_trace_json is None:
                    continue
                prior_revision = prior.trace.revision if prior.trace is not None else 0
                revision = node.trace.revision if node.trace is not None else 0
                preserve_prior = (
                    node.agent_trace_json is None and prior_revision == revision
                ) or (
                    node.agent_trace_json is not None
                    and (
                        revision < prior_revision
                        or (
                            revision == prior_revision
                            and node_id not in adopted_event_nodes
                            and (
                                not self._node_has_trace_body(node)
                                or self._node_has_trace_body(prior)
                            )
                        )
                    )
                )
                if not preserve_prior:
                    continue
                preserved = copy(node)
                preserved.agent_trace_json = prior.agent_trace_json
                if nodes is None:
                    if result is run:
                        result = copy(result)
                    nodes = dict(result.nodes)
                nodes[node_id] = preserved
            if nodes is not None:
                result.nodes = nodes
        return result

    def _invalidate_log_details(self, run: RunState) -> None:
        key = self._detail_key(run)
        self._log_details.pop(key, None)
        self._log_detail_sequences.pop(key, None)
        cache_index = self._run_cache_indexes.get(key)
        cached = self._runs_cache[cache_index] if cache_index is not None else None
        for candidate in (run, cached):
            if candidate is not None:
                candidate.details_hydrated = False

    def _invalidate_agent_event_details(self, run: RunState, node_id: str) -> None:
        agent_key = (*self._detail_key(run), node_id)
        self._agent_event_details.pop(agent_key, None)
        self._agent_event_sequences.pop(agent_key, None)
        self._invalid_agent_event_details.add(agent_key)

    def _cancel_detail_hydration_repairs(self) -> None:
        """Supersede all attempts and UI-loop retry deadlines."""
        self._detail_hydrations_in_flight.clear()
        self._detail_hydration_requirements.clear()
        self._detail_hydration_retries.clear()
        self._detail_hydration_failures.clear()

    def _reset_detail_hydration_backoff(self, key: RunDetailKey) -> None:
        self._detail_hydration_retries.pop(key, None)
        self._detail_hydration_failures.pop(key, None)

    def _finish_detail_hydration(self, key: RunDetailKey) -> None:
        self._detail_hydration_requirements.pop(key, None)
        self._reset_detail_hydration_backoff(key)

    def _schedule_detail_hydration(
        self,
        run: RunState,
        *,
        required_log_sequence: int = 0,
        required_event: tuple[str, int] | None = None,
    ) -> None:
        """Coalesce missing watermarks and immediately start newly stronger work."""
        workflow = self.current_workflow
        key = self._detail_key(run)
        if self._shutdown.is_set() or workflow is None or not self._run_matches(run, workflow):
            return
        requirements = self._detail_hydration_requirements.setdefault(
            key, _DetailHydrationRequirements()
        )
        stronger = requirements.merge(
            log_sequence=required_log_sequence,
            event=required_event,
        )
        if stronger:
            self._reset_detail_hydration_backoff(key)
        self._start_detail_hydration(run, key)

    def _start_detail_hydration(self, run: RunState, key: RunDetailKey) -> None:
        """Submit one repair using the current structural epoch and requirements."""
        workflow = self.current_workflow
        requirements = self._detail_hydration_requirements.get(key)
        if (
            self._shutdown.is_set()
            or key in self._detail_hydrations_in_flight
            or key in self._detail_hydration_retries
            or requirements is None
            or workflow is None
            or not self._run_matches(run, workflow)
        ):
            return
        selector = workflow.selector
        self._detail_hydration_generation += 1
        generation = self._detail_hydration_generation
        self._detail_hydrations_in_flight[key] = generation
        try:
            self._detail_hydration_executor.submit(
                self._load_detail_hydration,
                run.run_id,
                selector,
                self._run_data_revision(selector),
                self._workflow_context_epoch,
                key,
                generation,
                requirements.version,
                run.revision,
            )
        except RuntimeError:
            self._detail_hydrations_in_flight.pop(key, None)

    def _load_detail_hydration(
        self,
        run_id: str,
        selector: str,
        data_revision: int,
        context_epoch: int,
        key: RunDetailKey,
        generation: int,
        requirements_version: int,
        minimum_revision: int,
    ) -> None:
        """Read one full detail baseline on the lifecycle-owned worker."""
        try:
            fresh = self.provider.get_run(run_id)
        except Exception:
            fresh = None
        if self._shutdown.is_set():
            return
        self._background_updates.put(
            (
                "detail_hydration",
                (
                    selector,
                    data_revision,
                    context_epoch,
                    key,
                    generation,
                    requirements_version,
                    minimum_revision,
                    fresh,
                ),
            )
        )

    def _schedule_detail_hydration_retry(
        self,
        key: RunDetailKey,
        requirements: _DetailHydrationRequirements,
    ) -> None:
        """Record one exponential deadline for the UI loop, never the RPC worker."""
        if self._shutdown.is_set() or key in self._detail_hydrations_in_flight:
            return
        failures = self._detail_hydration_failures.get(key, 0) + 1
        self._detail_hydration_failures[key] = failures
        delay = min(
            DETAIL_HYDRATION_INITIAL_BACKOFF_SECONDS * (2 ** min(failures - 1, 10)),
            DETAIL_HYDRATION_MAX_BACKOFF_SECONDS,
        )
        self._detail_hydration_retry_generation += 1
        self._detail_hydration_retries[key] = _DetailHydrationRetry(
            generation=self._detail_hydration_retry_generation,
            requirements_version=requirements.version,
            deadline=self._detail_hydration_now() + delay,
        )

    def _apply_detail_hydration_retry(
        self,
        key: RunDetailKey,
        generation: int,
    ) -> None:
        """Launch only the still-current delayed retry generation."""
        retry = self._detail_hydration_retries.get(key)
        if retry is None or retry.generation != generation:
            return
        requirements = self._detail_hydration_requirements.get(key)
        if (
            self._shutdown.is_set()
            or requirements is None
            or requirements.version != retry.requirements_version
        ):
            self._detail_hydration_retries.pop(key, None)
            return
        current = self.current_run
        workflow = self.current_workflow
        if (
            current is None
            or self._detail_key(current) != key
            or workflow is None
            or not self._run_matches(current, workflow)
        ):
            self._finish_detail_hydration(key)
            return
        self._detail_hydration_retries.pop(key)
        self._start_detail_hydration(current, key)

    def _start_due_detail_hydrations(self) -> None:
        """Run due retry callbacks synchronously on the UI owner thread."""
        if self._shutdown.is_set():
            return
        now = self._detail_hydration_now()
        due = [
            (key, retry.generation)
            for key, retry in self._detail_hydration_retries.items()
            if retry.deadline <= now
        ]
        for key, generation in due:
            self._apply_detail_hydration_retry(key, generation)

    def _detail_requirements_satisfied_by_cache(
        self,
        key: RunDetailKey,
        requirements: _DetailHydrationRequirements,
    ) -> bool:
        if self._log_detail_sequences.get(key, -1) < requirements.log_sequence:
            return False
        return all(
            self._agent_event_sequences.get((*key, node_id), -1) >= event_sequence
            for node_id, event_sequence in requirements.event_sequences.items()
        )

    def _adopt_detail_hydration_progress(
        self,
        fresh: RunState,
        current: RunState,
        key: RunDetailKey,
        requirements: _DetailHydrationRequirements,
    ) -> bool:
        """Adopt monotonic bodies and report progress toward pending watermarks."""
        before_log = self._log_detail_sequences.get(key, -1)
        before_events = {
            node_id: self._agent_event_sequences.get((*key, node_id), -1)
            for node_id in requirements.event_sequences
        }
        merged = self._merge_cached_details(fresh, current)
        self._replace_run_references(merged)
        return self._log_detail_sequences.get(key, -1) > before_log or any(
            self._agent_event_sequences.get((*key, node_id), -1) > before_events[node_id]
            for node_id in requirements.event_sequences
        )

    def _apply_detail_hydration_completion(
        self,
        *,
        selector: str,
        data_revision: int,
        context_epoch: int,
        key: RunDetailKey,
        generation: int,
        requirements_version: int,
        minimum_revision: int,
        fresh: RunState | None,
    ) -> None:
        """Apply progress, replace superseded work once, or back off failures."""
        if self._detail_hydrations_in_flight.get(key) != generation:
            return
        self._detail_hydrations_in_flight.pop(key)
        requirements = self._detail_hydration_requirements.get(key)
        if requirements is None:
            return
        current = self.current_run
        workflow = self.current_workflow
        if (
            current is None
            or self._detail_key(current) != key
            or workflow is None
            or not self._run_matches(current, workflow)
        ):
            self._finish_detail_hydration(key)
            return
        if self._detail_requirements_satisfied_by_cache(key, requirements):
            self._finish_detail_hydration(key)
            return
        current_attempt = (
            selector == workflow.selector
            and context_epoch == self._workflow_context_epoch
            and data_revision == self._run_data_revision(selector)
        )
        superseded = requirements.version != requirements_version or not current_attempt
        if superseded:
            self._start_detail_hydration(current, key)
            return
        progress = False
        structurally_current = (
            fresh is not None
            and self._detail_key(fresh) == key
            and fresh.revision >= minimum_revision
            and fresh.revision >= current.revision
        )
        if structurally_current and fresh.details_hydrated:
            progress = self._adopt_detail_hydration_progress(
                fresh,
                current,
                key,
                requirements,
            )
        if self._detail_requirements_satisfied_by_cache(key, requirements):
            self._finish_detail_hydration(key)
            return
        if progress:
            self._reset_detail_hydration_backoff(key)
        self._schedule_detail_hydration_retry(key, requirements)

    def _apply_detail_update(self, detail: DetailDelta) -> bool:
        run = self.current_run
        if (
            run is None
            or run.operator_instance_id != detail.operator_instance_id
            or run.run_id != detail.run_id
            or run.created_sequence != detail.created_sequence
        ):
            return False
        key = self._detail_key(run)
        if isinstance(detail, LogDetailAppended):
            logs = self._log_details.get(key)
            if logs is None:
                if not run.details_hydrated and detail.log_sequence != 1:
                    self._invalidate_log_details(run)
                    self._schedule_detail_hydration(
                        run, required_log_sequence=detail.log_sequence
                    )
                    return False
                logs = list(run.logs)
                known_sequence = run.latest_log_sequence if run.details_hydrated else 0
                self._log_details[key] = logs
                self._log_detail_sequences[key] = known_sequence
            known_sequence = self._log_detail_sequences.get(key, 0)
            if detail.log_sequence <= known_sequence:
                return True
            if detail.log_sequence != known_sequence + 1:
                self._invalidate_log_details(run)
                self._schedule_detail_hydration(run, required_log_sequence=detail.log_sequence)
                return False
            logs.append(detail.log)
            self._log_detail_sequences[key] = detail.log_sequence
            cached_index = self._run_cache_indexes.get(key)
            cached = self._runs_cache[cached_index] if cached_index is not None else None
            for candidate in (run, cached):
                if candidate is None:
                    continue
                candidate.logs = logs
                candidate.latest_log_sequence = max(
                    candidate.latest_log_sequence, detail.log_sequence
                )
                candidate.details_hydrated = True
            return True

        if not isinstance(detail, AgentEventDetailAppended):
            return False
        node = run.nodes.get(detail.node_id)
        if node is None:
            return False
        agent_key = (*key, detail.node_id)
        if agent_key in self._invalid_agent_event_details:
            self._schedule_detail_hydration(
                run,
                required_event=(detail.node_id, detail.event.event_sequence),
            )
            return False
        events = self._agent_event_details.get(agent_key)
        if events is None:
            events = self._events_from_node(run, detail.node_id)
            known_sequence = self._event_sequence(events)
            self._agent_event_details[agent_key] = events
            self._agent_event_sequences[agent_key] = known_sequence
        known_sequence = self._agent_event_sequences.get(agent_key, 0)
        if detail.event.event_sequence <= known_sequence:
            return True
        try:
            event = json.loads(detail.event.event_json)
        except (TypeError, ValueError):
            return False
        if not isinstance(event, dict):
            return False
        events.append(event)
        self._agent_event_sequences[agent_key] = detail.event.event_sequence
        return True

    # ── Mutation: tick ──────────────────────────────────────────────

    def tick(self) -> None:
        """Advance frame and refresh workflow statuses."""
        self.frame += 1
        self._apply_background_updates()
        # Refresh provider data every ~1s in background threads (not every tick)
        if self.frame % 30 == 1:
            self._refresh_workflow_catalog()
            self._refresh_workflow_statuses()
            self._refresh_runs_cache()

    def enqueue_run_update(self, run: RunState) -> None:
        """Queue provider data for application on the UI thread."""
        self._background_updates.put(("run", run))

    def enqueue_detail_update(self, detail: DetailDelta) -> None:
        """Queue one identity-pinned detail append for the UI-thread reducer."""
        self._background_updates.put(("detail", detail))

    def enqueue_trace_hydration_completion(self, completion: TraceDetailCompletion) -> None:
        """Queue one narrow trace result for consumption on the UI thread."""
        self._background_updates.put(("trace_hydration_complete", completion))

    def take_trace_hydration_completions(
        self,
    ) -> list[tuple[TraceDetailCompletion, bool]]:
        """Transfer trace outcomes accumulated by the UI-thread reducer."""
        completions = self._trace_hydration_completions
        self._trace_hydration_completions = []
        return completions

    def _apply_trace_detail_completion(self, completion: TraceDetailCompletion) -> bool:
        """Install only a trace body when its structural identity is still exact."""
        run = self.current_run
        if (
            completion.trace_body is None
            or run is None
            or run.operator_instance_id != completion.operator_instance_id
            or run.run_id != completion.run_id
            or run.created_sequence != completion.created_sequence
        ):
            return False
        node = run.nodes.get(completion.node_id)
        descriptor = node.trace if node is not None else None
        if (
            node is None
            or node.node_id != completion.node_id
            or descriptor is None
            or descriptor.revision != completion.descriptor_revision
        ):
            return False
        try:
            envelope = json.loads(node.agent_trace_json) if node.agent_trace_json else {}
        except (TypeError, ValueError):
            envelope = {}
        if not isinstance(envelope, dict):
            envelope = {}
        envelope["trace"] = completion.trace_body
        updated_node = copy(node)
        updated_node.agent_trace_json = json.dumps(envelope, default=str)
        updated_run = copy(run)
        updated_run.nodes = dict(run.nodes)
        updated_run.nodes[completion.node_id] = updated_node
        self._replace_run_references(updated_run)
        return True

    def enqueue_polled_run_update(
        self,
        selector: str,
        data_revision: int,
        context_epoch: int,
        run: RunState,
    ) -> None:
        """Queue a unary poll result only for the state epoch that requested it."""
        self._background_updates.put(
            ("polled_run", (selector, data_revision, context_epoch, run))
        )

    @staticmethod
    def _reset_baseline_validation_error(
        notice: StreamResetNotice,
        baseline: ResetBaseline | None,
    ) -> str:
        if baseline is None or baseline.generation != notice.generation:
            return "baseline generation mismatch"
        if (
            not baseline.operator_instance_id
            or (
                notice.operator_instance_id
                and baseline.operator_instance_id != notice.operator_instance_id
            )
            or baseline.as_of_sequence < notice.observed_sequence
        ):
            return "invalid baseline identity or high-water"
        return ""

    def _on_stream_reset(self, notice: StreamResetNotice) -> None:
        """Reconcile an authoritative baseline until this reset is no longer pending."""
        if (
            self._shutdown.is_set()
            or get_stream_state(self.provider) == "stopped"
            or notice.generation < self._latest_reset_generation
            or notice.generation in self._reset_reconciliations_in_flight
        ):
            return
        self._latest_reset_generation = notice.generation
        self._cancel_detail_hydration_repairs()
        # Invalidate catalog/run responses issued before this reset immediately,
        # rather than waiting for the authoritative baseline to finish loading.
        self._workflow_context_epoch += 1
        self._reset_reconciliations_in_flight.add(notice.generation)
        loader = self._reset_baseline_loader

        def _do_reconcile() -> None:
            retry_count = 0
            while not self._reset_reconciliation_stopped(notice.generation):
                try:
                    baseline = loader(notice)
                    if validation_error := self._reset_baseline_validation_error(
                        notice, baseline
                    ):
                        raise RuntimeError(validation_error)
                except Exception as exc:
                    error = str(exc) or "Failed to reconcile live state"
                    self._background_updates.put(("stream_reset_error", (notice, error)))
                    delay = min(
                        RESET_RECONCILIATION_INITIAL_BACKOFF_SECONDS
                        * (2 ** min(retry_count, 10)),
                        RESET_RECONCILIATION_MAX_BACKOFF_SECONDS,
                    )
                    retry_count += 1
                    if self._wait_for_reset_retry(notice.generation, delay):
                        break
                    continue
                self._background_updates.put(("stream_reset", (notice, baseline, "")))
                return
            self._reset_reconciliations_in_flight.discard(notice.generation)

        threading.Thread(target=_do_reconcile, daemon=True).start()

    def _reset_reconciliation_stopped(self, generation: int) -> bool:
        return (
            self._shutdown.is_set()
            or generation != self._latest_reset_generation
            or get_stream_state(self.provider) == "stopped"
        )

    def _wait_for_reset_retry(self, generation: int, delay: float) -> bool:
        deadline = time.monotonic() + delay
        while not self._reset_reconciliation_stopped(generation):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._shutdown.wait(min(remaining, 0.1))
        return True

    def _refresh_workflow_catalog(self) -> None:
        if self._catalog_refresh_in_flight:
            return
        self._catalog_refresh_in_flight = True
        provider = self.provider
        context_epoch = self._workflow_context_epoch

        import threading

        def _do_refresh():
            try:
                workflows = provider.list_workflows()
            except Exception:
                workflows = None
            self._background_updates.put(("catalog", (context_epoch, workflows)))

        threading.Thread(target=_do_refresh, daemon=True).start()

    def _refresh_workflow_statuses(self) -> None:
        """Refresh sidebar workflow statuses in a background thread."""
        if self._status_refresh_in_flight:
            return
        self._status_refresh_in_flight = True
        provider = self.provider
        workflows = list(self.workflows)
        data_revisions = {
            workflow.selector: self._run_data_revision(workflow.selector)
            for workflow in workflows
        }

        import threading

        def _do_refresh():
            try:
                statuses: dict[str, RunStatus | None] = {}
                for p in workflows:
                    runs = provider.list_runs(p.selector)
                    statuses[p.selector] = runs[-1].status if runs else None
            except Exception:
                statuses = None
            self._background_updates.put(("statuses", (data_revisions, statuses)))

        threading.Thread(target=_do_refresh, daemon=True).start()

    # ── Mutation: workflow / run switching ──────────────────────────

    def switch_workflow(self, workflow: WorkflowInfo) -> None:
        """Switch to a different workflow — recompute DAG, reset selection."""
        self._run_interaction_generation += 1
        self._workflow_context_epoch += 1
        self._cancel_detail_hydration_repairs()
        self.close_trace_inspector()
        self.current_workflow = workflow
        self.dag, self.all_nodes = workflow_to_layout(workflow)
        self.nav_grid = build_nav_grid(self.dag)
        self.selected_node = None
        self.preferred_row = 0
        self.sidebar_selected_id = workflow.selector

        # Never show the previous workflow's runs while the new history loads.
        self._set_runs_cache([])
        self.current_run = None
        self.run_pinned = False
        self._refresh_runs_cache()

    def switch_run(self, run: RunState) -> None:
        self._run_interaction_generation += 1
        self._cancel_detail_hydration_repairs()
        self.close_trace_inspector()
        self.current_run = run
        self.run_pinned = True

    def deselect_run(self) -> None:
        """Unpin the current run — auto-follow will take over."""
        self._run_interaction_generation += 1
        self._cancel_detail_hydration_repairs()
        self.close_trace_inspector()
        self.run_pinned = False
        self.selected_node = None

    def auto_follow_latest_run(self) -> None:
        """When not pinned, keep current_run pointing at the latest run."""
        if self.run_pinned or self.current_workflow is None:
            return
        runs = self.runs_for_current_workflow
        latest = runs[-1] if runs else None
        if latest and (self.current_run is None or latest.run_id != self.current_run.run_id):
            self._cancel_detail_hydration_repairs()
            self.close_trace_inspector()
            self.current_run = latest

    # ── Mutation: pane focus ───────────────────────────────────────

    PANE_ORDER = ("sidebar", "run-history", "dag", "log")

    def cycle_pane(self, direction: int = 1) -> None:
        """Cycle focused pane forward (+1) or backward (-1).
        Skips sidebar when it is hidden."""
        order = [p for p in self.PANE_ORDER if p != "sidebar" or self.sidebar_visible]
        idx = order.index(self.focused_pane) if self.focused_pane in order else 0
        self.focused_pane = order[(idx + direction) % len(order)]

    def toggle_sidebar(self) -> None:
        self.sidebar_visible = not self.sidebar_visible
        if not self.sidebar_visible and self.focused_pane == "sidebar":
            self.focused_pane = "dag"
        if self.sidebar_visible:
            self.sidebar_width = self._compute_sidebar_width()
            self._sync_sidebar_cursor_to_workflow()

    def _compute_sidebar_width(self) -> int:
        """Compute ideal sidebar width to fit all visible tree content."""
        max_len = len("  EXPLORER")
        for p in self.workflows:
            parts = self._tree_source_file(p).split("/")[:-1]
            depth = len(parts)
            # indent + icon + space + name
            line_len = 2 + 2 * depth + 4 + len(p.rendered_name)
            max_len = max(max_len, line_len)
            for i, part in enumerate(parts):
                folder_len = 2 + 2 * i + 4 + len(part)
                max_len = max(max_len, folder_len)
        return max_len + 4  # border (2) + small padding

    # ── Mutation: node selection / navigation ──────────────────────

    def open_trace_inspector(self) -> bool:
        if self.selected_agent_node_id is None:
            return False
        self.trace_inspector_open = True
        self.trace_inspector_tab = "trace"
        self.focused_pane = "trace"
        steps = self.selected_agent_steps
        self.trace_turn_index = max(0, len(steps) - 1)
        self.trace_collapsed_turns.clear()
        self.trace_show_full_output = False
        return True

    def close_trace_inspector(self) -> None:
        self.trace_inspector_open = False
        self.trace_inspector_tab = "trace"
        self.trace_turn_index = 0
        self.trace_collapsed_turns.clear()
        self.trace_show_full_output = False
        if self.focused_pane == "trace":
            self.focused_pane = "dag"

    def move_trace_inspector_tab(self, delta: int) -> None:
        if not self.trace_inspector_open:
            return
        index = _TRACE_INSPECTOR_TABS.index(self.trace_inspector_tab)
        self.trace_inspector_tab = _TRACE_INSPECTOR_TABS[
            (index + delta) % len(_TRACE_INSPECTOR_TABS)
        ]

    def move_trace_turn(self, delta: int) -> None:
        steps = self.selected_agent_steps
        if not steps:
            self.trace_turn_index = 0
            return
        self.trace_turn_index = min(len(steps) - 1, max(0, self.trace_turn_index + delta))

    def toggle_trace_turn(self) -> None:
        index = self.trace_turn_index
        if index in self.trace_collapsed_turns:
            self.trace_collapsed_turns.remove(index)
        elif self.selected_agent_steps:
            self.trace_collapsed_turns.add(index)

    def toggle_trace_full_output(self) -> None:
        self.trace_show_full_output = not self.trace_show_full_output

    def select_node(self, node: DagNode) -> None:
        if self.trace_inspector_open and (
            self.selected_node is None or self.selected_node.name != node.name
        ):
            self.close_trace_inspector()
        self.selected_node = node
        self.preferred_row = node.row

    def deselect_node(self) -> None:
        self.close_trace_inspector()
        self.selected_node = None

    def move_nav(self, dx: int, dy: int) -> None:
        if self.selected_node is None:
            if self.all_nodes:
                self.selected_node = self.all_nodes[0]
                self.preferred_row = 0
            return
        self.selected_node, self.preferred_row = nav_move(
            self.nav_grid, self.selected_node, self.preferred_row, dx, dy
        )

    # ── Mutation: run control ──────────────────────────────────────

    def start_run(self) -> str | None:
        if not self.current_workflow:
            return None
        selector = self.current_workflow.selector
        try:
            run_id = self.provider.start_run(selector)
            if not run_id:
                self.run_error = "Run failed to start"
                return None
            run = self.provider.get_run(run_id)
            runs = self.provider.list_runs(selector)
        except Exception as exc:
            self.run_error = str(exc) or "Run failed to start"
            return None
        # Refresh cache so the new run appears immediately.
        self._set_runs_cache(runs)
        self.current_run = run
        if run is not None:
            self._replace_run_references(run)
        self._advance_run_data_revision(selector)
        self.start_time = time.monotonic()
        self.run_error = ""
        return run_id

    def start_run_async(self) -> bool:
        """Launch the complete start/get/list sequence off the UI thread."""
        workflow = self.current_workflow
        if workflow is None or self._start_run_in_flight:
            return False

        import threading

        self._start_run_in_flight = True
        self._start_request_generation += 1
        request_generation = self._start_request_generation
        interaction_generation = self._run_interaction_generation
        workflow_selector = workflow.selector
        data_revision = self._run_data_revision(workflow_selector)
        context_epoch = self._workflow_context_epoch
        provider = self.provider
        self.run_error = ""

        def _do_start() -> None:
            run_id = ""
            run = None
            runs = None
            error = ""
            try:
                run_id = provider.start_run(workflow_selector)
                if not run_id:
                    error = "Run failed to start"
                else:
                    run = provider.get_run(run_id)
                    runs = provider.list_runs(workflow_selector)
                    if run is None:
                        run = next((item for item in runs if item.run_id == run_id), None)
                        if run is None:
                            error = f"Started run {run_id} was not found"
            except Exception as exc:
                error = str(exc) or "Run failed to start"
            self._background_updates.put(
                (
                    "start_run",
                    (
                        workflow_selector,
                        request_generation,
                        interaction_generation,
                        data_revision,
                        context_epoch,
                        run_id,
                        run,
                        runs,
                        error,
                    ),
                )
            )

        threading.Thread(target=_do_start, daemon=True).start()
        return True

    def select_next_run(self) -> None:
        """Move down in run history (toward older runs)."""
        display = list(reversed(self.runs_for_current_workflow))
        if not display:
            return
        if self.current_run is None:
            self._run_interaction_generation += 1
            self._cancel_detail_hydration_repairs()
            self.current_run = display[0]
            self.run_pinned = True
            return
        for i, r in enumerate(display):
            if r.run_id == self.current_run.run_id:
                if i + 1 < len(display):
                    self._run_interaction_generation += 1
                    self._cancel_detail_hydration_repairs()
                    self.current_run = display[i + 1]
                    self.run_pinned = True
                return

    def select_prev_run(self) -> None:
        """Move up in run history (toward newer runs)."""
        display = list(reversed(self.runs_for_current_workflow))
        if not display:
            return
        if self.current_run is None:
            self._run_interaction_generation += 1
            self._cancel_detail_hydration_repairs()
            self.current_run = display[0]
            self.run_pinned = True
            return
        for i, r in enumerate(display):
            if r.run_id == self.current_run.run_id:
                if i > 0:
                    self._run_interaction_generation += 1
                    self._cancel_detail_hydration_repairs()
                    self.current_run = display[i - 1]
                    self.run_pinned = True
                return

    # ── Mutation: search ───────────────────────────────────────────

    def begin_search(self) -> None:
        self.searching = True
        self.search_query = ""
        self.search_index = -1

    def end_search(self) -> None:
        """Enter pressed — finalize search."""
        self.searching = False
        if self.match_count > 0:
            self.search_index = 0

    def cancel_search(self) -> None:
        """Escape during active search input."""
        self.searching = False

    def clear_search(self) -> None:
        """Escape when not searching — clear the query."""
        self.search_query = ""
        self.search_index = -1

    def search_append(self, char: str) -> None:
        self.search_query += char
        self.search_index = -1

    def search_backspace(self) -> None:
        self.search_query = self.search_query[:-1]
        self.search_index = -1

    def search_next(self) -> None:
        if self.match_count > 0:
            self.search_index = (self.search_index + 1) % self.match_count

    def search_prev(self) -> None:
        if self.match_count > 0:
            self.search_index = (self.search_index - 1) % self.match_count

    def set_match_count(self, count: int) -> None:
        self.match_count = count

    # ── Mutation: sidebar ──────────────────────────────────────────

    def sidebar_cursor_up(self) -> None:
        if self.sidebar_cursor > 0:
            self.sidebar_cursor -= 1

    def sidebar_cursor_down(self, max_items: int) -> None:
        if self.sidebar_cursor < max_items - 1:
            self.sidebar_cursor += 1

    def sidebar_toggle_expand(self, path: str) -> None:
        if path in self.sidebar_expanded:
            self.sidebar_expanded.discard(path)
        else:
            self.sidebar_expanded.add(path)

    def _sync_sidebar_cursor_to_workflow(self) -> None:
        """Move the sidebar cursor to the currently viewed workflow."""
        if not self.current_workflow:
            return
        # Rebuild the flat item list to find the cursor position.
        # Mirrors Sidebar._rebuild_tree / _walk_tree logic.
        tree: dict = {}
        for p in self.workflows:
            parts = self._tree_source_file(p).split("/")
            folders, node = parts[:-1], tree
            for f in folders:
                if f not in node:
                    node[f] = {}
                node = node[f]
            node[f"__workflow__{p.selector}"] = p

        flat: list[tuple[bool, str]] = []  # (is_folder, name_or_path)
        self._walk_flat(tree, "", flat)

        for idx, (is_folder, name) in enumerate(flat):
            if not is_folder and name == self.current_workflow.selector:
                self.sidebar_cursor = idx
                return

    def _walk_flat(self, node: dict, path_prefix: str, flat: list) -> None:
        folders = sorted(k for k in node if not k.startswith("__workflow__"))
        workflows = sorted(
            (node[k] for k in node if k.startswith("__workflow__")),
            key=lambda p: (p.rendered_name, p.selector),
        )
        for folder_name in folders:
            folder_path = f"{path_prefix}/{folder_name}" if path_prefix else folder_name
            flat.append((True, folder_path))
            if folder_path in self.sidebar_expanded:
                self._walk_flat(node[folder_name], folder_path, flat)
        for p in workflows:
            flat.append((False, p.selector))

    @staticmethod
    def _tree_source_file(workflow: WorkflowInfo) -> str:
        source = workflow.source_file.replace("\\", "/")
        if workflow.root_alias:
            return f"{workflow.root_alias}/{source}"
        return source

    def _merge_summary_runs(self, runs: list[RunState]) -> list[RunState]:
        """Preserve hydrated detail while refreshing summary metadata."""
        hydrated = {run.run_id: run for run in self._runs_cache if run.details_hydrated}
        if self.current_run is not None and self.current_run.details_hydrated:
            hydrated[self.current_run.run_id] = self.current_run

        merged: list[RunState] = []
        for summary in runs:
            detail = hydrated.get(summary.run_id)
            same_epoch = detail is not None and (
                not summary.operator_instance_id
                or not detail.operator_instance_id
                or summary.operator_instance_id == detail.operator_instance_id
            )
            if summary.details_hydrated or not same_epoch:
                merged.append(summary)
                continue

            run = copy(detail)
            if summary.revision >= detail.revision:
                run.flow_name = summary.flow_name
                run.status = summary.status
                run.started_at = summary.started_at
                run.ended_at = summary.ended_at
                run.triggered_by = summary.triggered_by
                run.workflow_id = summary.workflow_id
                run.workflow_display_name = summary.workflow_display_name
                run.operator_instance_id = summary.operator_instance_id
                run.created_sequence = summary.created_sequence
                run.revision = summary.revision
            merged.append(run)
        return merged

    @staticmethod
    def _run_matches(run: RunState, workflow: WorkflowInfo) -> bool:
        if run.workflow_id:
            return run.workflow_id == workflow.selector
        return run.flow_name in {workflow.name, workflow.rendered_name}

    def _run_data_revision(self, selector: str) -> int:
        return self._run_data_revisions.get(selector, 0)

    def _advance_run_data_revision(self, selector: str) -> None:
        """Invalidate authoritative run-data snapshots for one workflow."""
        self._run_data_revisions[selector] = self._run_data_revision(selector) + 1

    def _run_for_detail_key(self, key: RunDetailKey) -> RunState | None:
        if self.current_run is not None and self._detail_key(self.current_run) == key:
            return self.current_run
        cache_index = self._run_cache_indexes.get(key)
        return self._runs_cache[cache_index] if cache_index is not None else None

    def _repair_stream_handoff_overflow(
        self,
        structure_lost: bool,
        all_details_lost: bool,
        repairs: tuple[_DetailRepairWatermark, ...],
    ) -> None:
        """Recover dropped stream work from authoritative summary/detail reads."""
        if structure_lost:
            self._refresh_runs_cache()
        if all_details_lost and self.current_run is not None:
            run = self.current_run
            self._invalidate_log_details(run)
            self._schedule_detail_hydration(
                run,
                required_log_sequence=run.latest_log_sequence,
            )
            for node_id, node in run.nodes.items():
                latest_event_sequence = (
                    node.trace.latest_event_sequence if node.trace is not None else 0
                )
                if latest_event_sequence:
                    self._invalidate_agent_event_details(run, node_id)
                    self._schedule_detail_hydration(
                        run,
                        required_event=(node_id, latest_event_sequence),
                    )
        for repair in repairs:
            run = self._run_for_detail_key(repair.key)
            if run is None:
                continue
            if repair.node_id is None:
                self._invalidate_log_details(run)
                self._schedule_detail_hydration(
                    run,
                    required_log_sequence=repair.sequence,
                )
            else:
                self._invalidate_agent_event_details(run, repair.node_id)
                self._schedule_detail_hydration(
                    run,
                    required_event=(repair.node_id, repair.sequence),
                )

    def _apply_background_updates(self) -> None:
        for _ in range(BACKGROUND_UPDATES_PER_TICK):
            try:
                kind, payload = self._background_updates.get()
            except Empty:
                break
            if kind == "stream_handoff_overflow":
                self._repair_stream_handoff_overflow(*payload)
                continue
            if kind == "stream_reset_error":
                notice, error = payload
                if notice.generation == self._latest_reset_generation:
                    self.run_error = f"Live state reset failed: {error}"
                continue
            if kind == "stream_reset":
                notice, baseline, error = payload
                self._reset_reconciliations_in_flight.discard(notice.generation)
                if notice.generation != self._latest_reset_generation:
                    continue
                if error:
                    self.run_error = f"Live state reset failed: {error}"
                    continue
                validation_error = self._reset_baseline_validation_error(notice, baseline)
                if validation_error:
                    self.run_error = f"Live state reset failed: {validation_error}"
                    self._on_stream_reset(notice)
                    continue
                self._apply_reset_baseline(baseline)
                try:
                    self.provider.acknowledge_stream_reset(
                        notice.generation,
                        baseline.operator_instance_id,
                        baseline.as_of_sequence,
                    )
                except Exception as exc:
                    self.run_error = f"Live state reset failed: {exc}"
                continue
            if kind == "catalog":
                self._catalog_refresh_in_flight = False
                context_epoch, workflows = payload
                if workflows is not None and context_epoch == self._workflow_context_epoch:
                    self._reconcile_workflows(workflows)
            elif kind == "runs":
                selector, data_revision, context_epoch, runs = payload
                self._runs_refresh_in_flight.discard(selector)
                if (
                    runs is not None
                    and data_revision == self._run_data_revision(selector)
                    and context_epoch == self._workflow_context_epoch
                    and self.current_workflow is not None
                    and self.current_workflow.selector == selector
                ):
                    runs = self._merge_summary_runs(runs)
                    changed = self._runs_cache != runs
                    pinned_key = (
                        self._detail_key(self.current_run)
                        if self.run_pinned and self.current_run is not None
                        else None
                    )
                    self._set_runs_cache(runs)
                    if pinned_key is not None:
                        cache_index = self._run_cache_indexes.get(pinned_key)
                        if cache_index is not None:
                            self.current_run = self._runs_cache[cache_index]
                    else:
                        self.current_run = runs[-1] if runs else None
                    if runs:
                        self.workflow_statuses[selector] = runs[-1].status
                    if changed:
                        self._advance_run_data_revision(selector)
            elif kind == "statuses":
                self._status_refresh_in_flight = False
                data_revisions, statuses = payload
                if statuses is not None:
                    current_selectors = {workflow.selector for workflow in self.workflows}
                    for selector, status in statuses.items():
                        if selector not in current_selectors or data_revisions.get(
                            selector
                        ) != self._run_data_revision(selector):
                            continue
                        if status is None:
                            self.workflow_statuses.pop(selector, None)
                        else:
                            self.workflow_statuses[selector] = status
            elif kind == "detail_hydration":
                (
                    selector,
                    data_revision,
                    context_epoch,
                    key,
                    generation,
                    requirements_version,
                    minimum_revision,
                    fresh,
                ) = payload
                self._apply_detail_hydration_completion(
                    selector=selector,
                    data_revision=data_revision,
                    context_epoch=context_epoch,
                    key=key,
                    generation=generation,
                    requirements_version=requirements_version,
                    minimum_revision=minimum_revision,
                    fresh=fresh,
                )
            elif kind in {"run", "polled_run"}:
                if kind == "polled_run":
                    selector, data_revision, context_epoch, run = payload
                    if (
                        context_epoch != self._workflow_context_epoch
                        or data_revision != self._run_data_revision(selector)
                    ):
                        continue
                else:
                    run = payload
                workflow = next(
                    (item for item in self.workflows if self._run_matches(run, item)),
                    None,
                )
                if workflow is None:
                    continue
                selector = workflow.selector
                key = self._detail_key(run)
                current_match = (
                    self.current_run
                    if self.current_run is not None
                    and self._detail_key(self.current_run) == key
                    and self.current_workflow is not None
                    and self.current_workflow.selector == selector
                    else None
                )
                cache_index = self._run_cache_indexes.get(key)
                cached_match = (
                    self._runs_cache[cache_index] if cache_index is not None else None
                )
                previous = current_match or cached_match
                if previous is not None and run.revision < previous.revision:
                    continue
                run = self._merge_cached_details(run, previous)
                equivalents = [
                    item for item in (current_match, cached_match) if item is not None
                ]
                if not equivalents or any(item != run for item in equivalents):
                    self._advance_run_data_revision(selector)
                self._replace_run_references(run)
                if cache_index == len(self._runs_cache) - 1:
                    self.workflow_statuses[selector] = run.status
            elif kind == "detail":
                self._apply_detail_update(payload)
            elif kind == "trace_hydration_complete":
                applied = self._apply_trace_detail_completion(payload)
                self._trace_hydration_completions.append((payload, applied))
            elif kind == "start_run":
                (
                    selector,
                    request_generation,
                    interaction_generation,
                    data_revision,
                    context_epoch,
                    run_id,
                    run,
                    runs,
                    error,
                ) = payload
                if request_generation == self._start_request_generation:
                    self._start_run_in_flight = False
                context_relevant = (
                    request_generation == self._start_request_generation
                    and interaction_generation == self._run_interaction_generation
                    and context_epoch == self._workflow_context_epoch
                    and self.current_workflow is not None
                    and self.current_workflow.selector == selector
                )
                data_relevant = data_revision == self._run_data_revision(selector)
                relevant = context_relevant and data_relevant
                if not relevant:
                    if context_relevant and not data_relevant:
                        self._refresh_runs_cache()
                    continue
                if error:
                    self.run_error = error
                    continue
                self.run_error = ""
                self._set_runs_cache(runs or ([] if run is None else [run]))
                self.current_run = run or next(
                    (item for item in self._runs_cache if item.run_id == run_id), None
                )
                if self.current_run is not None:
                    self._replace_run_references(self.current_run)
                self.run_pinned = True
                self.start_time = time.monotonic()
                self._advance_run_data_revision(selector)
                if run_id and self._runs_cache:
                    self.workflow_statuses[selector] = self._runs_cache[-1].status
        self._start_due_detail_hydrations()

    def _apply_reset_baseline(self, baseline: ResetBaseline) -> None:
        """Atomically replace catalog/run caches before acknowledging a reset."""
        self._log_details.clear()
        self._log_detail_sequences.clear()
        self._agent_event_details.clear()
        self._agent_event_sequences.clear()
        self._cancel_detail_hydration_repairs()
        self._invalid_agent_event_details.clear()
        pinned_run_id = (
            self.current_run.run_id if self.run_pinned and self.current_run else None
        )
        previous_selectors = {workflow.selector for workflow in self.workflows}
        baseline_selectors = {workflow.selector for workflow in baseline.workflows}
        self._workflow_context_epoch += 1
        for selector in previous_selectors | baseline_selectors:
            self._advance_run_data_revision(selector)

        self._reconcile_workflows(list(baseline.workflows))
        # A selection change can schedule a refresh during reconciliation. Invalidate
        # it too so a pre-baseline response cannot overwrite authoritative state.
        for selector in previous_selectors | baseline_selectors:
            self._advance_run_data_revision(selector)
        self.workflow_statuses = {
            selector: runs[-1].status
            for selector, runs in baseline.runs_by_workflow.items()
            if runs
        }
        if self.current_workflow is None:
            self._set_runs_cache([])
            self.current_run = None
            self.run_pinned = False
        else:
            selector = self.current_workflow.selector
            runs = list(baseline.runs_by_workflow.get(selector, ()))
            self._set_runs_cache(runs)
            pinned = next(
                (run for run in runs if run.run_id == pinned_run_id),
                None,
            )
            self.current_run = pinned or (runs[-1] if runs else None)
            self.run_pinned = pinned is not None
        self.run_error = ""

    def _reconcile_workflows(self, workflows: list[WorkflowInfo]) -> None:
        old_signature = [self._workflow_revision_signature(item) for item in self.workflows]
        new_signature = [self._workflow_revision_signature(item) for item in workflows]
        old_folders = self._workflow_folder_paths(self.workflows)
        new_folders = self._workflow_folder_paths(workflows)
        selected_id = self.current_workflow.selector if self.current_workflow else ""
        self.workflows = workflows
        # Remote providers start with an empty catalog. Expand folders introduced by
        # an asynchronous refresh without reopening folders the user collapsed.
        self.sidebar_expanded.update(new_folders - old_folders)
        selected = next((item for item in workflows if item.selector == selected_id), None)
        if selected is None and workflows:
            selected = workflows[0]
        new_selected_id = selected.selector if selected is not None else ""
        changed_selection = new_selected_id != selected_id
        if changed_selection:
            self._run_interaction_generation += 1
            self._workflow_context_epoch += 1
        if selected is None:
            self.current_workflow = None
            self.current_run = None
            self._set_runs_cache([])
            self.dag = None
            self.all_nodes = []
            self.nav_grid = []
            self.sidebar_selected_id = ""
        else:
            self.current_workflow = selected
            self.dag, self.all_nodes = workflow_to_layout(selected)
            self.nav_grid = build_nav_grid(self.dag)
            self.sidebar_selected_id = selected.selector
            if changed_selection:
                self.current_run = None
                self._set_runs_cache([])
                self.run_pinned = False
                self._refresh_runs_cache()
            if self.selected_node is not None:
                selected_node_id = self.selected_node.name
                self.selected_node = next(
                    (item for item in self.all_nodes if item.name == selected_node_id), None
                )
        self.sidebar_width = self._compute_sidebar_width()
        if old_signature != new_signature:
            self.catalog_revision += 1

    @classmethod
    def _workflow_folder_paths(cls, workflows: list[WorkflowInfo]) -> set[str]:
        folders: set[str] = set()
        for workflow in workflows:
            path = ""
            for part in cls._tree_source_file(workflow).split("/")[:-1]:
                path = f"{path}/{part}" if path else part
                folders.add(path)
        return folders

    @classmethod
    def _workflow_revision_signature(cls, workflow: WorkflowInfo) -> tuple:
        """Return all descriptor data that can affect rendered UI behavior."""
        return (
            workflow.selector,
            workflow.name,
            workflow.rendered_name,
            cls._tree_source_file(workflow),
            workflow.builder_symbol,
            tuple(workflow.node_ids),
            tuple(
                sorted((parent, tuple(children)) for parent, children in workflow.graph.items())
            ),
            tuple(sorted(workflow.node_types.items())),
            tuple(sorted(workflow.display_names.items())),
            workflow.cron,
            workflow.next_run_at,
            workflow.last_run_at,
        )
