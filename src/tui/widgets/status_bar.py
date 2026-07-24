"""StatusBar widget — bottom bar showing breadcrumb and search."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from ..models import RunStatus
from ..theme import (
    DIM_STYLE,
    ICE_BRIGHT,
    ICE_FAIL,
    ICE_FROST,
    ICE_STEEL,
    ICE_TEAL,
    SPINNER_FRAMES,
)

_SEP = " › "
_SEP_STYLE = Style(color=ICE_STEEL)

_STATUS_STYLES: dict[RunStatus, Style] = {
    RunStatus.PENDING: Style(color=ICE_STEEL),
    RunStatus.RUNNING: Style(color=ICE_BRIGHT, bold=True),
    RunStatus.SUCCESS: Style(color=ICE_TEAL, bold=True),
    RunStatus.FAILED: Style(color=ICE_FAIL, bold=True),
    RunStatus.CANCELLED: Style(color=ICE_STEEL),
}

_STATUS_ICONS: dict[RunStatus, str] = {
    RunStatus.PENDING: "○",
    RunStatus.SUCCESS: "✓",
    RunStatus.FAILED: "✗",
    RunStatus.CANCELLED: "⊘",
}


class StatusBar(Static):
    """Bottom status bar — breadcrumb navigation or search input."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._test_store = None

    def render(self) -> Text:
        store = self._test_store or self.app.store
        text = Text()

        if store.searching:
            text.append("  ⟱  ", Style(color=ICE_TEAL, bold=True))
            text.append(store.search_query, Style(color=ICE_FROST, bold=True))
            text.append("█", Style(color=ICE_FROST))
            if store.search_query:
                if store.match_count > 0:
                    text.append(
                        f"  ▽▽ {store.search_index + 1}/{store.match_count}",
                        Style(color=ICE_STEEL),
                    )
                else:
                    text.append("  no matches", Style(color=ICE_FAIL))
            return text

        # ── Breadcrumb: workflow › run › node ──
        flow_name = store.current_workflow.rendered_name if store.current_workflow else ""
        text.append("  ")
        text.append(flow_name or "—", Style(color=ICE_FROST))
        if store.run_error:
            text.append("  · ✗ ", Style(color=ICE_FAIL, bold=True))
            text.append(store.run_error, Style(color=ICE_FAIL))

        run = store.current_run
        if run:
            text.append(_SEP, _SEP_STYLE)
            if store.run_pinned:
                text.append(run.run_id, Style(color=ICE_FROST, bold=True))
            else:
                text.append("latest", Style(color=ICE_FROST))
                text.append(f" ({run.run_id})", DIM_STYLE)
            # Status indicator
            if run.status == RunStatus.RUNNING:
                icon = SPINNER_FRAMES[store.frame % len(SPINNER_FRAMES)]
            else:
                icon = _STATUS_ICONS.get(run.status, "")
            if icon:
                text.append(f" {icon}", _STATUS_STYLES.get(run.status, Style()))

        node = store.selected_node
        if node:
            text.append(_SEP, _SEP_STYLE)
            text.append(node.display_name, Style(color=ICE_FROST, bold=True))

        # Residual search info
        if store.search_query and not store.searching:
            text.append("  · ⌕ ", Style(color=ICE_TEAL))
            text.append(store.search_query, Style(color=ICE_STEEL))
            if store.match_count > 0:
                text.append(
                    f"  ▽▽ {store.search_index + 1}/{store.match_count}",
                    Style(color=ICE_STEEL),
                )
        return text
