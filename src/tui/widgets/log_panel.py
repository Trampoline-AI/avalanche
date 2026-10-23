"""Viewport-rendered streaming logs with incremental bounded projections."""

from __future__ import annotations

from collections import OrderedDict, deque

from rich.style import Style
from rich.text import Text
from textual.geometry import Size
from textual.scroll_view import ScrollView
from textual.strip import Strip

from ..models import LogEntry, LogWindow, StreamLogEntry
from ..theme import DIM_STYLE, ICE_FROST, ICE_STEEL, LOG_LEVEL_STYLES

_PREFIX_WIDTH = 49


class LogWidget(ScrollView, can_focus=False):
    """Retain row indexes, not Rich renderings of the entire log history.

    Search scans only the labelled loaded window. Query/selection changes repaint
    visible rows; appends extend the row index and evictions remove its prefix.
    """

    DEFAULT_CSS = """
    LogWidget {
        width: 1fr; height: 1fr; min-width: 0;
        overflow-x: auto; overflow-y: auto;
        background: transparent;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._test_store = None
        self._wrap_width = 0
        self._rows: deque[tuple[int, LogEntry, int]] = deque()
        self._cache: OrderedDict[int, Strip] = OrderedDict()
        self._logs_ref: LogWindow | None = None
        self._seen_total = 0
        self._seen_revision = -1
        self._filters: tuple | None = None
        self._search: tuple | None = None
        self._match_lines: list[int] = []
        self._marker = ""
        self._placeholder = "  No logs yet."
        self._maximum_width = 80

    @staticmethod
    def render_header() -> Text:
        return Text(
            f"  {'Timestamp':<21}{'Node':<19}{'Level':<7}Message",
            style=Style(color=ICE_STEEL, bold=True),
        )

    @property
    def wrap_width(self) -> int:
        return self._wrap_width

    @wrap_width.setter
    def wrap_width(self, value: int) -> None:
        value = max(0, value)
        if value != self._wrap_width:
            self._wrap_width = value
            self._filters = None

    @property
    def lines(self) -> list[Strip]:
        """Compatibility inspection; normal rendering never materializes this list."""
        return [self._render_row(i) for i in range(max(1, len(self._rows)))]

    @property
    def cache_bytes(self) -> int:
        return sum(len(strip.text.encode("utf-8")) + 512 for strip in self._cache.values())

    def _visible(self, entry: LogEntry, store) -> bool:
        selected = store.selected_node
        node = store.log_node_filter or (selected.name if selected else None)
        if node is not None and entry.node_id != node:
            return False
        if store.log_level_filter and entry.level.value != store.log_level_filter:
            return False
        if store.log_channel_filter:
            if (
                not isinstance(entry, StreamLogEntry)
                or entry.channel != store.log_channel_filter
            ):
                return False
        if store.log_attempt_filter:
            if (
                not isinstance(entry, StreamLogEntry)
                or entry.attempt != store.log_attempt_filter
            ):
                return False
        return True

    def _append(self, sequence: int, entry: LogEntry) -> None:
        width = max(1, self._wrap_width - _PREFIX_WIDTH)
        count = max(1, (len(entry.message) + width - 1) // width) if self._wrap_width else 1
        for chunk in range(count):
            self._rows.append((sequence, entry, chunk))
        self._maximum_width = max(self._maximum_width, _PREFIX_WIDTH + len(entry.message))

    def sync_from_store(self) -> None:
        store = self._test_store or self.app.store
        logs = store.logs
        if not isinstance(logs, LogWindow):
            raise TypeError("UIStore.logs must expose a bounded LogWindow")
        filters = (
            store.selected_run_id,
            store.selected_node.name if store.selected_node else None,
            store.log_node_filter,
            store.log_level_filter,
            store.log_channel_filter,
            store.log_attempt_filter,
            self._wrap_width,
        )
        total = logs.evicted + len(logs)
        if (
            logs is not self._logs_ref
            and self._logs_ref is not None
            and filters == self._filters
            and logs.evicted == self._logs_ref.evicted
            and logs == self._logs_ref
        ):
            # A detached but equal provider snapshot does not invalidate the row
            # index or rendered viewport. Reuse both, while tracking the new
            # window's revision for subsequent in-place stream appends.
            self._logs_ref = logs
            self._seen_revision = logs.revision
        changed = logs is not self._logs_ref or filters != self._filters
        if changed:
            self._rows.clear()
            self._maximum_width = 80
            start = 0
        else:
            if logs.revision == self._seen_revision and self._search == (
                store.search_query,
                store.search_index,
            ):
                return
            start = max(0, self._seen_total - logs.evicted)
            while self._rows and self._rows[0][0] < logs.evicted:
                self._rows.popleft()
        for index in range(start, len(logs)):
            entry = logs[index]
            if self._visible(entry, store):
                self._append(logs.evicted + index, entry)
        self._marker = (
            f"  [evicted records 1–{logs.evicted:,}; search loaded window only]"
            if logs.evicted
            else ""
        )
        self._logs_ref = logs
        self._seen_total = total
        self._seen_revision = logs.revision
        self._filters = filters
        self._search = (store.search_query, store.search_index)
        query = store.search_query.casefold()
        self._match_lines = []
        previous = -1
        for index, (sequence, entry, _) in enumerate(self._rows):
            if sequence != previous and query and query in self._plain(entry).casefold():
                self._match_lines.append(index + bool(self._marker))
            previous = sequence
        store.set_match_count(len(self._match_lines))
        self._cache.clear()
        self.virtual_size = Size(
            self.size.width if self._wrap_width else self._maximum_width,
            max(1, len(self._rows) + bool(self._marker)),
        )
        self.refresh()

    @staticmethod
    def _plain(entry: LogEntry) -> str:
        return (
            f"{entry.timestamp:%Y-%m-%d %H:%M:%S} "
            f"{entry.node_id} {entry.level.value} {entry.message}"
        )

    def _render_row(self, y: int) -> Strip:
        cached = self._cache.get(y)
        if cached is not None:
            self._cache.move_to_end(y)
            return cached
        if self._marker and y == 0:
            text = Text(self._marker, DIM_STYLE)
        else:
            index = y - bool(self._marker)
            if not self._rows:
                text = Text(self._placeholder, DIM_STYLE)
            elif index < 0 or index >= len(self._rows):
                return Strip.blank(self.size.width)
            else:
                _, entry, chunk = self._rows[index]
                if self._wrap_width:
                    width = max(1, self._wrap_width - _PREFIX_WIDTH)
                    message = entry.message[chunk * width : (chunk + 1) * width]
                else:
                    message = entry.message
                text = Text()
                if chunk == 0:
                    text.append(f"  {entry.timestamp:%Y-%m-%d %H:%M:%S}  ", DIM_STYLE)
                    text.append(f"{entry.node_id:<17}  ", Style(color=ICE_FROST))
                    text.append(f"{entry.level.value:<5}  ", LOG_LEVEL_STYLES[entry.level])
                else:
                    text.append(" " * _PREFIX_WIDTH)
                text.append(message)
                store = self._test_store or self.app.store
                if store.search_query:
                    text.highlight_words(
                        [store.search_query], "bold black on yellow", case_sensitive=False
                    )
                if (
                    0 <= store.search_index < len(self._match_lines)
                    and self._match_lines[store.search_index] == y
                ):
                    text.stylize("reverse")
        width = max(self.size.width, min(self._maximum_width, 16384))
        options = self.app.console.options.update(width=max(1, width), height=1)
        strip = Strip.from_lines(self.app.console.render_lines(text, options))[0]
        self._cache[y] = strip
        while len(self._cache) > 256:
            self._cache.popitem(last=False)
        return strip

    def render_line(self, y: int) -> Strip:
        return self._render_row(y + int(self.scroll_y)).crop_extend(
            int(self.scroll_x), int(self.scroll_x) + self.size.width, self.rich_style
        )

    def scroll_to_match(self) -> None:
        store = self._test_store or self.app.store
        if 0 <= store.search_index < len(self._match_lines):
            self.scroll_to(
                y=max(0, self._match_lines[store.search_index] - self.size.height // 2),
                animate=False,
            )

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.is_mounted:
            self.app._log_autoscroll = new_value >= self.max_scroll_y - 1

    def on_click(self, event) -> None:
        self.app.store.focused_pane = "log"
