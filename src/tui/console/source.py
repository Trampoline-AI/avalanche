"""The console's data-source seam.

Screens read only through a `ConsoleSource`. Values are the console's wire shapes as plain JSON
(dicts, lists, strings), so one implementation can be a remote Delta API (delta tui) and another
the local runtime (ava). Every method may block; the store calls them on worker threads.
`close()` must wake every blocked read promptly.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Any, Protocol, runtime_checkable

Json = Any


class SourceError(Exception):
    """A read or action failed; `status`/`code` mirror the HTTP status and API reason."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


class Frame(Protocol):
    event: str
    id: str | None
    data: dict


@runtime_checkable
class ConsoleSource(Protocol):
    org: str
    read_only: bool

    # Account and catalog. Paged reads return (items, next_cursor).
    def org_label(self) -> str: ...
    def set_org(self, org: str, label: str | None = None) -> None: ...
    def me_orgs(self, cursor: str | None = None) -> tuple[list[Json], str | None]: ...
    def projects(self, cursor: str | None = None) -> tuple[list[Json], str | None]: ...
    def deployments(
        self, project: str, cursor: str | None = None
    ) -> tuple[list[Json], str | None]: ...
    def flows(
        self, project: str, deployment: str, cursor: str | None = None
    ) -> tuple[list[Json], str | None]: ...
    def flow(self, project: str, slug: str, deployment: str) -> Json: ...
    def schedules(
        self, project: str, cursor: str | None = None
    ) -> tuple[list[Json], str | None]: ...

    # Runs. Summaries return the RunSummaryPage shape: {runs, cursor, complete, projector}.
    def run_summaries(
        self,
        project: str | None = None,
        *,
        flow: str | None = None,
        deployment: str | None = None,
        cursor: str | None = None,
    ) -> Json: ...
    def run(self, project: str, run: str) -> Json: ...
    def run_inputs(self, project: str, run: str) -> Json: ...
    def run_outputs(self, project: str, run: str) -> Json: ...
    def run_artifacts(self, project: str, run: str) -> Json: ...
    def observation(self, project: str, run: str, inv: str, g: int) -> Json: ...
    def capabilities(self, project: str, run: str | None = None) -> Json | None: ...

    # Actions (permission-gated by the caller; the server re-authorizes).
    def cancel(self, project: str, run: str, cmd_id: str | None = None) -> tuple[Json, str]: ...
    def download(
        self, project: str, run: str, binding: str, path: str, destination: str
    ) -> Json: ...

    # Streams: frames until `stop` is set or the source closes; SourceError on HTTP errors.
    def project_events(
        self, project: str, *, stop: threading.Event, last_event_id: str | None = None
    ) -> Iterator[Frame]: ...
    def run_events(
        self, project: str, run: str, *, stop: threading.Event, last_event_id: str | None = None
    ) -> Iterator[Frame]: ...

    def close(self) -> None: ...


def follow(
    source: ConsoleSource,
    open_stream: Callable[[str | None], Iterator[Frame]],
    on_frame: Callable[[Frame], object],
    *,
    stop: threading.Event,
    on_state: Callable[[str], None] | None = None,
) -> None:
    """Resume a stream with bounded backoff; `stop` (or close) wakes it promptly."""
    cursor, delay, attempt = None, 1.0, 0
    while not stop.is_set():
        try:
            if on_state:
                on_state("connecting")
            for frame in open_stream(cursor):
                if on_state:
                    on_state("live")
                delay = 1.0
                if frame.id:
                    cursor = frame.id
                if on_frame(frame) is False:
                    return
        except SourceError as error:
            if on_state:
                on_state(f"unavailable · {error}")
            if error.status in {401, 403, 404} or error.code == "closed":
                return
        except Exception as error:  # noqa: BLE001 - transport loss resumes; display only.
            if stop.is_set():
                return
            if on_state:
                on_state(f"disconnected · {type(error).__name__}")
        attempt += 1
        stop.wait(delay * (1 + (attempt % 3) / 6))
        delay = min(delay * 2, 30)
