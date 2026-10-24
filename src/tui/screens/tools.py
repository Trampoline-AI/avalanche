"""Contextual inspection, log filtering and bounded extension resource pages."""

from __future__ import annotations

import json
from collections.abc import Mapping

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static, TextArea, Tree

from ..extensions import ExtensionSession
from ..models import LogLevel
from ..state import ExtensionPage, PanelDescriptor, TuiContext, TuiExtensionError, json_plain


class InspectionScreen(ModalScreen):
    DEFAULT_CSS = """
    InspectionScreen { align: center middle; }
    InspectionScreen > VerticalScroll {
        width: 90%; height: 85%; background: $surface;
        border: solid $accent; padding: 1 2;
    }
    InspectionScreen TextArea { height: 1fr; }
    """
    BINDINGS = [("escape", "dismiss", "Back")]

    def __init__(self, title: str, content: str) -> None:
        super().__init__()
        self.heading = title
        self.content = content

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label(self.heading)
            yield TextArea(self.content, read_only=True, id="inspection-content")
            yield Button("Back", id="inspection-back")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "inspection-back":
            self.dismiss()
            event.stop()


class LogFilterScreen(ModalScreen):
    DEFAULT_CSS = """
    LogFilterScreen { align: center middle; }
    LogFilterScreen > VerticalScroll {
        width: 70%; height: auto; max-height: 90%; background: $surface;
        border: solid $accent; padding: 1 2;
    }
    """
    BINDINGS = [("escape", "dismiss", "Back")]

    def compose(self) -> ComposeResult:
        store = self.app.store
        with VerticalScroll():
            yield Label("Log filters · loaded window only")
            yield Label("Node ID (blank follows selected DAG node)")
            yield Input(store.log_node_filter or "", id="filter-node")
            yield Select(
                [("All levels", "")] + [(level.value, level.value) for level in LogLevel],
                value=store.log_level_filter or "",
                allow_blank=False,
                id="filter-level",
            )
            yield Label("Channel")
            yield Input(store.log_channel_filter or "", id="filter-channel")
            yield Label("Attempt")
            yield Input(store.log_attempt_filter or "", id="filter-attempt")
            yield Button("Apply", id="filter-apply")
            yield Button("Clear filters", id="filter-clear")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        store = self.app.store
        if event.button.id == "filter-clear":
            store.log_node_filter = store.log_level_filter = None
            store.log_channel_filter = store.log_attempt_filter = None
        elif event.button.id == "filter-apply":
            store.log_node_filter = self.query_one("#filter-node", Input).value or None
            store.log_level_filter = self.query_one("#filter-level", Select).value or None
            store.log_channel_filter = self.query_one("#filter-channel", Input).value or None
            store.log_attempt_filter = self.query_one("#filter-attempt", Input).value or None
        else:
            return
        self.dismiss()
        self.app._refresh_widgets()


class ResourceScreen(ModalScreen):
    """Replace one bounded revision-pinned page; never accumulate artifact bodies."""

    DEFAULT_CSS = """
    ResourceScreen { align: center middle; }
    ResourceScreen > VerticalScroll {
        width: 95%; height: 90%; background: $surface; border: solid $accent; padding: 1;
    }
    ResourceScreen #resource-body { height: 1fr; }
    ResourceScreen Horizontal { height: 3; }
    """
    BINDINGS = [("escape", "dismiss", "Back")]

    def __init__(
        self, session: ExtensionSession, panel: PanelDescriptor, context: TuiContext
    ) -> None:
        super().__init__()
        self.session = session
        self.panel = panel
        self.context = context
        self._next_cursor: str | None = None
        self._revision: str | None = None
        self._requested_cursor: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label(f"{self.panel.slot} / {self.panel.label}")
            yield Static("Loading…", id="resource-status", markup=False)
            if self.panel.renderer == "table":
                yield DataTable(id="resource-body")
            elif self.panel.renderer == "tree":
                yield Tree(self.panel.label, id="resource-body")
            else:
                yield TextArea(read_only=True, id="resource-body")
            with Horizontal():
                yield Button("Reload", id="resource-reload")
                yield Button("Next page", id="resource-next", disabled=True)
                yield Button("Back", id="resource-back")

    def on_mount(self) -> None:
        self._load(None)

    def _load(self, cursor: str | None) -> None:
        if self.context != self.app.extension_context:
            self.query_one("#resource-status", Static).update(
                "Stale context · reopen this panel"
            )
            return
        self._requested_cursor = cursor
        self.query_one("#resource-next", Button).disabled = True
        self.query_one("#resource-reload", Button).disabled = True
        if not self.session.read(self.panel, cursor, self._accept, self._fail):
            self._fail(TuiExtensionError("unavailable", "Provider busy; reload when ready"))

    def _fail(self, error: TuiExtensionError) -> None:
        if not self.is_mounted:
            return
        self.query_one("#resource-status", Static).update(f"{error.code}: {error}")
        self.query_one("#resource-reload", Button).disabled = False

    def _accept(self, page: ExtensionPage) -> None:
        if not self.is_mounted or self.context != self.app.extension_context:
            return
        if self._requested_cursor is not None and self._revision != page.revision:
            self._fail(
                TuiExtensionError("cursor_expired", "Resource changed · reload from first page")
            )
            return
        self._revision = page.revision
        self._next_cursor = page.next_cursor
        self.query_one("#resource-status", Static).update(
            f"Revision {page.revision} · {len(page.items)} loaded rows"
            + (" · truncated/incomplete" if page.truncated else "")
            + (" · more pages" if page.next_cursor is not None else " · end of loaded snapshot")
        )
        self.query_one("#resource-next", Button).disabled = page.next_cursor is None
        self.query_one("#resource-reload", Button).disabled = False
        if self.panel.renderer == "table":
            table = self.query_one("#resource-body", DataTable)
            table.clear(columns=True)
            columns = self.panel.columns or (tuple(page.items[0]) if page.items else ("id",))
            table.add_columns(*columns)
            for item in page.items:
                table.add_row(*(json.dumps(json_plain(item.get(col))) for col in columns))
        elif self.panel.renderer == "tree":
            tree = self.query_one("#resource-body", Tree)
            tree.clear()
            remaining = [2000]

            def add(parent, value, depth: int = 0) -> None:
                if depth > 32 or remaining[0] <= 0:
                    parent.add_leaf("[tree truncated · inspect a narrower resource]")
                    return
                entries = value.items() if isinstance(value, Mapping) else enumerate(value)
                for key, child in entries:
                    if remaining[0] <= 0:
                        break
                    remaining[0] -= 1
                    if isinstance(child, (Mapping, list, tuple)):
                        add(parent.add(str(key)), child, depth + 1)
                    else:
                        parent.add_leaf(f"{key}: {json.dumps(child)}")

            add(tree.root, page.items)
            tree.root.expand()
        else:
            body = self.query_one("#resource-body", TextArea)
            if self.panel.renderer == "widget":
                body.text = (
                    "Trusted local widget factory not registered. No code was downloaded."
                )
            elif self.panel.renderer == "diff":
                import difflib

                sections = []
                for item in page.items:
                    if isinstance(item.get("diff"), str):
                        sections.append(item["diff"])
                    elif "before" in item and "after" in item:
                        before = json.dumps(json_plain(item["before"]), indent=2).splitlines()
                        after = json.dumps(json_plain(item["after"]), indent=2).splitlines()
                        sections.append(
                            "\n".join(
                                difflib.unified_diff(
                                    before,
                                    after,
                                    fromfile="before",
                                    tofile="after",
                                    lineterm="",
                                )
                            )
                        )
                    else:
                        sections.append(
                            "Diff data not exposed for this item:\n"
                            + json.dumps(json_plain(item), indent=2)
                        )
                body.text = "\n\n".join(sections)
            else:
                body.text = json.dumps(json_plain(page.items), indent=2)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "resource-back":
            self.dismiss()
        elif event.button.id == "resource-next" and self._next_cursor is not None:
            self._load(self._next_cursor)
        elif event.button.id == "resource-reload":
            self._revision = None
            self._load(None)
