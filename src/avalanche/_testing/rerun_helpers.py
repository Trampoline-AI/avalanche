"""Importable node functions for rerun tests that execute on distributed workers.

Ray executes node functions on separate worker processes, which must be able to
import the function's defining module. Functions defined inside a pytest test
body live in a module (``rerun_test``) that Ray workers cannot import, so any
node that actually runs remotely must come from an installed module instead.

These are raw (undecorated) functions; tests apply ``ava.source/step/dest`` so
node slugs stay owned by the test. They are internal test support only. Do not
re-export them from ``avalanche.__init__`` or treat them as public API.
"""

from __future__ import annotations

import polars as pl


def rerun_rows(*values: str) -> pl.DataFrame:
    return pl.DataFrame(
        {"id": list(range(1, len(values) + 1)), "value": list(values)}
    )


def lineage_load_data(*, source):
    return source.append(rerun_rows("alpha"))


def lineage_process_data(df: pl.DataFrame):
    return pl.DataFrame({"id": df["id"], "value": df["value"] + "-processed"})


def lineage_sink(df: pl.DataFrame, *, output):
    output.append(df)
    return "ok"


def lineage_split_pair():
    """Single-return node whose value is a tuple; indexed downstream via pair[0]."""
    return (
        pl.DataFrame({"id": [1], "value": ["alpha-left"]}),
        pl.DataFrame({"id": [1], "value": ["alpha-right"]}),
    )


def lineage_split_multireturn():
    """True multi-return node (num_returns=2).

    Under Ray each element is its own ObjectRef, so ``pair[0]`` yields a
    tuple/list of ObjectRefs on the driver — the path ``_indexed_parent_result``
    must materialize before indexing.
    """
    return (
        pl.DataFrame({"id": [1], "value": ["left"]}),
        pl.DataFrame({"id": [2], "value": ["right"]}),
    )
