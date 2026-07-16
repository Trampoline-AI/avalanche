"""RunDetail screen — DAG + logs + status bar for a single run."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header

from ..dag_layout import DagNode, SeqGroup, build_nav_grid, nav_move, workflow_to_layout
from ..models import NodeStatus, RunState, WorkflowInfo
from ..widgets.dag import DagWidget
from ..widgets.log_panel import LogWidget
from ..widgets.status_bar import StatusBar


class RunDetailScreen(Screen):
    """Displays DAG visualization + log panel + status bar for a run."""

    CSS = """
    RunDetailScreen {
        layout: vertical;
    }
    Header {
        dock: top;
        height: 1;
    }
    #dag-panel {
        height: auto;
        min-height: 5;
        padding: 1 0;
        border-bottom: solid $accent-darken-2;
    }
    #log-panel {
        height: 1fr;
        padding: 1 0 0 0;
        overflow-y: auto;
    }
    #status-bar {
        height: 1;
        dock: bottom;
        background: $primary-background;
    }
    """

    def __init__(
        self,
        workflow_info: WorkflowInfo,
        run: RunState | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.workflow_info = workflow_info
        self.run = run
        self.dag: SeqGroup
        self.all_nodes: list[DagNode]
        self.dag, self.all_nodes = workflow_to_layout(workflow_info)
        self.nav_grid = build_nav_grid(self.dag)
        self.selected: DagNode | None = None
        self.preferred_row = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield DagWidget(self.dag, self.all_nodes, id="dag-panel")
        yield LogWidget(id="log-panel")
        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.flow_name = self.workflow_info.rendered_name

    def get_statuses(self) -> dict[str, NodeStatus]:
        """Get current node statuses from the run."""
        if self.run is None:
            return {}
        return {nid: ns.status for nid, ns in self.run.nodes.items()}

    def update_widgets(self, frame: int, elapsed: float) -> None:
        """Called by the app tick to update all widgets."""
        statuses = self.get_statuses()

        dag_w = self.query_one("#dag-panel", DagWidget)
        dag_w.selected = self.selected
        dag_w.statuses = statuses
        dag_w.frame = frame

        log_w = self.query_one("#log-panel", LogWidget)
        log_w.selected_node = self.selected
        log_w.node_statuses = statuses
        if self.run:
            log_w.logs = list(self.run.logs)
        log_w.frame = frame

        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.node_name = self.selected.display_name if self.selected else ""
        status_bar.elapsed_str = _fmt_time(elapsed)

        if self.selected and self.run:
            ns = self.run.nodes.get(self.selected.name)
            if ns and ns.elapsed is not None:
                status_bar.node_elapsed_str = _fmt_time(ns.elapsed)
            else:
                status_bar.node_elapsed_str = ""
        else:
            status_bar.node_elapsed_str = ""

        status_bar.refresh()

    def select_node(self, node: DagNode) -> None:
        self.selected = node
        self.preferred_row = node.row

    def move(self, dx: int, dy: int) -> None:
        if self.selected is None:
            if self.all_nodes:
                self.selected = self.all_nodes[0]
                self.preferred_row = 0
            return
        self.selected, self.preferred_row = nav_move(
            self.nav_grid, self.selected, self.preferred_row, dx, dy
        )


def _fmt_time(secs: float) -> str:
    mins, s = divmod(secs, 60)
    if mins > 0:
        return f"{int(mins)}m{s:.0f}s"
    return f"{s:.1f}s"
