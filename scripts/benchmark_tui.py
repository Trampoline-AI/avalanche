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


def retained_bytes(value, seen: set[int] | None = None) -> int:
    """Count owned data recursively, excluding code, locks and provider resources."""
    import sys

    if seen is None:
        seen = set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        return size + sum(
            retained_bytes(k, seen) + retained_bytes(v, seen) for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return size + sum(retained_bytes(item, seen) for item in value)
    if type(value).__module__.startswith(("tui", "runtime.operator.models", "predict_rlm")):
        if hasattr(value, "__dict__"):
            return size + retained_bytes(vars(value), seen)
    return size


def current_rss_bytes() -> int:
    import os
    import subprocess

    return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())])) * 1024


async def measure_large_fixture(samples: int) -> dict:
    """Fixed synthetic rendering workload; no backend or network claim."""
    from tui.mock import MockStateProvider
    from tui.models import NodeState, NodeStatus, RunState, RunStatus, WorkflowInfo

    provider = MockStateProvider(include_agent_trace=True)
    ids = [f"node_{i}" for i in range(1000)]
    workflow = WorkflowInfo(
        name="large_fixture",
        file_path="benchmark.py",
        node_ids=ids,
        graph={ids[i]: [ids[i + 1]] for i in range(1000) if i % 10 != 9},
        node_types={nid: "step" for nid in ids},
        agent_node_ids=[ids[0]],
    )
    trace = json.loads(provider._runs["run_agent"].nodes["inspect_agent_1"].agent_trace_json)
    step = trace["trace"]["steps"][0]
    trace["trace"]["steps"] = [dict(step, iteration=i + 1) for i in range(1000)]
    trace["trace"]["iterations"] = 1000
    trace["trace"]["max_iterations"] = 1000
    nodes = {nid: NodeState(nid, nid, "step", NodeStatus.SUCCESS) for nid in ids}
    nodes[ids[0]] = replace(nodes[ids[0]], agent_trace_json=json.dumps(trace))
    runs = [
        RunState(
            run_id=f"bench_{i:05d}",
            flow_name=workflow.name,
            status=RunStatus.SUCCESS,
            started_at=1000 + i,
            ended_at=1001 + i,
            triggered_at=1767225600 + i,
            operator_instance_id="mock",
            created_sequence=i + 1,
        )
        for i in range(10000)
    ]
    runs[-1] = replace(runs[-1], nodes=nodes, logs=build_logs(100000, 80, ids[0]))
    provider._workflows = {workflow.selector: workflow}
    provider._runs = {run.run_id: run for run in runs}
    app = AvalancheApp(provider=provider)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        app._timer.pause()
        for _ in range(10):
            app._tick()
            await asyncio.sleep(0.02)
        app.store._set_runs_cache(runs)
        app.store.switch_run(runs[-1])
        app._refresh_widgets()
        await app.wait_for_refresh()
        dirty, inputs, switches, search = [], [], [], []
        for _ in range(samples):
            started = time.perf_counter()
            app._refresh_widgets()
            await app.wait_for_refresh()
            dirty.append((time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            await pilot.press("right")
            await app.wait_for_refresh()
            inputs.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        app.store.search_query = "xxxx"
        app._refresh_widgets()
        await app.wait_for_refresh()
        search.append((time.perf_counter() - started) * 1000)
        app.store.clear_search()
        app.store.select_node(app.store.all_nodes[0])
        started = time.perf_counter()
        app.action_activate()
        await app.wait_for_refresh()
        trace_first_ms = (time.perf_counter() - started) * 1000
        app.store.close_trace_inspector()
        retained = []
        for i in range(20):
            started = time.perf_counter()
            app.store.switch_run(runs[-1] if i % 2 else runs[-2])
            app._refresh_widgets()
            await app.wait_for_refresh()
            switches.append((time.perf_counter() - started) * 1000)
            gc.collect()
            retained.append(
                {
                    "switch": i + 1,
                    "rss_bytes": current_rss_bytes(),
                    "app_cache_bytes": retained_bytes(
                        {key: val for key, val in vars(app.store).items() if key != "provider"}
                    ),
                }
            )
        log_widget = app._screen.query_one("#log-content")
        return {
            "fixture": {
                "history_rows": 10000,
                "dag_nodes": 1000,
                "log_records": 100000,
                "trace_iterations": 1000,
                "terminal": [160, 50],
            },
            "samples": {
                "input_to_paint_ms": inputs,
                "dirty_refresh_ms": dirty,
                "warm_switch_ms": switches,
                "search_ms": search,
            },
            "input_to_paint_p95_ms": percentile(inputs, 0.95),
            "dirty_refresh_p95_ms": percentile(dirty, 0.95),
            "warm_switch_p95_ms": percentile(switches, 0.95),
            "trace_first_ms": trace_first_ms,
            "retained_after_switches": retained,
            "rss_bytes": current_rss_bytes(),
            "app_cache_bytes": retained[-1]["app_cache_bytes"]
            + retained_bytes(log_widget.lines),
            "log_window_records": len(app.store.logs),
            "log_window_bytes": retained_bytes(app.store.logs),
            "queued_updates": app.store._background_updates.qsize(),
            "measurement": "headless Textual input dispatch through wait_for_refresh; "
            "provider fixture retained separately; ps current RSS; "
            "recursive UIStore data plus rendered log cache bytes",
        }


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
    parser.add_argument("--large-fixture", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--fixture-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--assert-budget", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.fixture_child:
        import faulthandler

        faulthandler.dump_traceback_later(120, repeat=True)
        print(json.dumps(asyncio.run(measure_large_fixture(args.samples))))
        return 0
    if args.large_fixture:
        import platform
        import subprocess
        import sys

        results = []
        for repetition in range(args.repetitions):
            try:
                result = json.loads(
                    subprocess.check_output(
                        [
                            sys.executable,
                            __file__,
                            "--fixture-child",
                            "--samples",
                            str(args.samples),
                        ],
                        text=True,
                        timeout=300,
                    )
                )
            except subprocess.TimeoutExpired:
                result = {
                    "status": "DNF",
                    "deadline_seconds": 300,
                    "reason": "fixed large fixture did not finish; metrics unmeasured",
                }
            results.append(result)
            if args.json:
                args.json.write_text(json.dumps({"repetitions": results}, indent=2))
            print(
                f"repetition {repetition + 1}: {result.get('status', 'complete')}",
                file=sys.stderr,
                flush=True,
            )
        report = {
            "platform": platform.platform(),
            "python": sys.version,
            "revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "repetitions": results,
            "targets": {
                "input_to_paint_p95_ms": 100,
                "dirty_refresh_p95_ms": 33,
                "warm_switch_p95_ms": 250,
                "app_cache_bytes": 64 * 2**20,
                "log_window_records": 10000,
                "log_window_bytes": 8 * 2**20,
                "queued_updates": 256,
            },
        }
        output = json.dumps(report, indent=2) + "\n"
        print(output)
        if args.json:
            args.json.write_text(output)
        if args.assert_budget:
            return int(
                any(
                    result[key] > target
                    for result in results
                    for key, target in report["targets"].items()
                )
            )
        return 0
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
