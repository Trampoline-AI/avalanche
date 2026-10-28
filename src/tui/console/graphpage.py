"""Base for the Flow and Run pages: DAG backdrop + caption, bottom tabbed panel, step drawer.

The selected run (flow page ?run=, or the run page's run) drives node statuses; its event
stream feeds the Timeline and marks the run stale, and the clock reloads it at most once per
tick (coalesced, so a burst of facts never queues a burst of reads).
"""

from __future__ import annotations

import threading

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from .dag import DagView
from .page import Page, text_widget
from .runview import DRAWER_TABS, artifact_rows, drawer_body, io_body, timeline_body
from .widgets import Table, text_tabs


class GraphPage(Page):
    BINDINGS = [
        Binding("z", "toggle_panel", "Panel", show=False),
        Binding("minus", "resize(-2)", show=False),
        Binding("plus,equals_sign", "resize(2)", show=False),
        Binding("escape", "close_drawer", show=False),
        Binding("D", "download", show=False),
        Binding("comma", "drawer_tab(-1)", show=False),
        Binding("full_stop", "drawer_tab(1)", show=False),
    ]
    DEFAULT_CSS = """
    GraphPage { padding: 0; }
    GraphPage #g-body { height: 1fr; }
    GraphPage #g-left { width: 1fr; }
    GraphPage #g-caption { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    GraphPage #g-panel { height: 14; border-top: solid $border; padding: 0 1; }
    GraphPage #g-panel.-collapsed { height: 2; }
    GraphPage #g-tabs { height: 1; }
    GraphPage #g-note { height: auto; color: $text-muted; }
    GraphPage #g-scroll { height: 1fr; }
    GraphPage #g-drawer { width: 56; border-left: solid $border; padding: 0 1; display: none; }
    GraphPage #g-drawer.-open { display: block; }
    """

    def __init__(self, route) -> None:
        super().__init__(route)
        self.frames: list[dict] = []
        self.panel_height = 14
        self.drawer_tab = "overview"
        self._run_stop: threading.Event | None = None
        self._streamed: str | None = None
        self._stale = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="g-body"):
            with Vertical(id="g-left"):
                yield text_widget("", id="g-caption")
                yield DagView(id="g-dag")
                with Vertical(id="g-panel"):
                    yield text_widget("", id="g-tabs")
                    yield text_widget("", id="g-note")
                    yield Table(id="g-table")
                    with VerticalScroll(id="g-scroll"):
                        yield Static("", id="g-text")
            with VerticalScroll(id="g-drawer"):
                yield Static("", id="g-drawer-text")

    # ── subclass hooks ───────────────────────────────────────────────────────
    def run_id(self) -> str | None:
        return self.route.run

    def flow_key(self) -> tuple | None:
        """(project, slug, deployment) whose definition the graph shows."""
        return None

    def caption(self) -> Text:
        return Text()

    def draw_tab(self, tab: str, table: Table) -> tuple[Text | None, Text | None]:
        """Return (note, body) for the active tab; fill `table` for table tabs (body None)."""
        return self.run_tab(tab, table)

    # ── shared data ──────────────────────────────────────────────────────────
    def run_data(self) -> dict | None:
        run = self.run_id()
        if not run:
            return None
        res = self.store.ensure(("run", self.route.project, run))
        return (res.data or {}).get("run") if res.loaded else None

    def flow_data(self) -> dict | None:
        key = self.flow_key()
        if not key or not all(key):
            return None
        res = self.store.ensure(("flow", *key))
        return res.data if res.loaded else None

    def _follow_run(self) -> None:
        run = self.run_id()
        if run == self._streamed:
            return
        if self._run_stop:
            self._run_stop.set()
        self._streamed, self.frames, self._run_stop = run, [], None
        if run:
            project = self.route.project
            self._run_stop = self.stream(
                "run",
                lambda cursor, stop: self.app.source.run_events(
                    project, run, stop=stop, last_event_id=cursor
                ),
                self._on_run_frame,
            )

    def _on_run_frame(self, frame) -> None:
        if frame.event == "fact":
            self.frames.append({"event": frame.event, "id": frame.id, "data": frame.data})
            del self.frames[:-400]
            self._stale = True
            self.app.dirty = True

    # ── draw ─────────────────────────────────────────────────────────────────
    def draw(self) -> None:
        self._follow_run()
        self.prefetch_caps()
        if self._stale and self.run_id():
            key = ("run", self.route.project, self.run_id())
            if not self.store.get(key).loading:
                self._stale = False
                self.store.reload(key)
        pal = self.pal
        self.query_one("#g-caption").update(self.caption())
        dag = self.query_one(DagView)
        flow_res_key = self.flow_key()
        flow = self.flow_data()
        dag.set_flow(flow)
        dag.set_run(self.run_data(), now=self.app.now())
        message = None
        if flow is None and flow_res_key and all(flow_res_key):
            res = self.store.get(("flow", *flow_res_key))
            message = f"Graph unavailable · {res.error}" if res.error else "Loading graph…"
        dag.draw(pal, self.app.frame, message)
        self.query_one("#g-tabs").update(self.tab_strip("[ ] switch · z panel · - + resize"))
        table = self.query_one("#g-table", Table)
        note, body = self.draw_tab(self.route.active_tab, table)
        table.display = body is None
        self.query_one("#g-scroll").display = body is not None
        self.query_one("#g-note").update(note or "")
        if body is not None:
            self.query_one("#g-text", Static).update(body)
        self._draw_drawer(flow)

    def run_tab(self, tab: str, table: Table) -> tuple[Text | None, Text | None]:
        pal, project, run = self.pal, self.route.project, self.run_id()
        if not run:
            return None, Text(
                "Select a run in the Runs tab to see its details.", style=pal["muted"]
            )
        if tab == "io":
            inputs = self.store.ensure(("inputs", project, run))
            outputs = self.store.ensure(("outputs", project, run))
            return None, io_body(inputs, outputs, pal)
        if tab == "timeline":
            state = self.stream_state.get("run", "connecting")
            return None, timeline_body(self.frames, f"Stream: {state}", pal)
        if tab == "artifacts":
            res = self.store.ensure(("artifacts", project, run))
            empty = self.loading_text(res, "") if not res.loaded else None
            rows = artifact_rows(res) if res.loaded else []
            if empty or not rows:
                return None, Text(
                    empty or "No artifacts recorded for this run.", style=pal["muted"]
                )
            table.set_rows(["Binding", "Path", "Bytes", "SHA-256", "Executable"], rows)
            return Text(
                "D download the selected file (verified size + sha256)", style=pal["hairline"]
            ), None
        return None, Text("")

    # ── drawer ───────────────────────────────────────────────────────────────
    def on_dag_view_inspect(self, event: DagView.Inspect) -> None:
        event.stop()
        if self.run_id():
            self.app.navigate(self.route.with_(node=event.node), push=False)

    def _draw_drawer(self, flow: dict | None) -> None:
        drawer = self.query_one("#g-drawer")
        node, run = self.route.node, self.run_data()
        drawer.set_class(bool(node and run), "-open")
        if not (node and run):
            return
        observation = None
        if self.drawer_tab == "trace":
            attempts = ((run.get("nodes") or {}).get(node) or {}).get("attempts") or []
            if attempts:
                observation = self.store.ensure(
                    (
                        "observation",
                        self.route.project,
                        run["run_id"],
                        node,
                        attempts[-1].get("g", 1),
                    )
                )
        head = text_tabs(DRAWER_TABS, self.drawer_tab, "drawer_pick", self.pal)
        head.append("\n, . switch · esc close\n\n", self.pal["hairline"])
        head.append_text(drawer_body(self.drawer_tab, node, run, flow, observation, self.pal))
        self.query_one("#g-drawer-text", Static).update(head)

    def action_drawer_pick(self, value: str) -> None:
        self.drawer_tab = value
        self.app.dirty = True

    def action_drawer_tab(self, step: int) -> None:
        values = [value for value, _ in DRAWER_TABS]
        self.drawer_tab = values[(values.index(self.drawer_tab) + step) % len(values)]
        self.app.dirty = True

    def action_close_drawer(self) -> None:
        if self.route.node:
            self.app.navigate(self.route.with_(node=None), push=False)
            self.query_one(DagView).focus()

    def action_download(self) -> None:
        if self.route.active_tab == "artifacts":
            _, meta = self.query_one("#g-table", Table).current
            self.app.download(self.route.project, self.run_id(), meta)

    def prefetch_caps(self) -> None:
        if self.run_id() and not self.app.source.read_only:
            self.store.ensure(("caps", self.route.project, self.run_id()))

    # ── panel ────────────────────────────────────────────────────────────────
    def action_toggle_panel(self) -> None:
        self.query_one("#g-panel").toggle_class("-collapsed")

    def action_resize(self, delta: int) -> None:
        self.panel_height = max(4, min(40, self.panel_height + delta))
        self.query_one("#g-panel").styles.height = self.panel_height

    def animating(self) -> bool:
        return self.query_one(DagView).running

    def describe(self) -> str:
        dag = self.query_one(DagView)
        if dag.has_focus and dag.selected is not None:
            return f"step {dag.selected.name} · enter inspect · arrows move"
        return super().describe()
