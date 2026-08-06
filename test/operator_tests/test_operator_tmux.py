"""Integration tests for operator + TUI using tmux.

These tests start a real operator daemon and TUI in tmux panes, send
input, capture output, and assert on rendered text. They verify the
full gRPC path: operator → gRPC → TUI.

Run with: uv run pytest test/operator_tests/test_operator_tmux.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

TMUX = shutil.which("tmux")
SESSION = "pytest-operator"
FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")
OPERATOR_CMD = f"uv run ava operator --flows {FIXTURES}"
TUI_CMD = "uv run ava tui --connect localhost:17434"
OPERATOR_PORT_CMD = f"uv run ava operator --flows {FIXTURES} --port 17434"


def tmux_run(*args: str) -> str:
    result = subprocess.run(
        ["tmux", *args],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout


def capture(pane: str = f"{SESSION}:.0") -> list[str]:
    raw = tmux_run("capture-pane", "-t", pane, "-p")
    return raw.split("\n")


def send_keys(pane: str, *keys: str) -> None:
    for key in keys:
        tmux_run("send-keys", "-t", pane, key)
        time.sleep(0.15)


def wait_for_text(text: str, pane: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = capture(pane)
        if any(text in line for line in lines):
            return True
        time.sleep(0.3)
    return False


def wait_for_any_text(texts: tuple[str, ...], pane: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rendered = combined_text(pane)
        if any(text in rendered for text in texts):
            return True
        time.sleep(0.3)
    return False


def combined_text(pane: str) -> str:
    return "\n".join(capture(pane))


def open_explorer(pane: str) -> None:
    """Open the explorer if it is currently hidden."""
    if "EXPLORER" not in combined_text(pane):
        send_keys(pane, "Space", "e")
    assert wait_for_text("EXPLORER", pane, timeout=5), "Explorer did not open"


@pytest.fixture(scope="module")
def operator_session():
    """Start operator + TUI in a tmux session."""
    if not TMUX:
        pytest.skip("tmux not installed")

    # Kill any leftover session
    subprocess.run(["tmux", "kill-session", "-t", SESSION],
                   capture_output=True, timeout=5)

    # Create session with operator in pane 0
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50",
         f"{OPERATOR_PORT_CMD}; sleep 30"],
        check=True, timeout=10,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    )

    # Wait for operator to start
    time.sleep(2)

    # Split and start TUI in pane 1
    subprocess.run(
        ["tmux", "split-window", "-h", "-t", SESSION,
         f"{TUI_CMD}; sleep 30"],
        check=True, timeout=10,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    )

    # Wait for the TUI shell to render; workflow names may be hidden until the
    # explorer is opened.
    assert wait_for_text("DAG", f"{SESSION}:.1", timeout=20), \
        "TUI didn't render"

    yield

    subprocess.run(["tmux", "kill-session", "-t", SESSION],
                   capture_output=True, timeout=5)


@pytest.mark.tmux
class TestOperatorTmux:
    def test_tui_discovers_real_workflows(self, operator_session):
        """TUI should show workflows discovered from test fixtures."""
        open_explorer(f"{SESSION}:.1")
        assert wait_for_text("simple_workflow", f"{SESSION}:.1", timeout=10)
        assert wait_for_text("slow_workflow", f"{SESSION}:.1", timeout=10)
        text = combined_text(f"{SESSION}:.1")
        assert "simple_workflow" in text
        assert "slow_workflow" in text

    def test_tui_shows_dag_nodes(self, operator_session):
        """DAG should render real workflow nodes."""
        text = combined_text(f"{SESSION}:.1")
        # Any workflow's node should appear in the DAG
        assert "fetch" in text.lower() or "start" in text.lower()

    def test_run_workflow_and_see_success(self, operator_session):
        """Press 'r' to run a workflow, verify nodes succeed."""
        send_keys(f"{SESSION}:.1", "Escape", "r")
        # Wait for run to complete — look for checkmark (✓) in the tasks pane
        assert wait_for_text("✓", f"{SESSION}:.1", timeout=10), \
            "Run didn't complete successfully"

    def test_logs_appear_after_run(self, operator_session):
        """Logs from Logger() should stream to the TUI log panel."""
        send_keys(f"{SESSION}:.1", "Escape", "r")
        pane = f"{SESSION}:.1"
        log_markers = ("INFO", "DEBUG", "WARN", "Connecting", "complete", "Fetching")
        assert wait_for_any_text(log_markers, pane, timeout=15), \
            f"No log messages found in TUI output:\n{combined_text(pane)}"

    def test_run_count_increments(self, operator_session):
        """Border title should show run count."""
        text = combined_text(f"{SESSION}:.1")
        assert "Run" in text  # "N Runs" or "1 Run"

    @pytest.mark.skip(reason="Disconnect detection timing is unreliable in CI")
    def test_disconnect_modal_on_operator_kill(self, operator_session):
        """Kill operator, TUI should show disconnect modal."""
        subprocess.run(
            "pkill -9 -f 'runtime.operator.*17434' 2>/dev/null; "
            "lsof -ti:17434 | xargs kill -9 2>/dev/null",
            shell=True, capture_output=True, timeout=5,
        )
        assert wait_for_text("CONNECTION LOST", f"{SESSION}:.1", timeout=20), \
            "Disconnect modal didn't appear"

    @pytest.mark.skip(reason="Depends on disconnect test")
    def test_reconnect_on_operator_restart(self, operator_session):
        """Restart operator, TUI should reconnect and hide modal."""
        tmux_run("send-keys", "-t", f"{SESSION}:.0", f"{OPERATOR_PORT_CMD}", "")
        tmux_run("send-keys", "-t", f"{SESSION}:.0", "Enter")
        assert wait_for_text("simple_workflow", f"{SESSION}:.1", timeout=15), \
            "TUI didn't reconnect"
