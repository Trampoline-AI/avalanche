"""Tests for runtime.py - Stream, Logger primitives."""

import pytest

from avalanche.runtime import Logger, Stream


class MockTransaction:
    """Mock transaction for testing."""

    def __init__(self, table: "MockTable"):
        self.table = table

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return None

    def set_properties(self, **kwargs):
        """Update table properties."""
        self.table.properties.update(kwargs)


class MockTable:
    """Mock table for testing."""

    def __init__(self, name: str):
        self.name = name
        self.properties = {}

    def transaction(self):
        """Create a mock transaction."""
        return MockTransaction(self)


class TestStream:
    """Test Stream as provider marker."""

    def test_stream_initialization(self):
        """Test that Stream can be initialized with a table and key."""
        table = MockTable("docs")
        stream = Stream(table, key="test_stream", mode="append_scan")

        assert stream.table is table
        assert stream.key == "test_stream"
        assert stream.mode == "append_scan"

    def test_stream_defaults_to_run_scoped_without_key(self):
        """Stream defaults to run_scoped without a key."""
        table = MockTable("docs")
        stream = Stream(table)

        assert stream.mode == "run_scoped"
        assert stream.key is None

    def test_stream_is_provider(self):
        """Test that Stream can be identified as a provider."""
        table = MockTable("docs")
        stream = Stream(table)

        # Should be identifiable via isinstance (used by can_resolve)
        assert isinstance(stream, Stream)
        assert Stream.can_resolve(stream) is True

    def test_append_scan_mode_requires_key(self):
        """append_scan mode requires an explicit key."""
        table = MockTable("docs")

        with pytest.raises(ValueError, match="append_scan streams require key"):
            Stream(table, mode="append_scan")

    def test_run_scoped_stream_rejects_key(self):
        """run_scoped (default) rejects key= to avoid silent append-scan drift."""
        table = MockTable("docs")

        with pytest.raises(ValueError, match="run_scoped streams do not use key"):
            Stream(table, key="test_key")

    def test_stream_rejects_invalid_mode(self):
        """Unknown modes are rejected at construction."""
        table = MockTable("docs")

        with pytest.raises(ValueError, match="run_scoped.*append_scan"):
            Stream(table, mode="bogus")  # type: ignore[arg-type]

    def test_stream_repr(self):
        """Test Stream string representation."""
        table = MockTable("docs")
        stream = Stream(table, key="my_stream", mode="append_scan")

        repr_str = repr(stream)
        assert "Stream" in repr_str
        assert "my_stream" in repr_str
        assert "append_scan" in repr_str


class TestLogger:
    """Test Logger interface."""

    def test_logger_initialization(self):
        """Test that Logger can be initialized as a provider marker."""
        logger = Logger()
        assert isinstance(logger, Logger)
        assert logger.name is None
        assert logger.level is None

    def test_logger_is_provider(self):
        """Test that Logger implements ParameterProvider protocol."""
        logger = Logger()
        # Logger should be detectable by provider
        assert Logger.can_resolve(logger) is True
        assert Logger.can_resolve("not a logger") is False

    def test_logger_as_default_parameter_in_workflow(self):
        """Test that Logger() works as a default parameter in workflow execution."""
        import avalanche as ava

        @ava.step
        def process_with_logger(data, *, logger=Logger()):
            # Verify logger is a LoggerInstance, not Logger marker
            # This assertion runs in Ray worker - if it fails, workflow raises
            from avalanche.runtime.providers.logger import LoggerInstance

            assert isinstance(logger, LoggerInstance), (
                f"Expected LoggerInstance, got {type(logger)}"
            )
            # Return type info to verify in main process
            return {"data": data, "logger_type": type(logger).__name__}

        @ava.workflow
        def test_workflow():
            return process_with_logger([1, 2, 3])

        # Run workflow - should inject Logger automatically
        p = test_workflow()
        result = p.run().result()
        # If logger wasn't injected correctly, the assertion above would have failed
        assert result["data"] == [1, 2, 3]
        assert result["logger_type"] == "LoggerInstance"

    def test_logger_context_with_local_executor(self):
        """Test Logger context IDs with LocalExecutor."""
        import avalanche as ava
        from avalanche.executor import LocalExecutor

        @ava.step
        def capture_context(data, *, logger=Logger()):
            from avalanche.runtime.providers.logger import LoggerInstance

            assert isinstance(logger, LoggerInstance)
            # Return the context for verification
            return logger._context

        @ava.workflow
        def test_workflow():
            return capture_context([1, 2, 3])

        p = test_workflow()
        context = p.run(executor=LocalExecutor()).result()

        # Verify run_id is a valid ULID (26 chars, uppercase alphanumeric)
        assert context["run_id"] is not None
        assert len(context["run_id"]) == 26
        # ULID characters are 0-9 and A-Z (Crockford Base32)
        assert all(
            c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
            for c in context["run_id"]
        )

        # Verify worker_id is "local" for LocalExecutor
        assert context["worker_id"] == "local"

        # Verify node name
        assert context["node"] == "capture_context"

    @pytest.mark.ray
    def test_logger_context_with_ray_executor(self):
        """Test that Logger context has run_id and Ray worker_id with RayExecutor."""
        import avalanche as ava

        @ava.step
        def capture_context(data, *, logger=Logger()):
            from avalanche.runtime.providers.logger import LoggerInstance

            assert isinstance(logger, LoggerInstance)
            return logger._context

        @ava.workflow
        def test_workflow():
            return capture_context([1, 2, 3])

        p = test_workflow()
        # Default executor is RayExecutor
        context = p.run().result()

        # Verify run_id is a valid ULID
        assert context["run_id"] is not None
        assert len(context["run_id"]) == 26

        # Verify worker_id is from Ray (not "local")
        assert context["worker_id"] is not None
        assert context["worker_id"] != "local"
        # Ray worker_id is a hex string
        assert len(context["worker_id"]) > 0


class TestIntegrationPatterns:
    """Test how these primitives are used in task signatures."""

    def test_stream_as_default_argument_pattern(self):
        """Test the pattern of using Stream as default argument."""
        table = MockTable("docs")

        # This is the pattern used in task signatures
        def my_task(*, docs=Stream(table, key="test_key", mode="append_scan")):
            return docs

        # When called without args, gets the default
        result = my_task()
        assert isinstance(result, Stream)
        assert result.table is table
        assert result.key == "test_key"
