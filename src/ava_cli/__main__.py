"""Run the Avalanche CLI as `python -m ava_cli`."""

from __future__ import annotations

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
