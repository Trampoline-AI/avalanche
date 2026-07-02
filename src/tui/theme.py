"""Avalanche TUI theme — avalanche glacier palette and all style constants."""

from __future__ import annotations

from rich.style import Style
from textual.theme import Theme

from .models import LogLevel, NodeStatus

# ── Avalanche glacier palette ─────────────────────────────────────────────────
# Derived from the avalanche logo: deep navy → purple → steel blue → teal → ice
ICE_DEEP_NAVY = "#2f254c"
ICE_PURPLE = "#b898e0"
ICE_STEEL = "#90c8e0"
ICE_TEAL = "#60dce4"
ICE_BRIGHT = "#b8f4ff"
ICE_FROST = "#f0faff"
ICE_WARN = "#f8d078"      # warm amber (stands out against cool palette)
ICE_FAIL = "#f87898"      # bright rose-red

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

STATUS_CHARS: dict[NodeStatus, str] = {
    NodeStatus.PENDING: "○",
    NodeStatus.SUCCESS: "✓",
    NodeStatus.FAILED: "✗",
    NodeStatus.SKIPPED: "⊘",
}

VIRTUAL_LABELS: dict[str, str] = {
    "start": "start ◆",
    "end": "◆ end",
}
