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

Linux reads process information from ``/proc``, so no external program is needed.
Other POSIX systems (macOS) run the ``ps`` found on ``PATH``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, PositiveInt

_PROC = Path("/proc")
_PS_TIMEOUT_SECONDS = 5.0
DEFAULT_CANCEL_GRACE_SECONDS = 5.0
"""How long a run's processes get between SIGTERM and SIGKILL by default."""

_POLL_INTERVAL_SECONDS = 0.05
# A fixed locale and time zone, so the same process always reports the same start.
_PS_ENVIRONMENT = {"LC_ALL": "C", "TZ": "UTC"}


class RunProcess(BaseModel):
    """The coordinator process group of one run, as recorded when it started."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    process_group: PositiveInt
    # The group leader's start time: clock ticks after boot from ``/proc`` on Linux,
    # ``ps -o lstart`` elsewhere.
    started: str
    # When the machine and process 1 started, which changes when the machine (or
    # container) restarts. After a restart none of the run's processes can be alive,
    # and a process group with the recorded ID belongs to something else.
    booted: str


class RunProcessError(RuntimeError):
    """Base class for failures to find or stop a run's processes."""


class RunProcessLookupError(RunProcessError):
    """The operating system's process information could not be read."""


class RunProcessStopError(RunProcessError):
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
    leader = _process(pid)
    if leader is None or not leader.live:
        return None
    return RunProcess(process_group=pid, started=leader.started, booted=_booted())


def stop_run_process(process: RunProcess, *, grace: float) -> bool:
    """Stop a previous operator's run process group if it is still alive.

    Sends SIGTERM to the group, then SIGKILL after ``grace`` seconds. Returns
    whether anything was stopped. Nothing is stopped when the machine restarted since
    the run started, since none of its processes can have survived that, or when the
    group leader is alive but started at another time: then an unrelated process
    reused the ID. Raises :class:`RunProcessStopError` when live processes remain.
    """
    if os.name == "nt":
        return False
    if _booted() != process.booted:
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
        info for info in _all_processes() if info.process_group == process_group and info.live
    )


def _process(pid: int) -> _ProcessInfo | None:
    """The process with this ID, or ``None`` when it does not exist."""
    if sys.platform == "linux":
        return _proc_process(pid)
    return next(iter(_ps_processes("-p", str(pid))), None)


def _all_processes() -> tuple[_ProcessInfo, ...]:
    if sys.platform == "linux":
        try:
            entries = tuple(entry.name for entry in os.scandir(_PROC) if entry.name.isdigit())
        except OSError as exc:
            raise RunProcessLookupError(f"Could not list processes in {_PROC}: {exc}") from exc
        return tuple(
            info for info in (_proc_process(int(name)) for name in entries) if info is not None
        )
    return _ps_processes("-ax")


def _booted() -> str:
    """Identify the current boot of the machine (or container)."""
    if sys.platform == "linux":
        # The kernel's boot ID, which stays fixed until a reboot (unlike its boot
        # time, which follows wall-clock steps), plus process 1's start, which
        # changes when a container restarts on a host that did not.
        boot_id = _read_proc(_PROC / "sys" / "kernel" / "random" / "boot_id").strip()
        if not boot_id:
            raise RunProcessLookupError(f"{_PROC / 'sys/kernel/random/boot_id'} is empty")
        init = _proc_process(1)
        if init is None:
            raise RunProcessLookupError("Process 1 is not visible in /proc")
        return f"{boot_id}:{init.started}"
    (init,) = _ps_processes("-p", "1")
    return init.started


def _proc_process(pid: int) -> _ProcessInfo | None:
    path = _PROC / str(pid) / "stat"
    try:
        text = path.read_text()
    except (FileNotFoundError, ProcessLookupError):
        # The process does not exist, or exited while it was being read.
        return None
    except OSError as exc:
        raise RunProcessLookupError(f"Could not read {path}: {exc}") from exc
    # The command name is in parentheses and may itself contain spaces or ")".
    fields = text[text.rindex(")") + 2 :].split()
    # Fields 3 (state), 5 (process group) and 22 (start time) of proc_pid_stat(5).
    return _ProcessInfo(
        pid=pid, process_group=int(fields[2]), state=fields[0], started=fields[19]
    )


def _read_proc(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise RunProcessLookupError(f"Could not read {path}: {exc}") from exc


def _ps_processes(*selection: str) -> tuple[_ProcessInfo, ...]:
    ps = shutil.which("ps")
    if ps is None:
        raise RunProcessLookupError(
            "The ps command was not found on PATH; the operator needs it to record "
            "and stop run processes."
        )
    try:
        completed = subprocess.run(
            [ps, *selection, "-o", "pid=,pgid=,stat=,lstart="],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
            env=_PS_ENVIRONMENT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunProcessLookupError(f"Could not run {ps}: {exc}") from exc
    # ps exits 1 when a selected process does not exist, with nothing on stderr.
    if completed.returncode not in (0, 1) or completed.stderr.strip():
        raise RunProcessLookupError(
            f"{ps} failed with exit status {completed.returncode}: {completed.stderr.strip()}"
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
