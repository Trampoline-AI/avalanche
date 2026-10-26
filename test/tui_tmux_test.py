"""Real-terminal smoke for CLI launch, run controls, and agent drilldown."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

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
    assert "terminal=completed" in screen
    _tmux("send-keys", "-t", tui_session, "e")
    _wait_for(tui_session, lambda text: "Filter active records" in text)
    _tmux("send-keys", "-t", tui_session, "Right", "Enter")
    screen = _wait_for(tui_session, lambda text: '"active_count": 1' in text)
    assert "SANDBOX_STDOUT_SENTINEL" not in screen
    _tmux("send-keys", "-t", tui_session, "Escape")
    _wait_for(tui_session, lambda text: "Agent iteration.recorded" in text)


def test_terminal_optional_navigation_stays_beside_resource_views(tmp_path):
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / "navigation.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(root / 'test')!r})\n"
        "from tui_test import _RevampProvider, _NavigationFixture\n"
        "from tui.app import AvalancheApp\n"
        "provider = _RevampProvider()\n"
        "provider.tui_extensions = _NavigationFixture(provider)\n"
        "AvalancheApp(provider=provider, read_only=True).run()\n"
    )
    session = f"pytest-navigation-{uuid4().hex[:8]}"
    _tmux("new-session", "-d", "-s", session, "-c", str(root), "-x", "140", "-y", "40")
    try:
        _tmux("respawn-pane", "-k", "-t", session, f"uv run python {script}")
        _wait_for(session, lambda text: "Overview" in text and "Organization" in text)
        _tmux("send-keys", "-t", session, "c", "Down", "Enter")
        screen = _wait_for(session, lambda text: "acme › demo" in text and "Overview" in text)
        assert "Organization" in screen and "unknown:new-state" in screen
        _tmux("send-keys", "-t", session, "h")
        _wait_for(session, lambda text: "acme › demo" not in text and "Overview" in text)
        _tmux("send-keys", "-t", session, "C-c")
    finally:
        _tmux("kill-session", "-t", session)
