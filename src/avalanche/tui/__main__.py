"""Allow running the optional TUI as `python -m avalanche.tui`."""

from __future__ import annotations

try:
    from tui import launch_tui
except ModuleNotFoundError as exc:
    if exc.name in {"tui", "textual"}:
        raise ModuleNotFoundError(
            "avalanche.tui is optional. Install it with `avalanche-ai[tui]` "
            "or run `uv sync --extra tui`.",
            name="tui",
        ) from exc
    raise

launch_tui()
