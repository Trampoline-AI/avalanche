"""UIStore — single source of truth for all mutable TUI state."""

from __future__ import annotations

import time

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

    def __init__(self, provider: StateProvider) -> None:
        self.provider = provider

        # ── Workflow / Run ──────────────────────────────────────────
        self.workflows: list[WorkflowInfo] = provider.list_workflows()
        self.current_workflow: WorkflowInfo | None = None
        self.current_run: RunState | None = None
        self.run_pinned: bool = False  # True = user picked a run; False = follow latest
        self._runs_cache: list[RunState] = []

        # ── DAG layout (derived from current_workflow) ─────────────
        self.dag: SeqGroup | None = None
        self.all_nodes: list[DagNode] = []
        self.nav_grid: list[list[DagNode]] = []

        # ── Selection / Navigation ─────────────────────────────────
        self.selected_node: DagNode | None = None
        self.preferred_row: int = 0

        # ── Pane focus ─────────────────────────────────────────────
        self.focused_pane: str = "dag"  # "sidebar" | "dag" | "run-history" | "log"

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
        self.sidebar_selected_name: str = ""
        self.workflow_statuses: dict[str, RunStatus] = {}

        # Auto-expand all folders on init
        for p in self.workflows:
            parts = p.file_path.replace("\\", "/").split("/")[:-1]
            path = ""
            for part in parts:
                path = f"{path}/{part}" if path else part
                self.sidebar_expanded.add(path)

        # Set sidebar width to fit content
        self.sidebar_width = self._compute_sidebar_width()

        # Default to first workflow
        if self.workflows:
            self.switch_workflow(self.workflows[0])

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

    _runs_refresh_in_flight: bool = False

    @property
    def runs_for_current_workflow(self) -> list[RunState]:
        """Returns cached runs list — refreshed in background every ~1s."""
        if self.current_workflow is None:
            return []
        # Guard: if cache is from a different workflow, return empty until refreshed
        if self._runs_cache and self._runs_cache[0].flow_name != self.current_workflow.name:
            return []
        return self._runs_cache

    def _refresh_runs_cache(self) -> None:
        """Refresh runs cache in a background thread."""
        if self._runs_refresh_in_flight or self.current_workflow is None:
            return
        self._runs_refresh_in_flight = True
        provider = self.provider
        flow_name = self.current_workflow.name

        import threading

        def _do_refresh():
            try:
                self._runs_cache = provider.list_runs(flow_name)
            except Exception:
                pass
            finally:
                self._runs_refresh_in_flight = False

        threading.Thread(target=_do_refresh, daemon=True).start()

    @property
    def selected_node_elapsed_str(self) -> str:
        if self.selected_node and self.current_run:
            ns = self.current_run.nodes.get(self.selected_node.name)
            if ns and ns.elapsed is not None:
                return _fmt_time(ns.elapsed)
        return ""

    # ── Mutation: tick ──────────────────────────────────────────────

    def tick(self) -> None:
        """Advance frame and refresh workflow statuses."""
        self.frame += 1
        # Refresh provider data every ~1s in background threads (not every tick)
        if self.frame % 30 == 1:
            self._refresh_workflow_statuses()
            self._refresh_runs_cache()

    _status_refresh_in_flight: bool = False

    def _refresh_workflow_statuses(self) -> None:
        """Refresh sidebar workflow statuses in a background thread."""
        if self._status_refresh_in_flight:
            return
        self._status_refresh_in_flight = True
        provider = self.provider
        workflows = list(self.workflows)

        import threading

        def _do_refresh():
            try:
                statuses: dict[str, RunStatus] = {}
                for p in workflows:
                    runs = provider.list_runs(p.name)
                    if runs:
                        statuses[p.name] = runs[-1].status
                self.workflow_statuses = statuses
            except Exception:
                pass
            finally:
                self._status_refresh_in_flight = False

        threading.Thread(target=_do_refresh, daemon=True).start()

    # ── Mutation: workflow / run switching ──────────────────────────

    def switch_workflow(self, workflow: WorkflowInfo) -> None:
        """Switch to a different workflow — recompute DAG, reset selection."""
        self.current_workflow = workflow
        self.dag, self.all_nodes = workflow_to_layout(workflow)
        self.nav_grid = build_nav_grid(self.dag)
        self.selected_node = None
        self.preferred_row = 0
        self.sidebar_selected_name = workflow.name

        runs = self.provider.list_runs(workflow.name)
        self._runs_cache = runs  # seed cache so first frame isn't empty
        self.current_run = runs[-1] if runs else None
        self.run_pinned = False

    def switch_run(self, run: RunState) -> None:
        self.current_run = run
        self.run_pinned = True

    def deselect_run(self) -> None:
        """Unpin the current run — auto-follow will take over."""
        self.run_pinned = False
        self.selected_node = None

    def auto_follow_latest_run(self) -> None:
        """When not pinned, keep current_run pointing at the latest run."""
        if self.run_pinned or self.current_workflow is None:
            return
        runs = self.runs_for_current_workflow
        latest = runs[-1] if runs else None
        if latest and (self.current_run is None or latest.run_id != self.current_run.run_id):
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
            parts = p.file_path.replace("\\", "/").split("/")[:-1]
            depth = len(parts)
            # indent + icon + space + name
            line_len = 2 + 2 * depth + 4 + len(p.name)
            max_len = max(max_len, line_len)
            for i, part in enumerate(parts):
                folder_len = 2 + 2 * i + 4 + len(part)
                max_len = max(max_len, folder_len)
        return max_len + 4  # border (2) + small padding

    # ── Mutation: node selection / navigation ──────────────────────

    def select_node(self, node: DagNode) -> None:
        self.selected_node = node
        self.preferred_row = node.row

    def deselect_node(self) -> None:
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
        run_id = self.provider.start_run(self.current_workflow.name)
        self.current_run = self.provider.get_run(run_id)
        # Refresh cache so the new run appears immediately
        self._runs_cache = self.provider.list_runs(self.current_workflow.name)
        self.start_time = time.monotonic()
        return run_id

    def select_next_run(self) -> None:
        """Move down in run history (toward older runs)."""
        display = list(reversed(self.runs_for_current_workflow))
        if not display:
            return
        if self.current_run is None:
            self.current_run = display[0]
            self.run_pinned = True
            return
        for i, r in enumerate(display):
            if r.run_id == self.current_run.run_id:
                if i + 1 < len(display):
                    self.current_run = display[i + 1]
                    self.run_pinned = True
                return

    def select_prev_run(self) -> None:
        """Move up in run history (toward newer runs)."""
        display = list(reversed(self.runs_for_current_workflow))
        if not display:
            return
        if self.current_run is None:
            self.current_run = display[0]
            self.run_pinned = True
            return
        for i, r in enumerate(display):
            if r.run_id == self.current_run.run_id:
                if i > 0:
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
            parts = p.file_path.replace("\\", "/").split("/")
            folders, node = parts[:-1], tree
            for f in folders:
                if f not in node:
                    node[f] = {}
                node = node[f]
            node[f"__workflow__{p.name}"] = p

        flat: list[tuple[bool, str]] = []  # (is_folder, name_or_path)
        self._walk_flat(tree, "", flat)

        for idx, (is_folder, name) in enumerate(flat):
            if not is_folder and name == self.current_workflow.name:
                self.sidebar_cursor = idx
                return

    def _walk_flat(self, node: dict, path_prefix: str, flat: list) -> None:
        folders = sorted(k for k in node if not k.startswith("__workflow__"))
        workflows = sorted(
            (node[k] for k in node if k.startswith("__workflow__")),
            key=lambda p: p.name,
        )
        for folder_name in folders:
            folder_path = f"{path_prefix}/{folder_name}" if path_prefix else folder_name
            flat.append((True, folder_path))
            if folder_path in self.sidebar_expanded:
                self._walk_flat(node[folder_name], folder_path, flat)
        for p in workflows:
            flat.append((False, p.name))
