"""Integration tests using tmux for real terminal rendering.

These tests start the actual TUI in a tmux session, send input, capture
output, and assert on the rendered characters. They catch issues that
headless Textual pilot tests miss (scrollbar rendering, click coordinates,
text wrapping).

Run with: uv run pytest test/tui_tmux_test.py -v
Skip with: uv run pytest -m "not tmux"
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

TMUX = shutil.which("tmux")
SESSION = "pytest-tui"
TUI_CMD = "uv run ava tui"


def tmux(*args: str, input: str | None = None) -> str:
    """Run a tmux command and return stdout."""
    result = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        timeout=10,
        input=input,
    )
    return result.stdout


def capture() -> list[str]:
    """Capture the current tmux pane as a list of lines."""
    raw = tmux("capture-pane", "-t", SESSION, "-p")
    return raw.split("\n")


def send_keys(*keys: str) -> None:
    """Send keys to the tmux session."""
    for key in keys:
        tmux("send-keys", "-t", SESSION, key)
        time.sleep(0.15)


def start_tui(args: str = "", *, width: int = 100, height: int = 40) -> None:
    """Start the TUI in a fresh tmux session."""
    subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True, timeout=5)
    command = f"COLUMNS={width} LINES={height} {TUI_CMD}"
    if args:
        command = f"{command} {args}"
    subprocess.run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            SESSION,
            "-x",
            str(width),
            "-y",
            str(height),
        ],
        check=True,
        timeout=10,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )
    subprocess.run(
        ["tmux", "resize-window", "-t", SESSION, "-x", str(width), "-y", str(height)],
        check=True,
        timeout=10,
    )
    actual_size = tmux(
        "display-message",
        "-p",
        "-t",
        SESSION,
        "#{window_width}x#{window_height}",
    ).strip()
    assert (
        actual_size == f"{width}x{height}"
    ), f"tmux session has size {actual_size!r}, expected {width}x{height}"
    subprocess.run(
        ["tmux", "respawn-pane", "-k", "-t", SESSION, command],
        check=True,
        timeout=10,
    )
    assert wait_for("avalanche", timeout=15), "TUI did not start"


def restart_tui(args: str = "", *, width: int = 100, height: int = 40) -> None:
    """Restart the tmux session with optional TUI CLI args."""
    start_tui(args, width=width, height=height)


def open_explorer() -> None:
    """Open the explorer if it is currently hidden."""
    if not find_text("EXPLORER"):
        send_keys("Space", "e")
    assert wait_for("EXPLORER", timeout=5), "Explorer did not open"


def find_text(text: str, *, min_row: int = 0) -> tuple[int, int] | None:
    """Find the (row, char_col) of text in the captured pane. 1-indexed rows.

    *min_row* (0-indexed) lets callers skip rows so a match in the run-detail
    pane doesn't shadow the same label in the DAG below.
    """
    lines = capture()
    for i, line in enumerate(lines):
        if i < min_row:
            continue
        col = line.find(text)
        if col >= 0:
            return (i + 1, col)  # tmux uses 1-indexed rows
    return None


def wait_for(text: str, timeout: float = 5.0) -> bool:
    """Wait until text appears in the pane output."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find_text(text):
            return True
        time.sleep(0.2)
    return False


def wait_for_all_texts(*texts: str, timeout: float = 5.0) -> bool:
    """Wait until every text appears in the current pane output."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rendered = "\n".join(capture())
        if all(text in rendered for text in texts):
            return True
        time.sleep(0.2)
    return False


def status_line() -> str:
    """Return the rendered status line, ignoring tmux's trailing blank rows."""
    return next((line for line in reversed(capture()) if "· live updates" in line), "")


def wait_for_status_text(text: str, timeout: float = 5.0) -> bool:
    """Wait until the status line identifies the expected selection."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if text in status_line():
            return True
        time.sleep(0.2)
    return False


def _wait_for_status_run(run_id: str, timeout: float = 5.0) -> bool:
    """Wait until the status bar identifies the selected run."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if run_id in status_line():
            return True
        time.sleep(0.2)
    return False


def assert_node_selected(node_name: str) -> None:
    """Assert the selected node is visible in a selection-specific UI surface."""
    lines = capture()
    for line in lines:
        if node_name in line and ("Logs:" in line or "›" in line):
            return
    combined = "\n".join(lines)
    pytest.fail(f"Expected {node_name} selected in status/log title, got:\n{combined}")


@pytest.fixture(scope="module")
def tui_session():
    """Start a tmux session with the TUI, yield, then kill it."""
    if not TMUX:
        pytest.skip("tmux not installed")

    start_tui()

    yield SESSION

    subprocess.run(["tmux", "kill-session", "-t", SESSION], capture_output=True, timeout=5)


@pytest.mark.tmux
class TestTmuxRendering:
    """Tests that verify real terminal rendering via tmux."""

    def test_tui_starts_with_all_panes(self, tui_session):
        """All pane border titles should be visible."""
        open_explorer()
        lines = capture()
        combined = "\n".join(lines)
        assert "EXPLORER" in combined
        assert "Run" in combined
        assert "DAG" in combined
        assert "Logs" in combined

    def test_dag_renders_all_branches(self, tui_session):
        """Deep-link to ml_workflow and verify all 3 parallel branches render."""
        restart_tui("ml_workflow", width=160)
        assert wait_for_all_texts(
            "fetch_training",
            "fetch_validation",
            "fetch_features",
            timeout=15,
        )
        lines = capture()
        combined = "\n".join(lines)
        assert "fetch_training" in combined, "Missing fetch_training branch"
        assert "fetch_validation" in combined, "Missing fetch_validation branch"
        assert "fetch_features" in combined, "Missing fetch_features branch"

    def test_agent_dag_omits_embedded_control_legend(self, tui_session):
        """The DAG does not embed a control legend beneath its graph."""
        restart_tui("agent_trace", width=100)
        assert wait_for("inspect_agent_1", timeout=8)

        combined = "\n".join(capture())
        assert "Click or" not in combined
        assert "Enter inspect" not in combined
        assert "(agent) agent step" not in combined

    def test_deep_link_node_selects_status_bar(self, tui_session):
        """Deep-linking to a DAG node should show it in the status bar."""
        restart_tui("order_workflow/validate", width=220)
        assert wait_for_status_text("validate", timeout=8)

    def test_deep_link_workflow(self, tui_session):
        """Deep-link CLI arg should select the specified workflow."""
        restart_tui("ml_workflow")
        assert wait_for_all_texts("fetch_training", "preprocess", timeout=15)

        lines = capture()
        combined = "\n".join(lines)
        # DAG should show ml_workflow nodes, not order_workflow
        assert (
            "fetch_training" in combined or "preprocess" in combined
        ), "Deep-link to ml_workflow should show its DAG"

    def test_deep_link_notify_slack(self, tui_session):
        """Deep-linking notify_slack in ml_workflow should select it."""
        restart_tui("ml_workflow/notify_slack", width=260)
        assert wait_for("notify_slack", timeout=8)
        assert_node_selected("notify_slack")

    def test_arrow_nav_from_export_onnx(self, tui_session):
        """From export_onnx, arrow down should go to deploy_staging, then deploy_prod."""
        # Start on ml_workflow with export_onnx selected
        restart_tui("ml_workflow/export_onnx", width=160)
        assert wait_for("export_onnx", timeout=8)
        time.sleep(1)

        # Verify export_onnx is selected
        assert_node_selected("export_onnx")

        # Arrow down → deploy_staging
        send_keys("Down")
        time.sleep(0.5)
        assert_node_selected("deploy_staging")

        # Arrow down again → deploy_prod
        send_keys("Down")
        time.sleep(0.5)
        assert_node_selected("deploy_prod")

    def test_arrow_right_from_deploy_staging_to_adjacent_notify(self, tui_session):
        """Right from deploy_staging should select the notify_slack on the same row."""
        restart_tui("ml_workflow/deploy_staging", width=220)
        assert wait_for("deploy_staging", timeout=8)
        time.sleep(1)

        # Verify deploy_staging is selected
        assert_node_selected("deploy_staging")

        # Arrow right → should select the notify_slack on the SAME visual row
        send_keys("Right")
        time.sleep(0.5)
        assert_node_selected("notify_slack")

        # Now go left back to deploy_staging (not some other node)
        send_keys("Left")
        time.sleep(0.5)
        assert_node_selected("deploy_staging")

    def test_agent_trace_inspector_real_terminal_contract(self, tui_session):
        """Agent hierarchy remains responsive in a real terminal."""
        restart_tui("agent_trace/inspect_agent", width=100, height=35)
        assert wait_for("inspect_agent", timeout=8)
        open_explorer()

        send_keys("Escape", "Enter")
        assert wait_for_all_texts("Expand all", "Collapse all", timeout=5)

        send_keys("e")
        assert wait_for_all_texts("Expand all", "Collapse all", timeout=15)
        send_keys("z")
        time.sleep(0.3)
        assert "Reasoning" not in "\n".join(capture())

        send_keys("Enter")
        assert wait_for("Reasoning", timeout=5)
        send_keys("Down", "Enter")
        assert wait_for("Filter active records", timeout=5)
        send_keys("Down", "Enter")
        assert wait_for("records =", timeout=5)
        send_keys("Down", "Enter", "o")
        assert wait_for("FULL OUTPUT", timeout=5)

        send_keys("Right")
        assert wait_for("AGENT OUTPUT", timeout=5)
        combined = "\n".join(capture())
        assert "summary" in combined
        assert "active_count" not in combined
        send_keys("Enter")
        assert "SANDBOX_STDOUT_SENTINEL" not in "\n".join(capture())
        time.sleep(0.5)

        send_keys("Right")
        assert wait_for("AGENT METADATA", timeout=5)

        send_keys("Right")
        assert wait_for("STRUCTURED TRACE", timeout=5)
        assert wait_for("FULL OUTPUT", timeout=5)

        send_keys("Escape")
        assert wait_for("Logs", timeout=5)

    def test_run_controls_legend_and_bindings_in_real_terminal(self, tui_session):
        """Runs legend remains visible while its advertised shortcuts control runs."""
        restart_tui(width=100, height=40)
        combined = "\n".join(capture())
        for key_label in ("[r] Run", "[x] Stop", "[a] Actions", "[↑↓] Select"):
            assert key_label in combined

        run_header = find_text("Run ID")
        assert run_header is not None
        send_keys("r")
        assert wait_for("running", timeout=5)
        send_keys("a")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            opened_header = find_text("Run ID")
            if opened_header is not None and opened_header[0] > run_header[0]:
                break
            time.sleep(0.2)
        else:
            pytest.fail("Actions shortcut did not expand the Runs pane")
        send_keys("x")
        assert wait_for("cancelled", timeout=5)
        run_ids = [
            next(token for token in line.split() if token.startswith("run_"))
            for line in capture()
            if "run_" in line and any(status in line for status in ("cancelled", "success"))
        ]
        assert len(run_ids) >= 2
        newest_run_id, older_run_id = run_ids[:2]

        send_keys("d", "Down")
        assert _wait_for_status_run(older_run_id)
        send_keys("Up")
        assert _wait_for_status_run(newest_run_id)

        restart_tui(width=50, height=15)
        combined = "\n".join(capture())
        for key_label in ("[r] Run", "[x] Stop", "[a] Actions", "[↑↓] Select"):
            assert key_label in combined
        send_keys("a")
        time.sleep(0.3)
        assert "[a] Actions" in "\n".join(capture())
        assert not find_text("Stop selected run")
