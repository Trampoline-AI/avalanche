"""``ava state``: inspect and clear what the local operator keeps in its state database."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime

from runtime.operator.run_processes import RunProcessError
from runtime.operator.state import StateStoreError
from runtime.operator.state.cli import add_state_db_argument
from runtime.operator.state.maintenance import (
    VACUUM_FREE_FRACTION,
    DeletedRun,
    StateMaintenance,
)

STATE_HELP = "inspect and clear the operator's stored runs, results and files"
STATE_DESCRIPTION = (
    "Inspect and clear what the local operator keeps in its state database and the "
    "folders next to it. Nothing is deleted automatically. These commands refuse to run "
    "while an operator uses the database; stop it first."
)


def add_state_commands(state: argparse.ArgumentParser) -> None:
    """Add ``info``, ``delete``, ``prune`` and ``clear`` to the ``ava state`` parser."""
    commands = state.add_subparsers(dest="state_command", metavar="COMMAND", required=True)

    info = commands.add_parser(
        "info", help="show the database, its runs and the size of stored files"
    )
    info.set_defaults(handler=_run_info)

    delete = commands.add_parser(
        "delete", help="delete runs with their details, results and input files"
    )
    delete.add_argument("run_ids", nargs="+", metavar="RUN_ID", help="run to delete")
    delete.set_defaults(handler=_run_delete)

    prune = commands.add_parser(
        "prune",
        help="delete old finished runs; unfinished runs and schedules are kept",
    )
    selection = prune.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--before",
        type=_cutoff,
        metavar="DATE",
        help=(
            "delete finished runs triggered before this ISO 8601 date or date-time "
            "(local time unless it names a time zone)"
        ),
    )
    selection.add_argument(
        "--keep-last",
        type=_count,
        metavar="N",
        help="keep the N most recent finished runs and delete the older ones",
    )
    prune.set_defaults(handler=_run_prune)

    clear = commands.add_parser(
        "clear", help="delete every run, result and input file after a confirmation"
    )
    clear.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    clear.add_argument(
        "--include-schedules",
        action="store_true",
        help="also forget every schedule's on/off state, input and input files",
    )
    clear.set_defaults(handler=_run_clear)

    for command in (info, delete, prune, clear):
        add_state_db_argument(command)


def _run_info(args: argparse.Namespace) -> int:
    return _with_state(args, _print_info)


def _run_delete(args: argparse.Namespace) -> int:
    def delete(state: StateMaintenance) -> int:
        state.delete(args.run_ids, on_deleted=_print_deleted)
        _print_reclaimed(state)
        return 0

    return _with_state(args, delete)


def _run_prune(args: argparse.Namespace) -> int:
    def prune(state: StateMaintenance) -> int:
        if args.before is not None:
            pruned = state.prune_before(args.before, on_deleted=_print_deleted)
        else:
            pruned = state.prune_keep_last(args.keep_last, on_deleted=_print_deleted)
        print(
            f"Pruned {_plural(len(pruned.deleted), 'finished run')}; "
            f"{_plural(pruned.unfinished_kept, 'unfinished run')} left alone."
        )
        if pruned.deleted:
            _print_reclaimed(state)
        return 0

    return _with_state(args, prune)


def _run_clear(args: argparse.Namespace) -> int:
    def clear(state: StateMaintenance) -> int:
        if not args.yes and not _confirm_clear(state, args.include_schedules):
            print("Nothing was cleared.")
            return 1
        cleared = state.clear(include_schedules=args.include_schedules)
        for stopped in cleared.stopped_processes:
            print(
                f"Stopped process group {stopped.process_group} left by unfinished run "
                f"{stopped.run_id}."
            )
        print(
            f"Deleted {_plural(cleared.runs, 'run')}, "
            f"{_plural(cleared.result_bundles, 'result bundle')} and "
            f"{_plural(cleared.input_file_folders, 'input-file folder')}."
        )
        if args.include_schedules:
            print(
                f"Deleted {_plural(cleared.schedules_deleted, 'schedule')} and "
                f"{_plural(cleared.schedule_input_folders, 'schedule input-file folder')}."
            )
        else:
            print(
                f"Kept {_plural(cleared.schedules_kept, 'schedule')} "
                "(--include-schedules removes them); removed "
                f"{_plural(cleared.schedule_input_folders, 'schedule input-file folder')} "
                "no schedule uses."
            )
        _print_reclaimed(state, always_vacuum=True)
        return 0

    return _with_state(args, clear)


def _with_state(args: argparse.Namespace, command: Callable[[StateMaintenance], int]) -> int:
    try:
        with StateMaintenance.open(args.state_db) as state:
            return command(state)
    except (StateStoreError, RunProcessError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _confirm_clear(state: StateMaintenance, include_schedules: bool) -> bool:
    info = state.info()
    kept = "including" if include_schedules else "keeping"
    print(
        f"This deletes all {_plural(info.runs, 'run')} in {state.database} with their "
        f"logs, events, traces, evaluations, results and input files, {kept} "
        f"{_plural(info.schedules, 'schedule')}."
    )
    try:
        answer = input("Continue? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _print_info(state: StateMaintenance) -> int:
    info = state.info()
    if info.removed_pending_bundles:
        print(
            f"Removed {_plural(info.removed_pending_bundles, 'unfinished result bundle')} "
            "left by runs that never completed, as an operator start does."
        )
    statuses = ", ".join(
        f"{count} {status.value}"
        for status, count in sorted(info.runs_by_status.items(), key=lambda item: item[0].value)
    )
    rows = [
        ("Database", str(info.database)),
        ("Database size", _size(info.database_bytes)),
        ("Runs", f"{info.runs} ({statuses})" if statuses else "0"),
        ("Schedules", str(info.schedules)),
        (
            "Results",
            f"{_plural(info.results.entries, 'stored result')}, "
            f"{_size(info.results.size_bytes)} in {info.results.path}",
        ),
        (
            "Input files",
            f"{_plural(info.input_files.entries, 'folder')}, "
            f"{_size(info.input_files.size_bytes)} in {info.input_files.path}",
        ),
        (
            "Schedule inputs",
            f"{_plural(info.schedule_inputs.entries, 'folder')}, "
            f"{_size(info.schedule_inputs.size_bytes)} in {info.schedule_inputs.path}",
        ),
    ]
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        print(f"{label + ':':<{width}} {value}")
    if info.unfinished_runs:
        print(
            f"{_plural(info.unfinished_runs, 'run')} did not finish before the operator "
            "stopped; the next operator start marks them failed."
        )
    leftovers = [
        *(
            f"stored result of run {row.run_id} whose bundle {row.bundle_name} is missing "
            "(deleting the run removes it)"
            for row in info.result_index.missing_bundles
        ),
        *(
            f"result bundle no stored result refers to: {info.results.path / name}"
            for name in info.result_index.unindexed_bundles
        ),
        *(f"input-file folder no run uses: {path}" for path in info.unreferenced_input_files),
        *(
            f"schedule input-file folder no schedule uses: {path}"
            for path in info.unreferenced_schedule_inputs
        ),
    ]
    unexpected = [
        *(info.results.path / name for name in info.result_index.unexpected_entries),
        *info.unexpected_entries,
    ]
    if leftovers:
        print("Leftovers (`ava state clear` removes them):")
        for leftover in leftovers:
            print(f"  {leftover}")
    if unexpected:
        print("Entries the operator did not create (never removed):")
        for path in unexpected:
            print(f"  {path}")
    return 0


def _print_deleted(run: DeletedRun) -> None:
    details = run.details
    removed = [
        _plural(details.logs, "log"),
        _plural(details.agent_events, "agent event"),
        _plural(details.classifier_events, "classifier event"),
        _plural(details.trace_bodies, "trace body", "trace bodies"),
        _plural(details.evaluations, "evaluation"),
        "its result bundle" if run.result_bundle is not None else "no result bundle",
        _plural(len(run.input_file_folders), "input-file folder"),
    ]
    print(f"Deleted run {run.run_id} ({run.status.value}): {', '.join(removed)}.")


def _print_reclaimed(state: StateMaintenance, *, always_vacuum: bool = False) -> None:
    space = state.reclaim_space(always_vacuum=always_vacuum)
    sizes = f"{_size(space.bytes_before)} -> {_size(space.bytes_after)}"
    if space.vacuumed:
        print(f"Reclaimed disk space with VACUUM: database {sizes}.")
        return
    print(
        f"Database {sizes}; {_size(space.free_bytes)} of free space stays in it for reuse "
        f"(VACUUM runs once at least {VACUUM_FREE_FRACTION:.0%} of it is free)."
    )


def _cutoff(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not an ISO 8601 date or date-time, such as 2026-11-01 or "
            "2026-11-01T09:30"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from exc
    if count < 0:
        raise argparse.ArgumentTypeError("the number of runs to keep cannot be negative")
    return count


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


def _size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    size = size_bytes / 1024
    for unit in ("KiB", "MiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"
