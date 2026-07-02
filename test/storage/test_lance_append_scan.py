"""Lance-specific append_scan behavior."""

from __future__ import annotations

import dataframely as dy
import polars as pl
import pytest

from avalanche.lance import LanceNamespace, LanceNamespaceConfig, LanceTable

pytest.importorskip("lance")


class LanceAppendScanSchema(dy.Schema):
    id = dy.Int64(nullable=False)
    value = dy.String(nullable=False)


def _rows(value: str) -> pl.DataFrame:
    return pl.DataFrame({"id": [1], "value": [value]})


@pytest.fixture
def table(tmp_path):
    class LanceAppendScanNamespace(LanceNamespace):
        ns_config = LanceNamespaceConfig(
            name="lance-append-scan",
            base_location=str(tmp_path),
        )
        records = LanceTable(schema=LanceAppendScanSchema)

    ns = LanceAppendScanNamespace()
    ns.push()
    return ns.records


def test_lance_append_scan_replays_one_requested_data_version(table):
    first = table.append(_rows("first"))
    second = table.append(_rows("second"))
    third = table.append(_rows("third"))

    assert table.append_scan(snapshot_id=first.snapshot_id).to_polars()["value"].to_list() == [
        "first"
    ]
    assert table.append_scan(
        start_snapshot_id=second.snapshot_id,
        snapshot_id=third.snapshot_id,
    ).to_polars()["value"].to_list() == ["third"]


def test_lance_append_scan_rejects_unsupported_snapshot_ranges(table):
    first = table.append(_rows("first"))
    table.append(_rows("second"))
    third = table.append(_rows("third"))

    with pytest.raises(NotImplementedError, match="arbitrary snapshot ranges"):
        table.append_scan(
            start_snapshot_id=first.snapshot_id,
            snapshot_id=third.snapshot_id,
        ).to_polars()
