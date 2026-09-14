"""Live run-summary pagination boundaries for the gRPC client."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import grpc
import pytest

from runtime.operator.client import (
    GrpcStateProvider,
    OperatorCallError,
    _ClientBudgetExceededError,
    _ResetBaselineMismatchError,
)
from runtime.operator.proto import operator_pb2 as pb

_PageReader = Literal["list_runs", "reset_baseline"]


def _event_ulid(sequence: int) -> str:
    return f"{sequence:026X}"


def _lifecycle_cursor() -> pb.LifecycleCursorV2:
    return pb.LifecycleCursorV2(
        stream="operator-events",
        topology_fingerprint="operator-events-topology",
        stream_generation=1,
        retained_floor_event_ulid=_event_ulid(0),
        event_ulid=_event_ulid(20),
    )


def _summary_cursor(
    *,
    stream: str = "project-summaries",
    topology_fingerprint: str = "project-summary-topology",
    source_generation: str = "generation-1",
    retained_floor_sequence: int = 10,
    target_head_sequence: int = 20,
    checkpoint_watermark: int = 15,
    checkpoint_digest: str = "checkpoint-15",
) -> pb.ProjectSummaryCursorV2:
    return pb.ProjectSummaryCursorV2(
        stream=stream,
        topology_fingerprint=topology_fingerprint,
        source_generation=source_generation,
        retained_floor_sequence=retained_floor_sequence,
        target_head_sequence=target_head_sequence,
        checkpoint_watermark=checkpoint_watermark,
        checkpoint_digest=checkpoint_digest,
    )


def _summary_pages(
    *cursors: pb.ProjectSummaryCursorV2 | None,
) -> list[pb.RunSummaryPageV2]:
    scope = pb.ScopeReferenceV2(reference="operator-1")
    cursor = _lifecycle_cursor()
    pages: list[pb.RunSummaryPageV2] = []
    for index, summary_cursor in enumerate(cursors, start=1):
        page = pb.RunSummaryPageV2(
            cursor=cursor,
            scope_ref=scope,
            runs=[
                pb.RunSummaryV2(
                    run_id=f"run-{index}",
                    workflow_selector="flow",
                    workflow_display_name="flow",
                    status="running",
                    created_sequence=index,
                    revision=index,
                )
            ],
        )
        if summary_cursor is not None:
            page.project_summary_cursor.CopyFrom(summary_cursor)
        if index < len(cursors):
            page.next_page.CopyFrom(
                pb.ContinuationRefV2(
                    scope_ref=scope,
                    continuation_id=f"page-{index + 1}",
                    cursor=cursor,
                )
            )
            if summary_cursor is not None:
                page.next_page.project_summary_cursor.CopyFrom(summary_cursor)
        pages.append(page)
    return pages


class _SummaryPagesStub:
    def __init__(self, pages: list[pb.RunSummaryPageV2]) -> None:
        self.pages = pages
        self.requests: list[pb.ListRunSummariesRequestV2] = []

    def ListRunSummaries(  # noqa: N802
        self,
        request: pb.ListRunSummariesRequestV2,
        **_kwargs,
    ) -> pb.RunSummaryPageV2:
        copied_request = pb.ListRunSummariesRequestV2()
        copied_request.CopyFrom(request)
        index = len(self.requests)
        self.requests.append(copied_request)
        assert index < len(self.pages), "client requested an unexpected page"
        return self.pages[index]


@contextmanager
def _summary_pages_provider(
    pages: list[pb.RunSummaryPageV2],
    *,
    max_paged_items: int = 1000,
) -> Iterator[tuple[GrpcStateProvider, _SummaryPagesStub]]:
    provider = GrpcStateProvider("localhost:1", max_paged_items=max_paged_items)
    stub = _SummaryPagesStub(pages)
    provider._stub = stub
    try:
        yield provider, stub
    finally:
        provider.close()


def _read_page_chain(provider: GrpcStateProvider, reader: _PageReader) -> list[str]:
    if reader == "list_runs":
        return [run.run_id for run in provider.list_runs("flow")]
    _marker, summaries = provider._list_run_summaries()
    return [summary.run_id for summary in summaries]


@pytest.mark.parametrize("reader", ("list_runs", "reset_baseline"))
@pytest.mark.parametrize(
    ("heads", "watermarks", "digests"),
    [
        pytest.param(
            (7, 7),
            (5, 6),
            ("checkpoint-5", "checkpoint-6"),
            id="checkpoint-progress",
        ),
        pytest.param((7, 8, 9), (5, 5, 5), ("checkpoint-5",) * 3, id="head-progress"),
        pytest.param(
            (7, 8, 10),
            (5, 6, 8),
            ("checkpoint-5", "checkpoint-6", "checkpoint-8"),
            id="head-and-checkpoint-progress",
        ),
        pytest.param(
            (7, 7),
            (5, 5),
            ("checkpoint-a", "checkpoint-b"),
            id="checkpoint-representation",
        ),
    ],
)
def test_live_project_summary_progress_is_accepted_by_both_page_consumers(
    reader: _PageReader,
    heads: tuple[int, ...],
    watermarks: tuple[int, ...],
    digests: tuple[str, ...],
):
    pages = _summary_pages(
        *(
            _summary_cursor(
                retained_floor_sequence=0,
                target_head_sequence=head,
                checkpoint_watermark=watermark,
                checkpoint_digest=digest,
            )
            for head, watermark, digest in zip(heads, watermarks, digests, strict=True)
        )
    )

    with _summary_pages_provider(pages) as (provider, stub):
        assert _read_page_chain(provider, reader) == [
            f"run-{index}" for index in range(1, 1 + len(pages))
        ]

        assert len(stub.requests) == len(pages)
        if reader == "list_runs":
            assert stub.requests[0].workflow_selector == "flow"
        else:
            assert stub.requests[0].workflow_selector == ""
        if len(pages) > 1:
            assert stub.requests[1].continuation == pages[0].next_page


@pytest.mark.parametrize("reader", ("list_runs", "reset_baseline"))
def test_page_consumers_accept_a_consistently_absent_project_summary_cursor(
    reader: _PageReader,
):
    pages = _summary_pages(None, None)

    with _summary_pages_provider(pages) as (provider, stub):
        assert _read_page_chain(provider, reader) == ["run-1", "run-2"]
        assert len(stub.requests) == 2


@pytest.mark.parametrize("reader", ("list_runs", "reset_baseline"))
@pytest.mark.parametrize("last_head", (6, 8), ids=("below-first-head", "after-progress"))
def test_page_consumers_reject_a_project_summary_head_rewind(
    reader: _PageReader,
    last_head: int,
):
    pages = _summary_pages(
        *(
            _summary_cursor(
                retained_floor_sequence=0,
                target_head_sequence=head,
                checkpoint_watermark=5,
            )
            for head in (7, 9, last_head)
        )
    )

    with _summary_pages_provider(pages) as (provider, stub):
        with pytest.raises(OperatorCallError) as error:
            _read_page_chain(provider, reader)

        assert error.value.status is grpc.StatusCode.DATA_LOSS
        assert len(stub.requests) == 3


@pytest.mark.parametrize(
    "changed_cursor",
    [
        pytest.param(_summary_cursor(stream="other-summary-stream"), id="stream"),
        pytest.param(_summary_cursor(topology_fingerprint="other-topology"), id="topology"),
        pytest.param(_summary_cursor(source_generation="generation-2"), id="generation"),
        pytest.param(_summary_cursor(retained_floor_sequence=9), id="floor-rewind"),
        pytest.param(_summary_cursor(retained_floor_sequence=11), id="floor-advance"),
    ],
)
def test_list_runs_rejects_project_summary_source_discontinuity(
    changed_cursor: pb.ProjectSummaryCursorV2,
):
    pages = _summary_pages(_summary_cursor(), changed_cursor)

    with _summary_pages_provider(pages) as (provider, stub):
        with pytest.raises(OperatorCallError) as error:
            provider.list_runs("flow")

        assert error.value.status is grpc.StatusCode.DATA_LOSS
        assert len(stub.requests) == 2


@pytest.mark.parametrize(
    "changed_cursor",
    [
        pytest.param(_summary_cursor(stream="other-summary-stream"), id="stream"),
        pytest.param(_summary_cursor(topology_fingerprint="other-topology"), id="topology"),
        pytest.param(_summary_cursor(source_generation="generation-2"), id="generation"),
        pytest.param(_summary_cursor(retained_floor_sequence=11), id="floor"),
        pytest.param(_summary_cursor(target_head_sequence=21), id="head"),
        pytest.param(_summary_cursor(checkpoint_watermark=16), id="watermark"),
        pytest.param(_summary_cursor(checkpoint_digest="checkpoint-other"), id="digest"),
    ],
)
def test_list_runs_rejects_a_continuation_not_bound_to_its_issuing_page(
    changed_cursor: pb.ProjectSummaryCursorV2,
):
    pages = _summary_pages(_summary_cursor(), _summary_cursor())
    pages[0].next_page.project_summary_cursor.CopyFrom(changed_cursor)

    with _summary_pages_provider(pages) as (provider, stub):
        with pytest.raises(OperatorCallError) as error:
            provider.list_runs("flow")

        assert error.value.status is grpc.StatusCode.DATA_LOSS
        assert len(stub.requests) == 1


@pytest.mark.parametrize(
    ("first_cursor", "second_cursor"),
    [
        pytest.param(None, _summary_cursor(), id="cursor-appears"),
        pytest.param(_summary_cursor(), None, id="cursor-disappears"),
    ],
)
def test_list_runs_rejects_project_summary_cursor_appearance_or_disappearance(
    first_cursor: pb.ProjectSummaryCursorV2 | None,
    second_cursor: pb.ProjectSummaryCursorV2 | None,
):
    pages = _summary_pages(first_cursor, second_cursor)

    with _summary_pages_provider(pages) as (provider, stub):
        with pytest.raises(OperatorCallError) as error:
            provider.list_runs("flow")

        assert error.value.status is grpc.StatusCode.DATA_LOSS
        assert len(stub.requests) == 2


@pytest.mark.parametrize("reader", ("list_runs", "reset_baseline"))
def test_page_consumers_reject_a_repeated_continuation_after_live_progress(reader: _PageReader):
    pages = _summary_pages(
        _summary_cursor(target_head_sequence=20),
        _summary_cursor(target_head_sequence=21, checkpoint_watermark=16),
        _summary_cursor(target_head_sequence=21, checkpoint_watermark=16),
    )
    pages[1].next_page.continuation_id = pages[0].next_page.continuation_id

    with _summary_pages_provider(pages) as (provider, stub):
        if reader == "list_runs":
            with pytest.raises(OperatorCallError) as error:
                _read_page_chain(provider, reader)
            assert error.value.status is grpc.StatusCode.DATA_LOSS
        else:
            with pytest.raises(_ResetBaselineMismatchError) as error:
                _read_page_chain(provider, reader)

        assert "repeated a page token" in str(error.value)
        assert len(stub.requests) == 2


@pytest.mark.parametrize("reader", ("list_runs", "reset_baseline"))
def test_page_consumers_enforce_the_page_accumulation_bound(reader: _PageReader):
    pages = _summary_pages(_summary_cursor(), _summary_cursor())

    with _summary_pages_provider(pages, max_paged_items=1) as (provider, stub):
        with pytest.raises(_ClientBudgetExceededError):
            _read_page_chain(provider, reader)

        assert len(stub.requests) == 1
