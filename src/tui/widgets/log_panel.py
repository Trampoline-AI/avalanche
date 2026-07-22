"""LogWidget — renders logs with search highlighting."""

from __future__ import annotations

import json

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from ..models import LogLevel, NodeStatus
from ..theme import (
    DIM_STYLE,
    ICE_BRIGHT,
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


class LogWidget(Static):
    """Renders logs — all logs with selected node's entries highlighted."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._match_lines: list[int] = []
        self._test_store = None
        self.wrap_width: int = 0  # 0 = no wrap; >0 = wrap messages to this width

    @staticmethod
    def render_header() -> Text:
        """Render the sticky column header."""
        text = Text()
        text.append("  ")
        text.append(f"{'Timestamp':<21}", _HEADER_STYLE)
        text.append(f"{'Node':<19}", _HEADER_STYLE)
        text.append(f"{'Level':<7}", _HEADER_STYLE)
        text.append("Message", _HEADER_STYLE)
        # 2 + 21 + 19 + 7 + 7 = 56 columns of header
        text.append("\n")
        text.append("  " + "─" * 54, DIM_STYLE)
        return text

    def render(self) -> Text:
        store = self._test_store or self.app.store
        text = Text()

        selected_node = store.selected_node
        node_statuses = store.node_statuses
        logs = store.logs
        search_query = store.search_query
        search_current = store.search_index
        frame = store.frame

        # Status messages for specific node states
        if selected_node:
            status = node_statuses.get(selected_node.name, NodeStatus.PENDING)
            if status == NodeStatus.PENDING:
                text.append("  Waiting for dependencies…\n", DIM_STYLE)
                return text
            if status == NodeStatus.SKIPPED:
                node_state = (
                    store.current_run.nodes.get(selected_node.name)
                    if store.current_run is not None
                    else None
                )
                if node_state is not None and node_state.reason is not None:
                    text.append(f"  Skipped — {node_state.reason}\n", Style(color=ICE_WARN))
                    if node_state.metadata is not None:
                        metadata = json.dumps(node_state.metadata, sort_keys=True)
                        text.append(f"  Metadata: {metadata}\n", DIM_STYLE)
                else:
                    text.append(
                        "  Skipped — upstream dependency failed.\n",
                        Style(color=ICE_WARN),
                    )
                return text

        # Filter entries for selected node
        visible = logs
        if selected_node:
            visible = [
                e for e in logs if e.node_id in (selected_node.name, selected_node.display_name)
            ]

        if not visible and selected_node:
            status = node_statuses.get(selected_node.name, NodeStatus.PENDING)
            if status == NodeStatus.RUNNING:
                text.append("  Starting…\n", DIM_STYLE)
                return text

        if not visible:
            text.append("  No logs yet.\n", DIM_STYLE)
            return text

        # Render entries with search highlighting
        self._match_lines.clear()
        query = search_query.lower()

        for line_idx, entry in enumerate(visible):
            ts = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            level_str = entry.level.value
            node_status = node_statuses.get(entry.node_id, NodeStatus.PENDING)
            ls = LOG_LEVEL_STYLES.get(entry.level, Style())
            ns = STATUS_STYLES[node_status]
            msg_style = ls if entry.level != LogLevel.INFO else Style(color=ICE_FROST)

            full_line = f"{ts} {entry.node_id} {level_str} {entry.message}"
            is_match = query and query in full_line.lower()
            if is_match:
                self._match_lines.append(line_idx)

            is_current = is_match and search_current == len(self._match_lines) - 1

            text.append("  ")
            node_padded = f"{entry.node_id:<17}"
            level_padded = f"{level_str:<5}"

            msg_chunks = _wrap_message(entry.message, self.wrap_width)

            if is_match:
                self._append_highlighted(text, f"{ts}  ", query, DIM_STYLE, is_current)
                self._append_highlighted(text, node_padded, query, ns, is_current)
                text.append("  ")
                self._append_highlighted(text, level_padded, query, ls, is_current)
                text.append("  ")
                self._append_highlighted(text, msg_chunks[0], query, msg_style, is_current)
                text.append("\n")
                for chunk in msg_chunks[1:]:
                    text.append(" " * _MSG_PREFIX_WIDTH)
                    self._append_highlighted(text, chunk, query, msg_style, is_current)
                    text.append("\n")
            else:
                text.append(f"{ts}  ", DIM_STYLE)
                text.append(node_padded, ns)
                text.append(f"  {level_padded}  ", ls)
                text.append(f"{msg_chunks[0]}\n", msg_style)
                for chunk in msg_chunks[1:]:
                    text.append(" " * _MSG_PREFIX_WIDTH)
                    text.append(f"{chunk}\n", msg_style)

        # Blinking cursor if selected node is running
        if selected_node and not query:
            status = node_statuses.get(selected_node.name, NodeStatus.PENDING)
            if status == NodeStatus.RUNNING:
                if int(frame / 5) % 2 == 0:
                    text.append("  █\n", Style(color=ICE_BRIGHT))

        # Write back match count for StatusBar
        store.set_match_count(len(self._match_lines))

        return text

    def on_click(self, event) -> None:
        store = self._test_store or self.app.store
        store.focused_pane = "log"

    _HEADER_LINES = 0  # headers now in separate widget

    def scroll_to_match(self) -> None:
        """Scroll the parent container so the current search match is centered."""
        store = self._test_store or self.app.store
        if store.search_index < 0 or store.search_index >= len(self._match_lines):
            return
        line = self._match_lines[store.search_index]
        y = self._HEADER_LINES + line
        container = self.parent
        if container is not None:
            half = container.content_size.height // 2
            container.scroll_to(y=max(0, y - half), animate=False)

    @staticmethod
    def _append_highlighted(
        text: Text,
        msg: str,
        query: str,
        base_style: Style,
        is_current: bool,
    ) -> None:
        """Append msg with search matches highlighted."""
        hl = SEARCH_CURRENT if is_current else SEARCH_HIGHLIGHT
        lower = msg.lower()
        pos = 0
        while pos < len(msg):
            idx = lower.find(query, pos)
            if idx == -1:
                text.append(msg[pos:], base_style)
                break
            if idx > pos:
                text.append(msg[pos:idx], base_style)
            text.append(msg[idx : idx + len(query)], hl)
            pos = idx + len(query)
