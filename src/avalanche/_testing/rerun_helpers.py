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

from avalanche.runtime import BaseInput


class RerunSelectorInput(BaseInput):
    suffix: str = ""


def varargs_selector_consume(
    prefix,
    payload: RerunSelectorInput,
    df: pl.DataFrame,
    *tail,
):
    return prefix, df["value"].to_list()[0], tuple(tail), payload.suffix


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


def explicit_selector_load_left(*, source):
    return source.append(rerun_rows("left"))


def explicit_selector_load_right(_dependency=None, *, source):
    return source.append(rerun_rows("right"))


def explicit_selector_consume(
    payload: RerunSelectorInput,
    df: pl.DataFrame,
    separator: str,
    *,
    output,
):
    value = f"{df['value'].to_list()[0]}{separator}{payload.suffix}"
    output.append(pl.DataFrame({"id": [1], "value": [value]}))
    return [value]


def explicit_selector_combine(left_df: pl.DataFrame, right_df: pl.DataFrame):
    return f"{left_df['value'].to_list()[0]}+{right_df['value'].to_list()[0]}"


def explicit_selector_split(*, source):
    return (
        source.append(rerun_rows("left")),
        source.append(rerun_rows("right")),
    )


def explicit_selector_value(df: pl.DataFrame):
    return df["value"].to_list()[0]


def positional_only_selector_consume(
    payload: RerunSelectorInput,
    df: pl.DataFrame,
    /,
):
    return df["value"].to_list()[0], payload.suffix


def keyword_only_selector_value(*, df: pl.DataFrame):
    return df["value"].to_list()[0]


def selector_end(value):
    return value


def explicit_non_stream_load():
    return "value"


def explicit_non_stream_consume(value):
    return value


def unindexed_mixed_multireturn(*, source):
    return source.append(rerun_rows("stream")), "ordinary"


def unindexed_mixed_single_return_tuple(*, source):
    return source.append(rerun_rows("stream")), "ordinary"


def unindexed_mixed_single_return_list(*, source):
    return [source.append(rerun_rows("stream")), "ordinary"]


def unindexed_mixed_consume(df: pl.DataFrame, value: str):
    return f"{df['value'].to_list()[0]}+{value}"


def logical_multireturn_split(*, source):
    return "left", source.append(rerun_rows("middle"))


def logical_multireturn_sibling():
    return "other"


def logical_multireturn_consume(
    left: str,
    middle: pl.DataFrame,
    right: str,
):
    return left, middle["value"].to_list()[0], right
