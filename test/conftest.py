"""Shared pytest configuration for the avalanche test suite."""

import shutil
import subprocess

import pytest


@pytest.fixture(scope="session", autouse=True)
def _shutdown_ray_at_session_end():
    """Shut Ray down cleanly before interpreter exit.

    Tests that run workflows on the default executor implicitly initialize
    Ray (RayExecutor auto-inits on first ``fn.remote`` call). Leaving Ray
    initialized at interpreter shutdown segfaults CPython on Linux (exit
    code 139 in CI), even when every test passed. An explicit shutdown in
    session teardown avoids the atexit race.
    """
    yield
    try:
        import ray
    except ImportError:
        return
    if ray.is_initialized():
        ray.shutdown()


@pytest.fixture(scope="session", autouse=True)
def _tmux_server_keepalive(request):
    """Hold the tmux server alive for the whole test session.

    The tmux tests repeatedly ``kill-session`` + ``new-session``. When the
    killed session is the server's last one, the server process itself
    exits (default ``exit-empty on``); a ``new-session`` that connects
    during that teardown window fails with "server exited unexpectedly"
    (flaky in CI). Keeping one detached session of our own open means the
    server never dies between restarts. Only our session is created and
    removed — a developer's own tmux sessions are never touched.
    """
    session = "pytest-keepalive"
    active = shutil.which("tmux") and any(
        item.get_closest_marker("tmux") for item in request.session.items
    )
    if active:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session],
            capture_output=True,
            timeout=5,
        )
    yield
    if active:
        subprocess.run(
            ["tmux", "kill-session", "-t", session],
            capture_output=True,
            timeout=5,
        )
