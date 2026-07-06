"""Shared pytest configuration for the avalanche test suite."""

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
