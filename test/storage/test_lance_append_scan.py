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


def test_lance_append_scan_replays_one_version_and_rejects_ambiguous_ranges(table):
    first, second, third = [
        table.append(pl.DataFrame({"id": [1], "value": [value]}))
        for value in ("first", "second", "third")
    ]

    assert table.append_scan(snapshot_id=first.snapshot_id).to_polars()["value"].to_list() == [
        "first"
    ]
    assert table.append_scan(
        start_snapshot_id=second.snapshot_id,
        snapshot_id=third.snapshot_id,
    ).to_polars()["value"].to_list() == ["third"]

    with pytest.raises(NotImplementedError, match="arbitrary snapshot ranges"):
        table.append_scan(
            start_snapshot_id=first.snapshot_id,
            snapshot_id=third.snapshot_id,
        ).to_polars()
