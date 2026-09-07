"""Importable storage workers for rerun scenarios; tests apply node decorators."""

from __future__ import annotations

import polars as pl

from avalanche.runtime import BaseInput


class RerunSelectorInput(BaseInput):
    suffix: str = ""


def rerun_rows(*values: str) -> pl.DataFrame:
    return pl.DataFrame({"id": list(range(1, len(values) + 1)), "value": list(values)})


def lineage_load_data(*, source):
    return source.append(rerun_rows("alpha"))


def lineage_process_data(df: pl.DataFrame):
    return pl.DataFrame({"id": df["id"], "value": df["value"] + "-processed"})


def lineage_sink(df: pl.DataFrame, *, output):
    output.append(df)
    return "ok"


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
