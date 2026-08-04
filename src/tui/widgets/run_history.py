"""RunHistoryWidget — clickable list of past and active runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.style import Style
from rich.text import Text
from textual.message import Message
from textual.widgets import Static

from ..models import RunState, RunStatus
from ..theme import (
    DIM_STYLE,
    ICE_BRIGHT,
    ICE_FAIL,
    ICE_FROST,
    ICE_PURPLE,
    ICE_STEEL,
    ICE_TEAL,
    ICE_WARN,
    SPINNER_FRAMES,
)

_SELECTED_BG = Style(bgcolor="#2a4a6a")
_HEADER_STYLE = Style(color=ICE_STEEL, bold=True)

RUN_STATUS_STYLES: dict[RunStatus, Style] = {
    RunStatus.REQUESTING: Style(color=ICE_WARN, bold=True),
    RunStatus.PENDING: Style(color=ICE_STEEL),
    RunStatus.RUNNING: Style(color=ICE_BRIGHT, bold=True),
    RunStatus.SUCCESS: Style(color=ICE_TEAL, bold=True),
    RunStatus.FAILED: Style(color=ICE_FAIL, bold=True),
    RunStatus.CANCELLED: Style(color=ICE_PURPLE),
}

RUN_STATUS_ICONS: dict[RunStatus, str] = {
    RunStatus.REQUESTING: "◌",
    RunStatus.PENDING: "○",
    RunStatus.RUNNING: "◐",
    RunStatus.SUCCESS: "✓",
    RunStatus.FAILED: "✗",
    RunStatus.CANCELLED: "⊘",
}

# Docked header is a separate widget; no offset needed in content click handler
_HEADER_LINES = 0


class RunHistoryWidget(Static):
    """Displays a clickable list of runs for a workflow."""

    @dataclass
    class RunSelected(Message):
        run: RunState

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._display_order: list[RunState] = []
        self._test_store = None

    @staticmethod
    def render_header() -> Text:
        """Render the sticky column header."""
        text = Text()
        text.append("  ")
        text.append(f"{'Run ID':<14}", _HEADER_STYLE)
        text.append(f"{'Start':<18}", _HEADER_STYLE)
        text.append(f"{'Time':>7}", _HEADER_STYLE)
        text.append("   ")
        text.append(f"{'Status':<9}", _HEADER_STYLE)
        text.append("\n")
        text.append("  " + "─" * 51, DIM_STYLE)
        return text

    def render(self) -> Text:
        store = self._test_store or self.app.store
        runs = store.runs_for_current_workflow
        selected_run_id = store.selected_run_id
        frame = store.frame

        text = Text()

        if not runs:
            text.append("  No runs yet.\n", DIM_STYLE)
            self._display_order = []
            return text

        self._display_order = list(reversed(runs))

        for run in self._display_order:
            is_selected = run.run_id == selected_run_id
            style = RUN_STATUS_STYLES[run.status]

            if run.status == RunStatus.RUNNING:
                icon = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
            else:
                icon = RUN_STATUS_ICONS[run.status]

            ts = (
                datetime.now().strftime("%Y-%m-%d %H:%M")
                if run.started_at is not None
                else "                "
            )
            elapsed = run.elapsed
            dur_str = f"{_fmt_time(elapsed):>7}" if elapsed is not None else "      —"
            status_str = f"{run.status.value} {icon}"

            if is_selected:
                bg = _SELECTED_BG
                text.append("  ", bg)
                text.append(f"{run.run_id:<14}", Style(color=ICE_FROST, bold=True) + bg)
                text.append(f"{ts}  ", DIM_STYLE + bg)
                text.append(dur_str, DIM_STYLE + bg)
                text.append("   ", bg)
                text.append(status_str, style + bg)
                text.append("\n")
            else:
                text.append("  ")
                text.append(f"{run.run_id:<14}", Style(color=ICE_FROST))
                text.append(f"{ts}  ", DIM_STYLE)
                text.append(dur_str, DIM_STYLE)
                text.append("   ")
                text.append(status_str, style)
                text.append("\n")

        return text

    def on_click(self, event) -> None:
        self.app.store.focused_pane = "run-history"
        row = event.y
        if 0 <= row < len(self._display_order):
            run = self._display_order[row]
            self.post_message(self.RunSelected(run=run))

    def scroll_to_selected(self) -> None:
        """Scroll the parent container so the selected run is centered."""
        store = self._test_store or self.app.store
        selected_id = store.selected_run_id
        if not selected_id or not self._display_order:
            return
        for i, run in enumerate(self._display_order):
            if run.run_id == selected_id:
                y = _HEADER_LINES + i
                container = self.parent
                if container is not None:
                    half = container.content_size.height // 2
                    container.scroll_to(y=max(0, y - half), animate=False)
                return


def _fmt_time(secs: float) -> str:
    mins, s = divmod(secs, 60)
    if mins > 0:
        return f"{int(mins)}m{s:.0f}s"
    return f"{s:.1f}s"


def _next_run_label(cron_expr: str, next_run_at: float | None = None) -> str:
    """Return e.g. 'in 5m' for the next scheduled run, or '' if unknown.

    Uses server-provided *next_run_at* when available, otherwise computes
    the next fire time locally via croniter.
    """
    import time as _time

    delta = None
    if next_run_at:
        d = int(next_run_at - _time.time())
        if d > 0:
            delta = d
    # Fall through to croniter if server timestamp is missing or stale
    if delta is None:
        try:
            from croniter import croniter

            now = datetime.now()
            next_dt = croniter(cron_expr, now).get_next(datetime)
            delta = int((next_dt - now).total_seconds())
        except Exception:
            return ""

    if delta <= 0:
        return ""
    if delta < 60:
        return "in < 1m"
    if delta < 3600:
        return f"in {delta // 60}m"
    h, m = delta // 3600, (delta % 3600) // 60
    if delta < 86400:
        return f"in {h}h {m}m" if m else f"in {h}h"
    return f"in {delta // 86400}d"


def _cron_description(cron_expr: str) -> str:
    """Human-readable description of a cron expression (lowercase, no leading verb).

    Designed to follow "Scheduled" in a sentence, e.g. "Scheduled every 5 minutes".
    """
    parts = cron_expr.split()
    if len(parts) != 5:
        return cron_expr
    minute, hour, dom, month, dow = parts
    if cron_expr == "* * * * *":
        return "every minute"
    if minute.startswith("*/"):
        n = minute[2:]
        return f"every {n} minutes"
    if hour == "*" and dom == "*" and month == "*" and dow == "*":
        return f"every hour at :{minute.zfill(2)}"
    if dom == "*" and month == "*" and dow == "*":
        return f"daily at {hour.zfill(2)}:{minute.zfill(2)}"
    return cron_expr
