"""Format operator runs for the `ava runs` commands."""

from __future__ import annotations

import time
from typing import Any

from runtime.operator.models import RunState

_COLUMNS = ("RUN ID", "WORKFLOW", "STATUS", "STARTED", "DURATION")


def run_duration(run: Any) -> int:
    """Seconds a run has taken so far, or took in total."""
    started = getattr(run, "started_at", None) or 0
    ended = getattr(run, "ended_at", None) or time.time()
    return int(ended - started)


def format_duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02}m"
    if minutes:
        return f"{minutes}m{secs:02}s"
    return f"{secs}s"


def format_runs_table(runs: list[RunState]) -> str:
    rows = [_COLUMNS]
    for run in runs:
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.started_at))
        rows.append(
            (
                run.run_id,
                run.workflow_display_name or run.flow_name,
                run.status.value,
                started,
                format_duration(run_duration(run)),
            )
        )
    widths = [max(len(row[index]) for row in rows) for index in range(len(_COLUMNS))]
    return "\n".join(
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        for row in rows
    )


def runs_json(runs: list[RunState]) -> list[dict]:
    return [
        {
            "run_id": run.run_id,
            "workflow": run.workflow_display_name or run.flow_name,
            "status": str(run.status),
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "duration_seconds": run_duration(run),
        }
        for run in runs
    ]
