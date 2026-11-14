"""RunDetailWidget — shows details about the selected run."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from ..state import status_label
from ..theme import (
    DIM_STYLE,
    ICE_FROST,
    ICE_STEEL,
    node_status_display,
    node_wait_kind,
)

_VALUE_STYLE = Style(color=ICE_FROST)


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
            kind = node_wait_kind(store.current_workflow, ns.node_id)
            icon, ns_style = node_status_display(ns.status, kind, frame)

            text.append(f" {icon} ", ns_style)
            text.append(f"{ns.name:<18}", _VALUE_STYLE)
            text.append(f"{status_label(ns.status):<9}", ns_style)
            if ns.elapsed is not None:
                text.append(f"{_fmt_time(ns.elapsed):>6}", DIM_STYLE)
            text.append("\n")

        return text


def _fmt_time(secs: float) -> str:
    mins, s = divmod(secs, 60)
    if mins > 0:
        return f"{int(mins)}m{s:.0f}s"
    return f"{s:.1f}s"
