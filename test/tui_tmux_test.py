"""Real-terminal smoke for CLI launch, run controls, and agent drilldown."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from tui.app import NEXT_RUN_MARK, SCHEDULE_DISABLED_MARK
from tui.state import DISABLE_SCHEDULE_LABEL, ENABLE_SCHEDULE_LABEL

pytestmark = pytest.mark.tmux


def _tmux(*args: str) -> str:
    return subprocess.run(
        ["tmux", *args], check=True, capture_output=True, text=True, timeout=10
    ).stdout


def _wait_for(session: str, predicate) -> str:
    deadline = time.monotonic() + 15
    while True:
        screen = _tmux("capture-pane", "-t", session, "-p")
        if predicate(screen):
            return screen
        assert time.monotonic() < deadline, f"Expected terminal state did not appear:\n{screen}"
        time.sleep(0.1)


@pytest.fixture
def tui_session(request):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    session = f"pytest-tui-{uuid4().hex[:8]}"
    workflow = getattr(request, "param", "order_workflow")
    _tmux(
        "new-session",
        "-d",
        "-s",
        session,
        "-c",
        str(Path(__file__).resolve().parents[1]),
        "-x",
        "160",
        "-y",
        "40",
    )
    try:
        _tmux("resize-window", "-t", session, "-x", "160", "-y", "40")
        _tmux(
            "respawn-pane",
            "-k",
            "-t",
            session,
            f"COLUMNS=160 LINES=40 uv run ava tui {workflow}",
        )
        yield session
    finally:
        _tmux("kill-session", "-t", session)


def _confirm_empty_action(session: str) -> None:
    _wait_for(session, lambda text: "Preview validated input" in text)
    _tmux("send-keys", "-t", session, "Tab", "Enter")
    _wait_for(session, lambda text: "{}" in text)
    _tmux("send-keys", "-t", session, "Tab", "Enter")


def test_terminal_runtime_map_group_strip(tmp_path):
    import socket

    from runtime.operator.models import RunStatus
    from runtime.operator.operator import Operator
    from runtime.operator.server import serve as serve_operator

    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    workflow = Path(__file__).resolve().parents[1] / "examples/map_agent_fanout.py"
    operator = Operator([str(workflow)], watch=False, schedule=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = None
    session = f"pytest-map-{uuid4().hex[:8]}"
    try:
        server = serve_operator(operator, port=port, block=False)
        _tmux(
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            str(workflow.parents[1]),
            "-x",
            "200",
            "-y",
            "45",
            f"uv run ava tui --connect localhost:{port} --flow map_agent_pipeline",
        )
        _wait_for(session, lambda text: "write_article ×?" in text)
        run_id = operator.start_run("map_agent_pipeline")
        screen = _wait_for(
            session,
            lambda text: any(
                "write_article ×4" in line and "·" in line for line in text.splitlines()
            ),
        )
        assert "✓ ✓ ✓ ✓" not in next(
            line for line in screen.splitlines() if "write_article ×4" in line
        )
        deadline = time.monotonic() + 90
        while operator.get_run(run_id).status is not RunStatus.SUCCESS:
            run = operator.get_run(run_id)
            assert run.status is not RunStatus.FAILED, [entry.message for entry in run.logs]
            assert time.monotonic() < deadline
            time.sleep(0.1)
        _wait_for(session, lambda text: "✓ ✓ ✓ ✓" in text)
        # Select the mapped agent group and open its item list, then the ordinary trace.
        _tmux("send-keys", "-t", session, "Right", "Right", "Right", "Enter")
        _wait_for(session, lambda text: "Map items" in text)
        _tmux("send-keys", "-t", session, "Enter")
        _wait_for(session, lambda text: "AGENT TURN" in text)
    finally:
        if session in _tmux("list-sessions", "-F", "#{session_name}").splitlines():
            _tmux("kill-session", "-t", session)
        if server is not None:
            server.stop(grace=0).wait()
        operator.close()


def test_terminal_start_cancel_and_select_history(tui_session):
    screen = _wait_for(tui_session, lambda text: re.search(r"run_[0-9a-f-]+.*success", text))
    older = re.search(r"(run_[0-9a-f-]+).*success", screen).group(1)
    _tmux("send-keys", "-t", tui_session, "r")
    _confirm_empty_action(tui_session)
    screen = _wait_for(tui_session, lambda text: re.search(r"run_[0-9a-f-]+.*running", text))
    started = re.search(r"(run_[0-9a-f-]+).*running", screen).group(1)
    assert started != older
    _tmux("send-keys", "-t", tui_session, "x")
    _confirm_empty_action(tui_session)
    _wait_for(tui_session, lambda text: re.search(rf"{started}.*cancelled", text))

    _tmux("send-keys", "-t", tui_session, "d", "Down")
    _wait_for(
        tui_session,
        lambda text: any(
            older in line and "live updates" in line for line in text.splitlines()
        ),
    )
    _tmux("send-keys", "-t", tui_session, "Up")
    _wait_for(
        tui_session,
        lambda text: any(
            started in line and "live updates" in line for line in text.splitlines()
        ),
    )


@pytest.mark.parametrize("tui_session", ["agent_trace/inspect_agent"], indirect=True)
def test_terminal_agent_drilldown_and_return(tui_session):
    _wait_for(tui_session, lambda text: "inspect_agent" in text and "run_agent" in text)
    _tmux("send-keys", "-t", tui_session, "Enter")
    screen = _wait_for(tui_session, lambda text: "AGENT TURN" in text)
    assert "Run: agent-mock" in screen
    assert "Live record: complete" in screen
    assert "terminal=succeeded" in screen
    _tmux("send-keys", "-t", tui_session, "e")
    _wait_for(tui_session, lambda text: "Filter active records" in text)
    _tmux("send-keys", "-t", tui_session, "Right", "Enter")
    screen = _wait_for(tui_session, lambda text: '"active_count": 1' in text)
    assert "SANDBOX_STDOUT_SENTINEL" not in screen
    _tmux("send-keys", "-t", tui_session, "Escape")
    _wait_for(tui_session, lambda text: "Agent iteration.recorded" in text)


@pytest.mark.parametrize("tui_session", ["ingest_workflow"], indirect=True)
def test_terminal_schedule_enable_and_disable(tui_session):
    _wait_for(tui_session, lambda text: SCHEDULE_DISABLED_MARK in text)
    _tmux("send-keys", "-t", tui_session, "C-s")
    _wait_for(tui_session, lambda text: ENABLE_SCHEDULE_LABEL in text)

    _tmux("send-keys", "-t", tui_session, "1")
    _confirm_empty_action(tui_session)
    _wait_for(tui_session, lambda text: DISABLE_SCHEDULE_LABEL in text)

    _tmux("send-keys", "-t", tui_session, "Escape")
    screen = _wait_for(tui_session, lambda text: NEXT_RUN_MARK in text)
    assert SCHEDULE_DISABLED_MARK not in screen
