"""Avalanche TUI theme — avalanche glacier palette and all style constants."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from rich.style import Style
from textual.theme import Theme

from .models import LogLevel, NodeStatus, RunState, RunStatus, WorkflowInfo

# ── Avalanche glacier palette ─────────────────────────────────────────────────
# Derived from the avalanche logo: deep navy → purple → steel blue → teal → ice
ICE_DEEP_NAVY = "#2f254c"
ICE_PURPLE = "#b898e0"
ICE_STEEL = "#90c8e0"
ICE_TEAL = "#60dce4"
ICE_BRIGHT = "#b8f4ff"
ICE_FROST = "#f0faff"
ICE_WARN = "#f8d078"  # warm amber (stands out against cool palette)
ICE_FAIL = "#f87898"  # bright rose-red

# Colors for skip-edge connectors (distinct, muted pastels)
SKIP_EDGE_COLORS = ["#e8b8e0", "#b8e0b8", "#e0d898", "#98d8e0", "#e0b8b8", "#b8b8f0"]

AVALANCHE_THEME = Theme(
    name="avalanche",
    primary=ICE_TEAL,
    secondary=ICE_PURPLE,
    accent=ICE_TEAL,
    warning=ICE_WARN,
    error=ICE_FAIL,
    success=ICE_TEAL,
    background=ICE_DEEP_NAVY,
    surface="#382e5a",
    panel="#3f3566",
    dark=True,
)

# ── Node status styles ────────────────────────────────────────────────────

STATUS_STYLES: dict[NodeStatus, Style] = {
    NodeStatus.PENDING: Style(color=ICE_STEEL),
    NodeStatus.RUNNING: Style(color=ICE_BRIGHT, bold=True),
    NodeStatus.SUCCESS: Style(color=ICE_TEAL, bold=True),
    NodeStatus.FAILED: Style(color=ICE_FAIL, bold=True),
    NodeStatus.SKIPPED: Style(color=ICE_PURPLE),
}

# Unknown statuses are display-only; identity prevents a raw string or a
# provider object that compares equal to an enum from borrowing its meaning.
OTHER_STATUS_CHAR = "◇"
OTHER_STATUS_STYLE = Style(color=ICE_STEEL, dim=True)


def status_display[T](
    status: object,
    values: Mapping[NodeStatus, T] | Mapping[RunStatus, T],
    other: T,
) -> T:
    """Use a display map only for its actual enum members (no hashing required)."""
    return next((value for member, value in values.items() if status is member), other)


# ── Structural styles ─────────────────────────────────────────────────────

BRACKET_STYLE = Style(color=ICE_STEEL)
ARROW_STYLE = Style(color=ICE_STEEL)
DOT_STYLE = Style(color=ICE_DEEP_NAVY)
SELECTED_STYLE = Style(color=ICE_DEEP_NAVY, bgcolor=ICE_FROST, bold=True)
DIM_STYLE = Style(color=ICE_STEEL)
VIRTUAL_STYLE = Style(color=ICE_STEEL)

# ── Search styles ──────────────────────────────────────────────────────────

SEARCH_HIGHLIGHT = Style(color=ICE_DEEP_NAVY, bgcolor=ICE_WARN, bold=True)
SEARCH_CURRENT = Style(color=ICE_DEEP_NAVY, bgcolor=ICE_BRIGHT, bold=True)

# ── Log level styles ───────────────────────────────────────────────────────

LOG_LEVEL_STYLES: dict[LogLevel, Style] = {
    LogLevel.DEBUG: Style(color=ICE_STEEL),
    LogLevel.INFO: Style(color=ICE_FROST),
    LogLevel.WARN: Style(color=ICE_WARN, bold=True),
    LogLevel.ERROR: Style(color=ICE_FAIL, bold=True),
}

# ── DAG rendering constants ───────────────────────────────────────────────

SPINNER_FRAMES = ["◐", "◑", "◒", "◓"]
AGENT_CAPTION_STYLE = Style(color=ICE_STEEL, dim=True)
AGENT_CAPTION_SELECTED_STYLE = Style(color=ICE_DEEP_NAVY, bgcolor=ICE_STEEL, dim=True)


STATUS_CHARS: dict[NodeStatus, str] = {
    NodeStatus.PENDING: "○",
    NodeStatus.SUCCESS: "✓",
    NodeStatus.FAILED: "✗",
    NodeStatus.SKIPPED: "⊘",
}

WaitKind = Literal["sleep", "event", "decision", "run"]
WAIT_DECISION_CHAR = "!"
WAIT_DECISION_STYLE = Style(color=ICE_WARN, bold=True)
WAIT_TIME_CHAR = "◷"
WAIT_TIME_STYLE = Style(color=ICE_STEEL, dim=True)


def node_wait_kind(workflow: WorkflowInfo | None, node_id: str) -> WaitKind | None:
    """Resolve declared waits, including mapped decision invocations."""
    if workflow is None:
        return None
    spec = workflow.node_waits.get(node_id.split("[", 1)[0])
    return spec.kind if spec is not None else None


def node_status_display(
    status: object, wait_kind: WaitKind | None = None, frame: int = 0
) -> tuple[str, Style]:
    """Only executing work animates; child-run waits are event-like waits."""
    if status is NodeStatus.RUNNING:
        if wait_kind == "decision":
            return WAIT_DECISION_CHAR, WAIT_DECISION_STYLE
        if wait_kind is not None:
            return WAIT_TIME_CHAR, WAIT_TIME_STYLE
        return SPINNER_FRAMES[frame % len(SPINNER_FRAMES)], STATUS_STYLES[NodeStatus.RUNNING]
    return (
        status_display(status, STATUS_CHARS, OTHER_STATUS_CHAR),
        status_display(status, STATUS_STYLES, OTHER_STATUS_STYLE),
    )


def run_wait_kind(run: RunState, workflow: WorkflowInfo | None) -> WaitKind | None:
    """Use loaded node state for a parked run's summary, never change runtime status."""
    if run.status is not RunStatus.RUNNING:
        return None
    active = [node for node in run.nodes.values() if node.status is NodeStatus.RUNNING]
    kinds: list[WaitKind | None] = [node_wait_kind(workflow, node.node_id) for node in active]
    if not kinds or None in kinds:
        return None
    return "decision" if "decision" in kinds else kinds[0]


VIRTUAL_LABELS: dict[str, str] = {
    "start": "start ◆",
    "end": "◆ end",
}
