"""Measure Avalanche TUI refresh latency as log history grows."""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

from tui.app import AvalancheApp
from tui.models import LogEntry, LogLevel


@dataclass(frozen=True)
class Result:
    scenario: str
    rows: int
    characters: int
    p50_ms: float
    p95_ms: float
    maximum_ms: float
    frame_budget_ms: float

    @property
    def within_budget(self) -> bool:
        return self.p95_ms <= self.frame_budget_ms


def percentile(samples: list[float], percentile_value: float) -> float:
    ordered = sorted(samples)
    rank = max(0, math.ceil(percentile_value * len(ordered)) - 1)
    return ordered[rank]


def build_log(message_length: int, node_id: str) -> LogEntry:
    return LogEntry(
        timestamp=datetime(2026, 1, 1),
        level=LogLevel.INFO,
        node_id=node_id,
        message="x" * message_length,
    )


def build_logs(count: int, message_length: int, node_id: str) -> list[LogEntry]:
    return [build_log(message_length, node_id) for _ in range(count)]


async def measure(
    rows: int,
    *,
    scenario: str,
    message_length: int,
    samples: int,
    width: int,
    height: int,
    frame_budget_ms: float,
) -> Result:
    app = AvalancheApp()
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause()
        if app._timer is not None:
            app._timer.pause()

        # Apply startup updates before replacing the snapshot under test.
        app._tick()
        await app.wait_for_refresh()

        run = app.store.current_run
        if run is None:
            raise RuntimeError("mock TUI did not initialize a current run")

        app.store.run_pinned = True
        node_id = app.store.all_nodes[0].name
        app.store.current_run = replace(
            run,
            logs=build_logs(rows, message_length, node_id),
        )

        # Isolate the visible-widget refresh path from mock-provider polling,
        # which would replace the synthetic snapshot with its backing run.
        app._refresh_widgets()
        await app.wait_for_refresh()
        gc.collect()

        durations: list[float] = []
        for _ in range(samples):
            current_run = app.store.current_run
            if current_run is None:
                raise RuntimeError("current run disappeared during benchmark")
            if scenario == "steady":
                app.store.current_run = replace(
                    current_run,
                    logs=list(current_run.logs),
                )
            else:
                app.store.current_run = replace(
                    current_run,
                    logs=[*current_run.logs, build_log(message_length, node_id)],
                )
            started = time.perf_counter()
            app._refresh_widgets()
            await app.wait_for_refresh()
            durations.append((time.perf_counter() - started) * 1000)

        current_run = app.store.current_run
        if current_run is None:
            raise RuntimeError("current run disappeared during benchmark")
        characters = sum(
            len(entry.node_id) + len(entry.level.value) + len(entry.message) + 25
            for entry in current_run.logs
        )
        return Result(
            scenario=scenario,
            rows=len(current_run.logs),
            characters=characters,
            p50_ms=statistics.median(durations),
            p95_ms=percentile(durations, 0.95),
            maximum_ms=max(durations),
            frame_budget_ms=frame_budget_ms,
        )


async def run_benchmark(args: argparse.Namespace) -> list[Result]:
    frame_budget_ms = 1000 / args.fps
    results = []
    for rows in args.rows:
        for scenario in args.scenarios:
            results.append(
                await measure(
                    rows,
                    scenario=scenario,
                    message_length=args.message_length,
                    samples=args.samples,
                    width=args.width,
                    height=args.height,
                    frame_budget_ms=frame_budget_ms,
                )
            )
    return results


def print_results(results: list[Result]) -> None:
    print("scenario    rows       chars      p50 ms      p95 ms      max ms    budget")
    for result in results:
        status = "PASS" if result.within_budget else "FAIL"
        print(
            f"{result.scenario:<8}  {result.rows:>7,}  {result.characters:>10,}  "
            f"{result.p50_ms:>10.2f}  {result.p95_ms:>10.2f}  "
            f"{result.maximum_ms:>10.2f}  {status:>8}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, nargs="+", default=[100, 1_000, 5_000, 10_000])
    parser.add_argument("--message-length", type=int, default=80)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=("steady", "append"),
        default=["steady", "append"],
    )
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=40)
    parser.add_argument("--fps", type=float, default=30)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--assert-budget", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = asyncio.run(run_benchmark(args))
    print_results(results)

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                [
                    asdict(result) | {"within_budget": result.within_budget}
                    for result in results
                ],
                indent=2,
            )
            + "\n"
        )

    if args.assert_budget and not all(result.within_budget for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
