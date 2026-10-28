"""Page base: one page per route identity (Route.key); tabs/filters/selection update in place.

A page owns its streams (project change notices, run events) and stops them on unmount, so a
route change never leaks a reader. `draw()` is idempotent and called whenever the store or the
glyph clock marks the app dirty.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from .routes import Route
from .source import Frame, follow
from .widgets import Table, text_tabs


class Page(Vertical):
    TABS: tuple = ()
    DEFAULT_CSS = """
    Page { height: 1fr; background: $background; padding: 0 1; }
    Page > .title { height: auto; padding: 1 0 0 0; }
    Page > .tabs { height: 1; margin: 1 0 0 0; }
    Page > .note { height: auto; color: $text-muted; }
    Page Table { height: 1fr; }
    """

    def __init__(self, route: Route) -> None:
        super().__init__()
        self.route = route
        self._streams: list[threading.Event] = []
        self.stream_state: dict[str, str] = {}

    # ── overridable ──────────────────────────────────────────────────────────
    def draw(self) -> None:
        """Redraw from the store (cheap; called often)."""

    def meta(self) -> Text:
        """Right side of the top bar."""
        return Text()

    def focused_id(self) -> str | None:
        """The ID `y` copies and the status line describes."""
        table = self.active_table()
        return table.current[0] if table else None

    def describe(self) -> str:
        """Status-line text for the focused item (C4: replaces hover tooltips)."""
        ident = self.focused_id()
        return f"{ident} · y copy" if ident else ""

    def open_row(self, key: str, meta: object) -> None:
        """Enter on a table row."""

    def animating(self) -> bool:
        return False

    # ── shared helpers ───────────────────────────────────────────────────────
    @property
    def store(self):
        return self.app.store

    @property
    def pal(self) -> dict:
        return self.app.pal

    def set_route(self, route: Route) -> None:
        self.route = route
        self.draw()

    def tab_strip(self, hint: str = "") -> Text:
        return text_tabs(self.TABS, self.route.active_tab, "app.tab", self.pal, hint)

    def active_table(self) -> Table | None:
        tables = [table for table in self.query(Table) if table.display]
        focused = next((table for table in tables if table.has_focus), None)
        return focused or (tables[0] if tables else None)

    def title(self, heading: str, sub: str = "") -> Text:
        out = Text(heading, style=f"bold {self.pal['ink']}")
        if sub:
            out.append("  " + sub, style=self.pal["muted"])
        return out

    def more_line(self, *resources) -> Text:
        """console PageMore: 'Load more' while any list has a next cursor; inline errors."""
        out = Text()
        for res in resources:
            if res is None:
                continue
            if res.error:
                out.append(f"{res.error}  R retry  ", self.pal["danger"])
            elif res.loading and res.loaded:
                out.append("Loading more…  ", self.pal["muted"])
            elif res.cursor:
                out.append("m load more  ", self.pal["accent"])
        return out

    def loading_text(self, res, empty: str) -> str | None:
        if res is None:
            return empty
        if res.error and not res.items:
            return f"Unavailable · {res.error}"
        if res.pending:
            return "Loading…"
        return None if res.items else empty

    def stream(
        self,
        name: str,
        opener: Callable[[str | None, threading.Event], Iterator[Frame]],
        on_frame: Callable[[Frame], object],
    ) -> threading.Event:
        """Follow a resumable stream on a daemon thread until the page unmounts."""
        stop = threading.Event()
        self._streams.append(stop)

        def state(value: str) -> None:
            self.stream_state[name] = value
            self.app.dirty = True

        def run() -> None:
            follow(
                self.app.source,
                lambda cursor: opener(cursor, stop),
                on_frame,
                stop=stop,
                on_state=state,
            )

        threading.Thread(target=run, name=f"console-{name}", daemon=True).start()
        return stop

    def on_unmount(self) -> None:
        for stop in self._streams:
            stop.set()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = event.data_table
        if isinstance(table, Table):
            key, meta = table.current
            if key is not None:
                event.stop()
                self.open_row(key, meta)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.app.dirty = True


def text_widget(classes: str, id: str | None = None) -> Static:
    return Static("", classes=classes, id=id)
