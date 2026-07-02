"""Allow running the optional operator as `python -m avalanche.operator`."""

from __future__ import annotations

try:
    from runtime.operator.__main__ import main
except ModuleNotFoundError as exc:
    if exc.name == "runtime":
        raise ModuleNotFoundError(
            "avalanche.operator is optional. Install it with `avalanche-ai[runtime]` "
            "or run `uv sync --extra runtime`.",
            name="runtime",
        ) from exc
    raise

raise SystemExit(main())
