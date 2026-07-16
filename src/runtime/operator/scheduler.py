"""Cron scheduler with explicit descriptor reconciliation."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Iterable, Iterator

from croniter import croniter

from .models import WorkflowDescriptor

if TYPE_CHECKING:
    from .operator import Operator

logger = logging.getLogger(__name__)


class Scheduler:
    """Run reconciled cron registrations keyed by canonical workflow ID."""

    def __init__(self, operator: Operator) -> None:
        self._operator = operator
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._dispatch_lock = threading.RLock()
        self._registrations: dict[str, WorkflowDescriptor] = {}
        self._last_triggered: dict[str, float] = {}
        self._revision = 0
        self._thread: threading.Thread | None = None

    def reconcile(self, descriptors: Iterable[WorkflowDescriptor]) -> None:
        """Atomically replace current cron registrations."""
        registrations = {
            descriptor.workflow_id: descriptor
            for descriptor in descriptors
            if descriptor.cron is not None
        }
        with self._dispatch_lock:
            with self._lock:
                self._revision += 1
                self._registrations = dict(sorted(registrations.items()))
                current_ids = set(self._registrations)
                self._last_triggered = {
                    workflow_id: timestamp
                    for workflow_id, timestamp in self._last_triggered.items()
                    if workflow_id in current_ids
                }

    @contextmanager
    def reconciliation_boundary(self) -> Iterator[None]:
        """Serialize catalog publication and schedule replacement with dispatch."""
        with self._dispatch_lock:
            yield

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Scheduler started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(timeout=10):
            self._check_schedules()

    def _check_schedules(self) -> None:
        now = datetime.now()
        current_minute = now.replace(second=0, microsecond=0)
        current_ts = current_minute.timestamp()
        with self._lock:
            revision = self._revision
            registrations = tuple(self._registrations.values())

        for descriptor in registrations:
            try:
                if croniter.match(descriptor.cron, current_minute):
                    with self._dispatch_lock:
                        with self._lock:
                            if revision != self._revision:
                                continue
                            last = self._last_triggered.get(descriptor.workflow_id, 0)
                            if last >= current_ts:
                                continue
                            self._last_triggered[descriptor.workflow_id] = current_ts
                        self._operator.start_run(
                            descriptor.workflow_id, triggered_by="scheduled"
                        )
                    logger.info(
                        "Scheduled run: %s (cron=%s)",
                        descriptor.workflow_id,
                        descriptor.cron,
                    )
            except Exception as exc:
                logger.warning("Scheduler error for %s: %s", descriptor.workflow_id, exc)

    def next_run_time(self, cron_expr: str) -> datetime | None:
        try:
            return croniter(cron_expr, datetime.now()).get_next(datetime)
        except Exception:
            return None

    def last_triggered(self, workflow_id: str) -> float | None:
        with self._lock:
            return self._last_triggered.get(workflow_id)

    def list_schedules(self) -> list[dict]:
        with self._lock:
            registrations = tuple(self._registrations.values())
            last_triggered = dict(self._last_triggered)
        return [
            {
                "flow_name": descriptor.display_name,
                "workflow_id": descriptor.workflow_id,
                "cron": descriptor.cron,
                "next_run": self.next_run_time(descriptor.cron),
                "last_triggered": last_triggered.get(descriptor.workflow_id),
            }
            for descriptor in registrations
        ]
