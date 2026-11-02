"""The ``--state-db`` command-line option shared by every command that opens the store."""

from __future__ import annotations

import argparse

from .store import DEFAULT_STATE_DB


def add_state_db_argument(parser: argparse.ArgumentParser) -> None:
    """Add ``--state-db PATH``, defaulting to the start folder's state database."""
    parser.add_argument(
        "--state-db",
        default=str(DEFAULT_STATE_DB),
        metavar="PATH",
        help=(
            "SQLite file that keeps operator state, or :memory: for a throwaway one "
            "(default: .avalanche/operator/state.db under the current folder)"
        ),
    )
