"""Tests for executor.py - Execution engine abstraction."""

import sys
from types import SimpleNamespace

import pytest

from runtime.executor import LocalExecutor, RayExecutor, get_default_executor


class TestLocalExecutor:
    """Test LocalExecutor for sequential execution."""

    def test_local_executor_submit_executes_immediately(self):
        """Test that LocalExecutor executes functions immediately."""
        executor = LocalExecutor()

        def add(a, b):
            return a + b

        result = executor.submit(add, 2, 3)

        # LocalExecutor executes immediately, no future
        assert result == 5

    def test_local_executor_get_returns_results(self):
        """Test that get() returns results as-is."""
        executor = LocalExecutor()

        results = [1, 2, 3]
        fetched = executor.get(results)

        assert fetched == results

    def test_local_executor_shutdown(self):
        """Test that shutdown() doesn't raise errors."""
        executor = LocalExecutor()
        executor.shutdown()  # Should not raise

    def test_local_executor_num_returns_multiple(self):
        """Test LocalExecutor with num_returns > 1."""
        executor = LocalExecutor()

        def return_pair():
            return (1, 2)

        result = executor.submit(return_pair, num_returns=2)
        assert result == (1, 2)

    def test_local_executor_num_returns_mismatch_raises(self):
        """Test that num_returns mismatch raises ValueError."""
        executor = LocalExecutor()

        def return_single():
            return 42  # Returns single value, not tuple

        with pytest.raises(ValueError, match="expected to return 2 values"):
            executor.submit(return_single, num_returns=2)

    def test_local_executor_num_returns_wrong_length_raises(self):
        """Test that tuple of wrong length raises ValueError."""
        executor = LocalExecutor()

        def return_triple():
            return (1, 2, 3)  # Returns 3 values, not 2

        with pytest.raises(ValueError, match="expected to return 2 values"):
            executor.submit(return_triple, num_returns=2)


@pytest.mark.ray
class TestRayExecutor:
    """Test RayExecutor for distributed execution."""

    def test_ray_executor_requires_ray(self):
        """Test that RayExecutor can be created when Ray is available."""
        pytest.importorskip("ray")

        # Ray is available, RayExecutor should work without init
        # (assumes Ray might be initialized elsewhere or will be initialized later)
        executor = RayExecutor()
        assert executor is not None
        assert hasattr(executor, "ray")
        assert hasattr(executor, "submit")
        assert hasattr(executor, "get")

    def test_ray_executor_merges_job_runtime_env_into_init_kwargs(self, monkeypatch):
        calls = []
        fake_ray = SimpleNamespace(
            is_initialized=lambda: False,
            init=lambda **kwargs: calls.append(kwargs),
        )
        monkeypatch.setitem(sys.modules, "ray", fake_ray)

        RayExecutor(
            runtime_env={"working_dir": "/live/import-root"},
            ray_init_kwargs={
                "address": "auto",
                "runtime_env": {"env_vars": {"MODE": "test"}},
            },
        )

        assert calls == [
            {
                "address": "auto",
                "runtime_env": {
                    "env_vars": {"MODE": "test"},
                    "working_dir": "/live/import-root",
                },
            }
        ]

    def test_ray_executor_executes_task(self):
        """Test that RayExecutor actually executes tasks through Ray."""
        pytest.importorskip("ray")
        import ray

        # Initialize Ray without packaging working directory
        if ray.is_initialized():
            ray.shutdown()

        ray.init(
            num_cpus=2,
            ignore_reinit_error=True,
            include_dashboard=False,  # Don't package working directory
        )

        try:
            executor = RayExecutor()

            def add(a, b):
                return a + b

            # Submit task to Ray - actually executes!
            future = executor.submit(add, 2, 3)

            # Verify it's a Ray ObjectRef
            assert "ObjectRef" in str(type(future))

            # Fetch result - actually executes through Ray!
            results = executor.get([future])

            # Verify result came from actual Ray execution
            assert results == [5]
            print(f"✓ Ray executed: add(2, 3) = {results[0]}")

        finally:
            ray.shutdown()

    def test_ray_executor_parallel_execution(self):
        """Test that RayExecutor executes multiple tasks in parallel through Ray."""
        pytest.importorskip("ray")
        import ray

        # Initialize Ray without working directory packaging
        if ray.is_initialized():
            ray.shutdown()

        ray.init(
            num_cpus=2,
            ignore_reinit_error=True,
            include_dashboard=False,
        )

        try:
            executor = RayExecutor()

            def square(x):
                return x * x

            # Submit 5 tasks in parallel
            futures = [executor.submit(square, i) for i in range(5)]

            # All should be Ray ObjectRefs
            assert len(futures) == 5
            for future in futures:
                assert "ObjectRef" in str(type(future))

            # Fetch all results - actually executes in parallel!
            results = executor.get(futures)

            # Verify results from actual Ray execution
            assert results == [0, 1, 4, 9, 16]
            print(f"✓ Ray executed {len(futures)} tasks in parallel: {results}")

        finally:
            ray.shutdown()

    def test_ray_executor_with_init_kwargs(self):
        """Test RayExecutor with ray_init_kwargs."""
        pytest.importorskip("ray")
        import ray

        if ray.is_initialized():
            ray.shutdown()

        try:
            # Pass init kwargs - should initialize Ray with them
            executor = RayExecutor(
                ray_init_kwargs={
                    "num_cpus": 1,
                    "ignore_reinit_error": True,
                    "include_dashboard": False,
                }
            )

            assert ray.is_initialized()
            assert executor is not None

        finally:
            ray.shutdown()

    def test_ray_executor_shutdown(self):
        """Test that RayExecutor.shutdown() doesn't raise errors."""
        pytest.importorskip("ray")
        import ray

        if ray.is_initialized():
            ray.shutdown()

        ray.init(num_cpus=1, ignore_reinit_error=True, include_dashboard=False)

        try:
            executor = RayExecutor()
            executor.shutdown()  # Should not raise
        finally:
            ray.shutdown()


class TestGetDefaultExecutor:
    """Test get_default_executor() factory."""

    def test_get_default_executor_returns_executor(self):
        """Test that get_default_executor() returns some executor."""
        executor = get_default_executor()

        # Should return an executor
        assert hasattr(executor, "submit")
        assert hasattr(executor, "get")
        assert hasattr(executor, "shutdown")

    def test_get_default_executor_prefers_ray(self):
        """Test that get_default_executor() prefers Ray if available."""
        try:
            import ray  # noqa: F401

            executor = get_default_executor()
            # Ray is available, should return RayExecutor
            assert isinstance(executor, RayExecutor)
        except ImportError:
            executor = get_default_executor()
            # Ray not available, should return LocalExecutor
            assert isinstance(executor, LocalExecutor)
