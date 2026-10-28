"""Flow/run graph: Avalanche's DAG layout + renderer, recoloured, keyboard/mouse selection."""

from __future__ import annotations

import time

from rich.text import Text
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.message import Message
from textual.widgets import Static

from ..dag_layout import build_nav_grid, nav_move, workflow_to_layout
from ..models import NodeStatus, WorkflowInfo
from .dagfit import render_fitted
from .theme import recolor

NODE_STATUS = {
    "accepted": NodeStatus.SUCCESS,
    "running": NodeStatus.RUNNING,
    "failed": NodeStatus.FAILED,
    "indeterminate": NodeStatus.FAILED,
    "ready": NodeStatus.PENDING,
    "intended": NodeStatus.PENDING,
    "waiting": NodeStatus.PENDING,
    "cancelled": NodeStatus.SKIPPED,
    "expired": NodeStatus.SKIPPED,
}


def workflow_info(flow: dict) -> WorkflowInfo:
    return WorkflowInfo(
        name=flow["flow_slug"],
        file_path=flow.get("definition_file") or "",
        node_ids=list(flow.get("node_ids") or []),
        graph={key: list(value) for key, value in (flow.get("graph") or {}).items()},
        node_types=dict(flow.get("node_types") or {}),
        display_names=dict(flow.get("display_names") or {}),
        agent_node_ids=list(flow.get("agent_nodes") or {}),
        display_name=flow.get("workflow_name") or "",
    )


def node_statuses(run: dict | None, now: float | None = None) -> tuple[dict, dict]:
    statuses, elapsed = {}, {}
    if not run:
        return statuses, elapsed
    now = time.time() if now is None else now
    for inv, node in (run.get("nodes") or {}).items():
        statuses[inv] = NODE_STATUS.get(node.get("state") or "", NodeStatus.PENDING)
        attempts = node.get("attempts") or []
        last = attempts[-1] if attempts else {}
        started, ended = last.get("started_at"), last.get("ended_at")
        if isinstance(started, (int, float)):
            elapsed[inv] = (ended if isinstance(ended, (int, float)) else now) - started
    return statuses, elapsed


class DagView(ScrollableContainer, can_focus=True):
    """Full-bleed graph backdrop. Arrows move between steps, enter opens the step drawer."""

    BINDINGS = [
        Binding("left", "move(-1,0)", "←", show=False),
        Binding("right", "move(1,0)", "→", show=False),
        Binding("up", "move(0,-1)", "↑", show=False),
        Binding("down", "move(0,1)", "↓", show=False),
        Binding("enter", "inspect", "Inspect step", show=False),
    ]
    DEFAULT_CSS = """
    DagView { height: 1fr; background: $panel; padding: 1 1; scrollbar-size: 1 1; }
    DagView:focus { background-tint: $primary 4%; }
    DagView > Static { width: auto; background: $panel; }
    """

    class Inspect(Message):
        def __init__(self, node: str) -> None:
            super().__init__()
            self.node = node

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.canvas = Static("", id="dag-canvas")
        self.flow: dict | None = None
        self.graph = None
        self.nodes: list = []
        self.grid: list = []
        self.selected = None
        self.preferred_row = 0
        self.regions: list[tuple[int, int, int, object]] = []
        self.statuses: dict = {}
        self.elapsed: dict = {}
        self.pal: dict = {}
        self.frame = 0

    def compose(self):
        yield self.canvas

    def set_flow(self, flow: dict | None) -> None:
        identity = (
            (flow or {}).get("flow_slug"),
            (flow or {}).get("deployment_id"),
            tuple((flow or {}).get("node_ids") or ()),
        )
        if identity == getattr(self, "_identity", None):
            return
        self._identity = identity
        self.flow = flow
        if flow and flow.get("node_ids"):
            self.graph, self.nodes = workflow_to_layout(workflow_info(flow))
            self.grid = build_nav_grid(self.graph)
        else:
            self.graph, self.nodes, self.grid = None, [], []
        self.selected = None

    def set_run(self, run: dict | None, now: float | None = None) -> None:
        self.statuses, self.elapsed = node_statuses(run, now)

    def select(self, name: str | None) -> None:
        self.selected = next((node for node in self.nodes if node.name == name), None)

    @property
    def running(self) -> bool:
        return any(status is NodeStatus.RUNNING for status in self.statuses.values())

    def draw(self, pal: dict, frame: int, message: str | None = None) -> None:
        self.pal, self.frame = pal, frame
        if self.graph is None:
            self.canvas.update(Text(message or "No graph to show.", style=pal["muted"]))
            return
        # Fit the pane (fold into rows when wider), then centre it: the graph is the backdrop.
        width = max(20, self.size.width - 2) if self.size.width else 10_000
        height = max(1, self.size.height - 2)
        lines, folded = render_fitted(
            self.graph, self.statuses, frame, self.selected, self.elapsed, width
        )
        if folded:
            lines = lines + [
                Text(""),
                Text(
                    "Also depends on: " + ", ".join(f"{src} → {dst}" for src, dst in folded),
                    style="#b898e0",
                ),
            ]
        graph_w = max((line.cell_len for line in lines), default=0)
        left = max(0, (width - graph_w) // 2)
        top = max(0, (height - len(lines)) // 2)
        self.regions = []
        for node in self.nodes:
            if (
                node.virtual
                or node.render_row is None
                or node.render_col is None
                or node.render_row >= len(lines)
            ):
                continue
            needle = f" {node.display_name} "
            start = node.render_col
            end = start + len(needle)
            plain = lines[node.render_row].plain
            if end < len(plain) and plain[end] == "(":
                close = plain.find(") ", end)
                end = close + 2 if close >= 0 else end
            self.regions.append((node.render_row + top, start + left, end + left, node))
        text = Text(no_wrap=True)
        text.append("\n" * top)
        for index, line in enumerate(lines):
            if index:
                text.append("\n")
            text.append(" " * left)
            text.append_text(recolor(line, pal))
        self.canvas.update(text)

    def on_resize(self, event) -> None:
        if self.pal:
            self.draw(self.pal, self.frame)

    def on_click(self, event) -> None:
        offset = (
            event.get_content_offset(self.canvas)
            if hasattr(event, "get_content_offset")
            else None
        )
        x, y = (offset.x, offset.y) if offset else (event.x, event.y)
        for row, start, end, node in self.regions:
            if y == row and start <= x < end:
                self.selected = node
                self.focus()
                self.post_message(self.Inspect(node.name))
                self.draw(self.pal, self.frame)
                return

    def action_move(self, dx: int, dy: int) -> None:
        if not self.grid:
            return
        if self.selected is None:
            self.selected = self.grid[0][0]
        else:
            self.selected, self.preferred_row = nav_move(
                self.grid, self.selected, self.preferred_row, dx, dy
            )
        self.draw(self.pal, self.frame)
        for row, start, _, node in self.regions:
            if node is self.selected:
                self.scroll_to(
                    x=max(0, start - self.size.width // 3),
                    y=max(0, row - self.size.height // 2),
                    animate=False,
                )

    def action_inspect(self) -> None:
        if self.selected is not None:
            self.post_message(self.Inspect(self.selected.name))
