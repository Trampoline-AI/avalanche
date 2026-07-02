"""Integration tests for Workflow execution with Ray."""

import pytest

from avalanche.dag import dest, source, step, workflow
from avalanche.executor import LocalExecutor, RayExecutor

# Shared state for tracking task execution order
execution_log = []


class TestWorkflowExecution:
    """Test Workflow.run() with different executors."""

    def test_workflow_execution_with_local_executor(self):
        """Test workflow execution with LocalExecutor."""

        @source
        def load_data():
            return {"data": [1, 2, 3]}

        @step
        def double_values(data):
            # Actually use the input data
            return {"data": [x * 2 for x in data["data"]]}

        @dest
        def save_results(data):
            # Use the input data
            return f"saved_{len(data['data'])}_items"

        @workflow
        def my_workflow():
            result = load_data() >> double_values() >> save_results()
            return result  # Return final result

        # Build workflow
        p = my_workflow()

        # Verify workflow structure
        assert len(p.nodes) == 3
        assert len(p.graph) == 2  # 2 edges

        # Execute with LocalExecutor
        executor = LocalExecutor()
        result = p.run(executor=executor)

        # Verify result
        assert result == "saved_3_items"

        print("✓ Workflow executed locally!")
        print(f"  Result: {result}")

    def test_workflow_execution_with_ray_executor(self):
        """Test workflow execution with RayExecutor - actual distributed execution!"""
        pytest.importorskip("ray")
        import ray

        if ray.is_initialized():
            ray.shutdown()

        ray.init(
            num_cpus=4,
            ignore_reinit_error=True,
            include_dashboard=False,
            runtime_env={"working_dir": None},
        )

        try:

            @source
            def extract():
                """Extract data from source."""
                return [1, 2, 3, 4, 5]

            @step
            def double(data):
                """Double the values."""
                return [x * 2 for x in data]

            @dest
            def load(data):
                """Load results."""
                return f"completed_{len(data)}_items"

            @workflow
            def data_workflow():
                a = extract()
                b = double()
                c = load()
                a >> b >> c
                return c  # Return final result

            # Build workflow
            p = data_workflow()

            # Verify workflow structure
            assert len(p.nodes) == 3
            assert len(p.graph) == 2  # 2 edges: extract->double, double->load

            # Execute with RayExecutor - ACTUALLY RUNS THROUGH RAY!
            executor = RayExecutor()
            result = p.run(executor=executor)

            # Verify result
            assert result == "completed_5_items"

            print("✓ Workflow executed through Ray successfully!")
            print(f"  Result: {result}")

        finally:
            ray.shutdown()

    def test_workflow_parallel_execution_with_ray(self):
        """Test parallel execution through Ray."""
        pytest.importorskip("ray")
        import ray

        if ray.is_initialized():
            ray.shutdown()

        ray.init(
            num_cpus=4,
            ignore_reinit_error=True,
            include_dashboard=False,
            runtime_env={"working_dir": None},
        )

        try:

            @source
            def source_task():
                return "source_data"

            @step
            def branch_a(data):
                return f"{data}_processed_a"

            @step
            def branch_b(data):
                return f"{data}_processed_b"

            @dest
            def sink(data_a, data_b):
                # Note: With parallel branches, both pass data but sink needs to handle both
                # For now, just accept one (first one to arrive)
                return f"sink_done_{data_a}"

            @workflow
            def parallel_workflow():
                src = source_task()
                a = branch_a()
                b = branch_b()
                dest_task = sink()

                # Fan-out and fan-in
                result = src >> (a & b) >> dest_task
                return result  # Return final sink result

            # Build workflow
            p = parallel_workflow()

            # Verify DAG structure
            assert len(p.nodes) == 4

            # Execute through Ray - branches run in parallel!
            executor = RayExecutor()
            result = p.run(executor=executor)

            # Verify final result contains expected data
            assert "sink_done" in result
            assert "source_data_processed" in result

            print("✓ Parallel workflow executed through Ray!")
            print("  DAG: source -> (branch_a & branch_b) -> sink")
            print(f"  Result: {result}")

        finally:
            ray.shutdown()


class TestFireAndForgetWorkflows:
    """Test workflows that don't return values."""

    def test_fire_and_forget_local(self):
        """Test fire-and-forget workflow with LocalExecutor."""
        executed = []

        @source
        def load():
            executed.append("load")
            return [1, 2, 3]

        @dest
        def save(data):
            executed.append("save")
            return "done"

        @workflow
        def background_job():
            data = load()
            save(data)
            # No return - fire and forget!

        p = background_job()
        result = p.run(LocalExecutor())

        # Should return None but execute all nodes
        assert result is None
        assert executed == ["load", "save"]

    def test_fire_and_forget_with_ray(self):
        """Test fire-and-forget workflow with Ray."""
        pytest.importorskip("ray")
        import ray

        if ray.is_initialized():
            ray.shutdown()

        ray.init(
            num_cpus=2,
            ignore_reinit_error=True,
            include_dashboard=False,
            runtime_env={"working_dir": None},
        )

        try:

            @source
            def extract():
                return [1, 2, 3, 4, 5]

            @step
            def process(data):
                return [x * 2 for x in data]

            @dest
            def sink(data):
                return f"processed_{len(data)}_items"

            @workflow
            def fire_and_forget():
                data = extract()
                processed = process(data)
                sink(processed)
                # No return!

            p = fire_and_forget()
            result = p.run()

            # Should return None (fire-and-forget)
            assert result is None
            # Workflow executed successfully (didn't raise errors)

        finally:
            ray.shutdown()
