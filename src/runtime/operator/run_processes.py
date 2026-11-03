"""Identify a run's coordinator process group, and stop it after an operator restart.

Each run coordinator starts its own session, so its process ID is also the ID of
the process group holding the run's whole process tree. The operator records
that ID together with the coordinator's start time when the run starts. After a
crash or ``kill -9`` the coordinator outlives the operator, so the next operator
stops the group before it marks the run failed. The start time tells the
recorded coordinator apart from an unrelated process that later reused its ID.

On Windows a run's processes belong to a kill-on-close Job Object owned by the
operator, so the operating system already ends them when the operator dies and
nothing is recorded.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, PositiveInt

_PS = "/bin/ps"
_PS_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05
# A fixed locale and time zone, so the same process always reports the same start.
_PS_ENVIRONMENT = {"LC_ALL": "C", "TZ": "UTC"}


class RunProcess(BaseModel):
    """The coordinator process group of one run, as recorded when it started."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    process_group: PositiveInt
    # The group leader's start time as ``ps -o lstart`` reports it.
    started: str


class RunProcessStopError(RuntimeError):
    """A previous operator's run processes are still alive and could not be stopped."""


@dataclass(frozen=True)
class _ProcessInfo:
    pid: int
    process_group: int
    state: str
    started: str

    @property
    def live(self) -> bool:
        # Zombies and dead processes have already stopped running code.
        return not self.state.startswith(("Z", "X"))


def identify_run_process(pid: int) -> RunProcess | None:
    """Record the process group a freshly started coordinator leads.

    Returns ``None`` on Windows, where the run's Job Object ends its processes
    with the operator, and when the coordinator has already exited, since it
    then leaves nothing to stop.
    """
    if os.name == "nt":
        return None
    leader = next((info for info in _processes() if info.pid == pid), None)
    if leader is None or not leader.live:
        return None
    return RunProcess(process_group=pid, started=leader.started)


def stop_run_process(process: RunProcess, *, grace: float) -> bool:
    """Stop a previous operator's run process group if it is still alive.

    Sends SIGTERM to the group, then SIGKILL after ``grace`` seconds. Returns
    whether anything was stopped. A group whose leader is alive but started at
    another time belongs to an unrelated process that reused the ID, and is left
    alone. Raises :class:`RunProcessStopError` when live processes remain.
    """
    if os.name == "nt":
        return False
    members = _live_members(process.process_group)
    if not members:
        return False
    leader = next((info for info in members if info.pid == process.process_group), None)
    if leader is not None and leader.started != process.started:
        return False
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.process_group, signal_number)
        except ProcessLookupError:
            return True
        except PermissionError as exc:
            raise RunProcessStopError(
                f"Could not stop run process group {process.process_group} left by a "
                f"previous operator: {exc}. Stop it, then start the operator again."
            ) from exc
        deadline = time.monotonic() + grace
        while _live_members(process.process_group):
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
        else:
            return True
    raise RunProcessStopError(
        f"Run process group {process.process_group} left by a previous operator is still "
        "alive after SIGKILL. Stop it, then start the operator again."
    )


def _live_members(process_group: int) -> tuple[_ProcessInfo, ...]:
    return tuple(
        info for info in _processes() if info.process_group == process_group and info.live
    )


def _processes() -> tuple[_ProcessInfo, ...]:
    completed = subprocess.run(
        [_PS, "-axo", "pid=,pgid=,stat=,lstart="],
        check=True,
        capture_output=True,
        text=True,
        timeout=_PS_TIMEOUT_SECONDS,
        env=_PS_ENVIRONMENT,
    )
    processes = []
    for line in completed.stdout.splitlines():
        pid, process_group, state, started = line.split(maxsplit=3)
        processes.append(
            _ProcessInfo(
                pid=int(pid),
                process_group=int(process_group),
                state=state,
                started=" ".join(started.split()),
            )
        )
    return tuple(processes)
