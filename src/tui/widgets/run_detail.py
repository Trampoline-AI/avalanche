"""RunDetailWidget — shows details about the selected run."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from ..models import NodeStatus
from ..theme import (
    DIM_STYLE,
    ICE_BRIGHT,
    ICE_FAIL,
    ICE_FROST,
    ICE_PURPLE,
    ICE_STEEL,
    ICE_TEAL,
    SPINNER_FRAMES,
)

_VALUE_STYLE = Style(color=ICE_FROST)

_NODE_STATUS_CHARS: dict[NodeStatus, str] = {
    NodeStatus.PENDING: "○",
    NodeStatus.RUNNING: "◐",
    NodeStatus.SUCCESS: "✓",
    NodeStatus.FAILED: "✗",
    NodeStatus.SKIPPED: "⊘",
}

_NODE_STATUS_STYLES: dict[NodeStatus, Style] = {
    NodeStatus.PENDING: Style(color=ICE_STEEL),
    NodeStatus.RUNNING: Style(color=ICE_BRIGHT, bold=True),
    NodeStatus.SUCCESS: Style(color=ICE_TEAL),
    NodeStatus.FAILED: Style(color=ICE_FAIL),
    NodeStatus.SKIPPED: Style(color=ICE_PURPLE),
}


class RunDetailWidget(Static):
    """Displays details about the currently selected run."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._test_store = None

    @staticmethod
    def render_header() -> Text:
        """Render the sticky column header."""
        _hdr = Style(color=ICE_STEEL, bold=True)
        text = Text()
        text.append("\n")
        text.append("   ", _hdr)
        text.append(f"{'Node':<18}", _hdr)
        text.append(f"{'Status':<9}", _hdr)
        text.append("Time", _hdr)
        text.append("\n")
        text.append("  " + "─" * 35, DIM_STYLE)
        return text

    def render(self) -> Text:
        store = self._test_store or self.app.store
        run = store.current_run
        frame = store.frame

        text = Text()

        if not run:
            text.append("  Select a run to view details.\n", DIM_STYLE)
            return text

        # Node rows
        for ns in run.nodes.values():
            if ns.status == NodeStatus.RUNNING:
                icon = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
            else:
                icon = _NODE_STATUS_CHARS[ns.status]
            ns_style = _NODE_STATUS_STYLES[ns.status]

            text.append(f" {icon} ", ns_style)
            text.append(f"{ns.name:<18}", _VALUE_STYLE)
            text.append(f"{ns.status.value:<9}", ns_style)
            if ns.elapsed is not None:
                text.append(f"{_fmt_time(ns.elapsed):>6}", DIM_STYLE)
            text.append("\n")

        return text


def _fmt_time(secs: float) -> str:
    mins, s = divmod(secs, 60)
    if mins > 0:
        return f"{int(mins)}m{s:.0f}s"
    return f"{s:.1f}s"
