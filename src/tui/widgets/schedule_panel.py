"""ScheduleWidget — shows scheduled workflows with next run times."""

from __future__ import annotations

from datetime import datetime

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from ..theme import DIM_STYLE, ICE_FROST, ICE_STEEL, ICE_TEAL

_HEADER_STYLE = Style(color=ICE_STEEL, bold=True)


class ScheduleWidget(Static):
    """Displays scheduled workflows with cron expressions and next run times."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._test_store = None

    @staticmethod
    def render_header() -> Text:
        """Render the sticky column header."""
        text = Text()
        text.append("\n")
        text.append("  ")
        text.append(f"{'Field':<14}", _HEADER_STYLE)
        text.append("Value", _HEADER_STYLE)
        text.append("\n")
        text.append("  " + "─" * 36, DIM_STYLE)
        return text

    def render(self) -> Text:
        store = self._test_store or self.app.store
        info = store.current_workflow

        text = Text()

        if info is None:
            text.append("  Select a workflow.\n", DIM_STYLE)
            return text

        if not info.cron:
            text.append("  No schedule.\n", DIM_STYLE)
            text.append("  This workflow runs manually only.\n", DIM_STYLE)
            return text

        text.append("  ")
        text.append(f"{'Cron':<14}", Style(color=ICE_STEEL))
        text.append(f"{info.cron}\n", Style(color=ICE_FROST))

        text.append("  ")
        text.append(f"{'Description':<14}", Style(color=ICE_STEEL))
        text.append(f"{_cron_description(info.cron)}\n", DIM_STYLE)

        text.append("  ")
        text.append(f"{'Next run':<14}", Style(color=ICE_STEEL))
        if info.next_run_at:
            next_str = _humanize_delta(info.next_run_at)
        else:
            next_str = _next_run_str(info.cron)
        text.append(f"{next_str}\n", Style(color=ICE_TEAL))

        if info.last_run_at:
            text.append("  ")
            text.append(f"{'Last run':<14}", Style(color=ICE_STEEL))
            last_str = _humanize_delta(info.last_run_at)
            text.append(f"{last_str}\n", DIM_STYLE)

        return text


def _humanize_delta(timestamp: float) -> str:
    """Humanize a unix timestamp relative to now."""
    import time

    delta = timestamp - time.time()
    abs_delta = abs(int(delta))
    if abs_delta < 60:
        label = "< 1m"
    elif abs_delta < 3600:
        label = f"{abs_delta // 60}m"
    elif abs_delta < 86400:
        h = abs_delta // 3600
        m = (abs_delta % 3600) // 60
        label = f"{h}h {m}m" if m else f"{h}h"
    else:
        label = f"{abs_delta // 86400}d"

    if delta > 0:
        return f"in {label}"
    else:
        return f"{label} ago"


def _cron_description(cron_expr: str) -> str:
    """Human-readable description of a cron expression."""
    parts = cron_expr.split()
    if len(parts) != 5:
        return cron_expr
    minute, hour, dom, month, dow = parts
    if cron_expr == "* * * * *":
        return "Every minute"
    if minute.startswith("*/"):
        n = minute[2:]
        return f"Every {n} minutes"
    if hour == "*" and dom == "*" and month == "*" and dow == "*":
        return f"Every hour at :{minute.zfill(2)}"
    if dom == "*" and month == "*" and dow == "*":
        return f"Daily at {hour.zfill(2)}:{minute.zfill(2)}"
    return cron_expr


def _next_run_str(cron_expr: str) -> str:
    """Humanize the time until the next cron trigger."""
    try:
        from croniter import croniter

        now = datetime.now()
        next_dt = croniter(cron_expr, now).get_next(datetime)
        delta = next_dt - now
        total_seconds = int(delta.total_seconds())

        if total_seconds < 60:
            return "< 1m"
        elif total_seconds < 3600:
            return f"in {total_seconds // 60}m"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            mins = (total_seconds % 3600) // 60
            return f"in {hours}h {mins}m" if mins else f"in {hours}h"
        else:
            days = total_seconds // 86400
            return f"in {days}d"
    except Exception:
        return "?"
