"""LogWidget — renders logs with search highlighting."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.widgets import RichLog

from ..models import LogLevel, NodeStatus
from ..theme import (
    DIM_STYLE,
    ICE_FROST,
    ICE_STEEL,
    ICE_WARN,
    LOG_LEVEL_STYLES,
    SEARCH_CURRENT,
    SEARCH_HIGHLIGHT,
    STATUS_STYLES,
)

_HEADER_STYLE = Style(color=ICE_STEEL, bold=True)

# 2 (indent) + 21 (ts+gap) + 17 (node) + 2 (gap) + 5 (level) + 2 (gap) = 49
_MSG_PREFIX_WIDTH = 49


def _wrap_message(msg: str, wrap_width: int) -> list[str]:
    """Split *msg* into chunks that fit the message column, or return as-is."""
    if wrap_width <= 0:
        return [msg]
    avail = wrap_width - _MSG_PREFIX_WIDTH
    if avail <= 0 or len(msg) <= avail:
        return [msg]
    return [msg[i : i + avail] for i in range(0, len(msg), avail)]


class LogWidget(RichLog):
    """Virtualized log view that renders only visible terminal rows."""

    DEFAULT_CSS = """
    LogWidget {
        width: 1fr;
        height: 1fr;
        min-width: 0;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        overflow-x: auto;
        overflow-y: auto;
        background: transparent;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(
            min_width=0,
            wrap=False,
            highlight=False,
            markup=False,
            auto_scroll=False,
            **kwargs,
        )
        self._match_lines: list[int] = []
        self._test_store = None
        self._wrap_width = 0
        self._projection_key: tuple | None = None
        self._logs_ref = None
        self._seen_log_count = 0
        self._visible_count = 0
        self._has_placeholder = False

    @staticmethod
    def render_header() -> Text:
        """Render the sticky column header."""
        text = Text()
        text.append("  ")
        text.append(f"{'Timestamp':<21}", _HEADER_STYLE)
        text.append(f"{'Node':<19}", _HEADER_STYLE)
        text.append(f"{'Level':<7}", _HEADER_STYLE)
        text.append("Message", _HEADER_STYLE)
        text.append("\n")
        text.append("  " + "─" * 54, DIM_STYLE)
        return text

    @property
    def wrap_width(self) -> int:
        return self._wrap_width

    @wrap_width.setter
    def wrap_width(self, value: int) -> None:
        value = max(0, value)
        if value != self._wrap_width:
            self._wrap_width = value
            self._projection_key = None

    def sync_from_store(self) -> None:
        """Append new entries or rebuild when the visible projection changes."""
        store = self._test_store or self.app.store
        selected_node = store.selected_node
        node_statuses = store.node_statuses
        selected_status = (
            node_statuses.get(selected_node.name, NodeStatus.PENDING) if selected_node else None
        )
        status_projection = (
            selected_status if selected_node else frozenset(node_statuses.items())
        )
        query = store.search_query
        projection_key = (
            store.current_run.run_id if store.current_run else None,
            selected_node.name if selected_node else None,
            status_projection,
            query,
            store.search_index if query else -1,
            self._wrap_width,
        )
        logs = store.logs
        snapshot_replaced = store.current_run is not None and logs is not self._logs_ref
        replacement_changed = (
            snapshot_replaced and len(logs) == self._seen_log_count and logs != self._logs_ref
        )

        if (
            projection_key != self._projection_key
            or len(logs) < self._seen_log_count
            or replacement_changed
        ):
            self._projection_key = projection_key
            self._rebuild(store)
            return

        if snapshot_replaced and len(logs) == self._seen_log_count:
            self._logs_ref = logs

        if len(logs) > self._seen_log_count:
            new_entries = logs[self._seen_log_count :]
            self._logs_ref = logs if store.current_run is not None else None
            self._seen_log_count = len(logs)
            if self._has_placeholder and any(
                self._entry_visible(entry, selected_node) for entry in new_entries
            ):
                self._rebuild(store)
                return
            self._append_entries(new_entries, store)

        store.set_match_count(len(self._match_lines))

    def _rebuild(self, store) -> None:
        self.clear()
        self._match_lines.clear()
        self._visible_count = 0
        self._has_placeholder = False
        self._logs_ref = store.logs if store.current_run is not None else None
        self._seen_log_count = len(store.logs)

        selected_node = store.selected_node
        if selected_node:
            status = store.node_statuses.get(selected_node.name, NodeStatus.PENDING)
            if status == NodeStatus.PENDING:
                self._write_placeholder("  Waiting for dependencies…", DIM_STYLE)
                store.set_match_count(0)
                return
            if status == NodeStatus.SKIPPED:
                self._write_placeholder(
                    "  Skipped — upstream dependency failed.",
                    Style(color=ICE_WARN),
                )
                store.set_match_count(0)
                return

        self._append_entries(store.logs, store)
        if self._visible_count == 0:
            status = (
                store.node_statuses.get(selected_node.name, NodeStatus.PENDING)
                if selected_node
                else None
            )
            if status == NodeStatus.RUNNING:
                self._write_placeholder("  Starting…", DIM_STYLE)
            elif selected_node is None:
                self._write_placeholder("  No logs yet.", DIM_STYLE)

        store.set_match_count(len(self._match_lines))

    def _append_entries(self, entries, store) -> None:
        selected_node = store.selected_node
        query = store.search_query.lower()
        node_statuses = store.node_statuses
        text = Text()
        line_count = 0
        first_line = len(self.lines)

        for entry in entries:
            if not self._entry_visible(entry, selected_node):
                continue
            line_count += self._append_entry(
                text,
                entry,
                query,
                store.search_index,
                node_statuses,
                first_line + line_count,
            )
            self._visible_count += 1

        if text:
            text.remove_suffix("\n")
            self.write(text, shrink=False, scroll_end=False)

    @staticmethod
    def _entry_visible(entry, selected_node) -> bool:
        return selected_node is None or entry.node_id in (
            selected_node.name,
            selected_node.display_name,
        )

    def _append_entry(
        self,
        text: Text,
        entry,
        query: str,
        search_index: int,
        node_statuses,
        first_line: int,
    ) -> int:
        ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        level_str = entry.level.value
        node_status = node_statuses.get(entry.node_id, NodeStatus.PENDING)
        level_style = LOG_LEVEL_STYLES.get(entry.level, Style())
        node_style = STATUS_STYLES[node_status]
        message_style = level_style if entry.level != LogLevel.INFO else Style(color=ICE_FROST)
        full_line = f"{ts} {entry.node_id} {level_str} {entry.message}"
        is_match = bool(query and query in full_line.lower())
        match_index = len(self._match_lines)
        is_current = is_match and search_index == match_index

        if is_match:
            self._match_lines.append(first_line)

        chunks = _wrap_message(entry.message, self._wrap_width)
        self._append_field(text, "  ", query, None, is_current)
        self._append_field(text, f"{ts}  ", query, DIM_STYLE, is_current)
        self._append_field(
            text,
            f"{entry.node_id:<17}",
            query,
            node_style,
            is_current,
        )
        self._append_field(text, "  ", query, None, is_current)
        self._append_field(
            text,
            f"{level_str:<5}",
            query,
            level_style,
            is_current,
        )
        self._append_field(text, "  ", query, None, is_current)
        self._append_field(text, chunks[0], query, message_style, is_current)
        text.append("\n")

        for chunk in chunks[1:]:
            text.append(" " * _MSG_PREFIX_WIDTH)
            self._append_field(text, chunk, query, message_style, is_current)
            text.append("\n")

        return len(chunks)

    @classmethod
    def _append_field(
        cls,
        text: Text,
        value: str,
        query: str,
        style: Style | None,
        is_current: bool,
    ) -> None:
        if query and query in value.lower():
            cls._append_highlighted(text, value, query, style or Style(), is_current)
        else:
            text.append(value, style)

    def _write_placeholder(self, message: str, style: Style) -> None:
        self._has_placeholder = True
        self.write(Text(message, style=style), shrink=False, scroll_end=False)

    def scroll_to_match(self) -> None:
        """Scroll the current search match to the center of the viewport."""
        store = self._test_store or self.app.store
        if store.search_index < 0 or store.search_index >= len(self._match_lines):
            return
        line = self._match_lines[store.search_index]
        self.scroll_to(y=max(0, line - self.size.height // 2), animate=False)

    def watch_scroll_x(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_x(old_value, new_value)
        try:
            self.screen.query_one("#log-header").styles.offset = (-new_value, 0)
        except Exception:
            pass

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        try:
            self.app._log_autoscroll = new_value >= self.max_scroll_y - 1
        except Exception:
            pass

    def on_click(self, event) -> None:
        store = self._test_store or self.app.store
        store.focused_pane = "log"

    @staticmethod
    def _append_highlighted(
        text: Text,
        msg: str,
        query: str,
        base_style: Style,
        is_current: bool,
    ) -> None:
        """Append msg with search matches highlighted."""
        highlight = SEARCH_CURRENT if is_current else SEARCH_HIGHLIGHT
        lower = msg.lower()
        position = 0
        while position < len(msg):
            index = lower.find(query, position)
            if index == -1:
                text.append(msg[position:], base_style)
                break
            if index > position:
                text.append(msg[position:index], base_style)
            text.append(msg[index : index + len(query)], highlight)
            position = index + len(query)
