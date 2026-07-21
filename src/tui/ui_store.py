"""UIStore — single source of truth for all mutable TUI state."""

from __future__ import annotations

import json
import time
from queue import SimpleQueue
from typing import Any, Literal

from .dag_layout import DagNode, SeqGroup, build_nav_grid, nav_move, workflow_to_layout
from .models import LogEntry, NodeStatus, RunState, RunStatus, WorkflowInfo
from .state import StateProvider


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

    def __init__(self, provider: StateProvider, *, defer_initial_catalog: bool = False) -> None:
        self.provider = provider

        # ── Workflow / Run ──────────────────────────────────────────
        self.workflows: list[WorkflowInfo] = (
            [] if defer_initial_catalog else provider.list_workflows()
        )
        self.current_workflow: WorkflowInfo | None = None
        self.current_run: RunState | None = None
        self.run_pinned: bool = False  # True = user picked a run; False = follow latest
        self._runs_cache: list[RunState] = []
        self._background_updates: SimpleQueue[tuple[str, Any]] = SimpleQueue()
        self._runs_refresh_in_flight: set[str] = set()
        self._status_refresh_in_flight = False
        self._catalog_refresh_in_flight = False
        self._start_run_in_flight = False
        self._start_request_generation = 0
        self._run_interaction_generation = 0
        self._workflow_context_epoch = 0
        self._run_data_revisions: dict[str, int] = {}
        self.run_error: str = ""
        self.catalog_revision = 0

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
        self.trace_inspector_tab: Literal["trace", "metadata"] = "trace"
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
                if getattr(provider, "connected", True) is False:
                    runs = None
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
        envelope = self.selected_agent_trace_envelope
        if envelope is None:
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

    def _refresh_workflow_catalog(self) -> None:
        if self._catalog_refresh_in_flight:
            return
        self._catalog_refresh_in_flight = True
        provider = self.provider

        import threading

        def _do_refresh():
            try:
                workflows = provider.list_workflows()
                if getattr(provider, "connected", True) is False:
                    workflows = None
            except Exception:
                workflows = None
            self._background_updates.put(("catalog", workflows))

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
                    if getattr(provider, "connected", True) is False:
                        statuses = None
                        break
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
        self.close_trace_inspector()
        self.current_workflow = workflow
        self.dag, self.all_nodes = workflow_to_layout(workflow)
        self.nav_grid = build_nav_grid(self.dag)
        self.selected_node = None
        self.preferred_row = 0
        self.sidebar_selected_id = workflow.selector

        # Never show the previous workflow's runs while the new history loads.
        self._runs_cache = []
        self.current_run = None
        self.run_pinned = False
        self._refresh_runs_cache()

    def switch_run(self, run: RunState) -> None:
        self._run_interaction_generation += 1
        self.close_trace_inspector()
        self.current_run = run
        self.run_pinned = True

    def deselect_run(self) -> None:
        """Unpin the current run — auto-follow will take over."""
        self._run_interaction_generation += 1
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

    def toggle_trace_inspector_tab(self) -> None:
        if not self.trace_inspector_open:
            return
        self.trace_inspector_tab = (
            "metadata" if self.trace_inspector_tab == "trace" else "trace"
        )

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
        run_id = self.provider.start_run(self.current_workflow.selector)
        if not run_id:
            self.run_error = getattr(self.provider, "last_error", "") or "Run failed to start"
            return None
        selector = self.current_workflow.selector
        self.current_run = self.provider.get_run(run_id)
        # Refresh cache so the new run appears immediately
        self._runs_cache = self.provider.list_runs(selector)
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
                    error = getattr(provider, "last_error", "") or "Run failed to start"
                else:
                    run = provider.get_run(run_id)
                    if getattr(provider, "connected", True) is False:
                        error = (
                            getattr(provider, "last_error", "")
                            or "Failed to load the started run"
                        )
                    else:
                        runs = provider.list_runs(workflow_selector)
                        if getattr(provider, "connected", True) is False:
                            error = (
                                getattr(provider, "last_error", "")
                                or "Failed to refresh runs after starting"
                            )
                        elif run is None:
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
            self.current_run = display[0]
            self.run_pinned = True
            return
        for i, r in enumerate(display):
            if r.run_id == self.current_run.run_id:
                if i + 1 < len(display):
                    self._run_interaction_generation += 1
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
            self.current_run = display[0]
            self.run_pinned = True
            return
        for i, r in enumerate(display):
            if r.run_id == self.current_run.run_id:
                if i > 0:
                    self._run_interaction_generation += 1
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

    def _apply_background_updates(self) -> None:
        while not self._background_updates.empty():
            kind, payload = self._background_updates.get()
            if kind == "catalog":
                self._catalog_refresh_in_flight = False
                if payload is not None:
                    self._reconcile_workflows(payload)
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
                    changed = self._runs_cache != runs
                    self._runs_cache = runs
                    if not self.run_pinned:
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
            elif kind == "run":
                run = payload
                workflow = next(
                    (item for item in self.workflows if self._run_matches(run, item)),
                    None,
                )
                if workflow is None:
                    continue
                selector = workflow.selector
                current_match = (
                    self.current_run
                    if self.current_run
                    and self.current_run.run_id == run.run_id
                    and self.current_workflow is not None
                    and self.current_workflow.selector == selector
                    else None
                )
                cache_index = next(
                    (
                        index
                        for index, cached in enumerate(self._runs_cache)
                        if cached.run_id == run.run_id and self._run_matches(cached, workflow)
                    ),
                    None,
                )
                cached_match = (
                    self._runs_cache[cache_index] if cache_index is not None else None
                )
                equivalents = [
                    item for item in (current_match, cached_match) if item is not None
                ]
                if not equivalents or any(item != run for item in equivalents):
                    self._advance_run_data_revision(selector)
                if current_match is not None:
                    self.current_run = run
                if cache_index is not None:
                    self._runs_cache[cache_index] = run
                    if cache_index == len(self._runs_cache) - 1:
                        self.workflow_statuses[selector] = run.status
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
                self._runs_cache = runs or ([] if run is None else [run])
                self.current_run = run or next(
                    (item for item in self._runs_cache if item.run_id == run_id), None
                )
                self.run_pinned = True
                self.start_time = time.monotonic()
                self._advance_run_data_revision(selector)
                if run_id and self._runs_cache:
                    self.workflow_statuses[selector] = self._runs_cache[-1].status

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
            self._runs_cache = []
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
                self._runs_cache = []
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
