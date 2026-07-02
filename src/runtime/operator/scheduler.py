"""Scheduler — cron-based automatic workflow execution."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING

from croniter import croniter

if TYPE_CHECKING:
    from .operator import Operator

logger = logging.getLogger(__name__)


class Scheduler:
    """Runs scheduled workflows based on cron expressions.

    Checks every 60 seconds whether any workflow's cron expression
    matches the current minute. If so, calls operator.start_run().
    Deduplicates by tracking the last triggered minute per workflow.
    """

    def __init__(self, operator: Operator) -> None:
        self._operator = operator
        self._stop = threading.Event()
        self._last_triggered: dict[str, float] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Scheduler started")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(timeout=10):
            self._check_schedules()

    def _check_schedules(self) -> None:
        now = datetime.now()
        current_minute = now.replace(second=0, microsecond=0)
        current_ts = current_minute.timestamp()

        for info in self._operator.list_workflows():
            if not info.cron:
                continue

            try:
                if croniter.match(info.cron, current_minute):
                    last = self._last_triggered.get(info.name, 0)
                    if last < current_ts:
                        self._last_triggered[info.name] = current_ts
                        logger.info(f"Scheduled run: {info.name} (cron={info.cron})")
                        self._operator.start_run(
                            info.name, triggered_by="scheduled"
                        )
            except Exception as e:
                logger.warning(f"Scheduler error for {info.name}: {e}")

    def next_run_time(self, cron_expr: str) -> datetime | None:
        """Get the next run time for a cron expression."""
        try:
            return croniter(cron_expr, datetime.now()).get_next(datetime)
        except Exception:
            return None

    def list_schedules(self) -> list[dict]:
        """List all scheduled workflows with their next run times."""
        schedules = []
        for info in self._operator.list_workflows():
            if info.cron:
                next_run = self.next_run_time(info.cron)
                schedules.append({
                    "flow_name": info.name,
                    "cron": info.cron,
                    "next_run": next_run,
                    "last_triggered": self._last_triggered.get(info.name),
                })
        return schedules
