"""Tests for dag.py - DAG construction and operators."""

import pytest

from avalanche.dag import (
    Node,
    NodeType,
    Pipeline,
    Workflow,
    dest,
    pipeline,
    source,
    step,
    transform,
    workflow,
)
from avalanche.executor import LocalExecutor


class TestNodeDecorators:
    """Test node decorators create proper Node objects."""

    def test_source_decorator_creates_node(self):
        @source
        def my_source():
            pass

        assert isinstance(my_source, Node)
        assert my_source.node_type == NodeType.SOURCE
        assert my_source.name == "my_source"

    def test_step_decorator_creates_node(self):
        @step
        def my_step():
            pass

        assert isinstance(my_step, Node)
        assert my_step.node_type == NodeType.STEP
        assert my_step.name == "my_step"

    def test_transform_is_step_synonym(self):
        @transform
        def my_transform():
            pass

        assert isinstance(my_transform, Node)
        assert my_transform.node_type == NodeType.STEP
        assert my_transform.name == "my_transform"

    def test_dest_decorator_creates_node(self):
        @dest
        def my_dest():
            pass

        assert isinstance(my_dest, Node)
        assert my_dest.node_type == NodeType.DEST
        assert my_dest.name == "my_dest"


class TestWorkflowSynonyms:
    def test_pipeline_class_and_decorator_are_workflow_synonyms(self):
        @source
        def load():
            return "data"

        @step
        def process(data):
            return f"processed-{data}"

        @pipeline
        def old_name_entrypoint():
            return load() >> process()

        built = old_name_entrypoint()
        assert isinstance(built, Workflow)
        assert isinstance(built, Pipeline)
        assert Pipeline is Workflow
        assert built.name == "old_name_entrypoint"


class TestNodeInvocationOutsideWorkflow:
    """Test that nodes cannot be invoked outside workflow context."""

    def test_node_call_outside_workflow_raises_error(self):
        @source
        def my_source():
            pass

        with pytest.raises(RuntimeError, match="outside of workflow context"):
            my_source()


class TestWorkflowDAGConstruction:
    """Test DAG construction via >> and & operators."""

    def test_simple_linear_workflow(self):
        @source
        def load():
            pass

        @step
        def transform_task():
            pass

        @dest
        def export():
            pass

        @workflow
        def my_workflow():
            load() >> transform_task() >> export()

        p = my_workflow()

        # Workflow should be created
        assert isinstance(p, Workflow)
        assert p.name == "my_workflow"

        # Should have 3 nodes
        assert len(p.nodes) == 3

        # Should have 2 edges: load->step, step->export
        # Graph format: {parent: [children]}
        assert len(p.graph) == 2
        assert "load_1" in p.graph
        assert "transform_task_1" in p.graph
        assert "transform_task_1" in p.graph["load_1"]
        assert "export_1" in p.graph["transform_task_1"]

    def test_parallel_branches(self):
        @source
        def load():
            pass

        @step
        def process_a():
            pass

        @step
        def process_b():
            pass

        @dest
        def export():
            pass

        @workflow
        def my_workflow():
            load() >> (process_a() & process_b()) >> export()

        p = my_workflow()

        # Graph format: {parent: [children]}
        # load's children are process_a and process_b
        assert "process_a_1" in p.graph["load_1"]
        assert "process_b_1" in p.graph["load_1"]

        # process_a and process_b's child is export
        assert "export_1" in p.graph["process_a_1"]
        assert "export_1" in p.graph["process_b_1"]

    def test_walrus_explicit_arg_and_chain_dedupes_graph_edge(self):
        @source
        def load():
            return "raw"

        @step
        def process(data):
            return f"processed-{data}"

        @workflow
        def my_workflow():
            (a := load()) >> (b := process(a))
            return b

        p = my_workflow()

        assert p.graph["load_1"].count("process_1") == 1
        assert p.run(executor=LocalExecutor()) == "processed-raw"

    def test_parallel_chain_dedupes_explicit_arg_edge(self):
        @source
        def x():
            return "x"

        @source
        def y():
            return "y"

        @step
        def z(value):
            return f"z-{value}"

        @workflow
        def my_workflow():
            x_future = x()
            return (x_future & y()) >> z(x_future)

        p = my_workflow()

        assert p.graph["x_1"].count("z_1") == 1
        assert p.graph["y_1"].count("z_1") == 1
        assert p.run(executor=LocalExecutor()) == "z-x"

    def test_variable_based_workflow(self):
        @source
        def load():
            pass

        @step
        def transform_task():
            pass

        @dest
        def export():
            pass

        @workflow
        def my_workflow():
            loaded = load()
            transformed = loaded >> transform_task()
            transformed >> export()

        p = my_workflow()

        # Should have same structure as linear
        # Graph format: {parent: [children]}
        assert "transform_task_1" in p.graph["load_1"]
        assert "export_1" in p.graph["transform_task_1"]

    def test_multiple_node_instances(self):
        """Test that calling the same node multiple times creates different instances."""

        @source
        def load():
            pass

        @step
        def process():
            pass

        @workflow
        def my_workflow():
            # Call load twice
            load1 = load()
            load2 = load()

            # Each should go to a different process instance
            load1 >> process()
            load2 >> process()

        p = my_workflow()

        # Should have load_1, load_2, process_1, process_2
        # Graph format: {parent: [children]}
        assert "load_1" in p.graph
        assert "load_2" in p.graph
        assert "process_1" in p.graph["load_1"]
        assert "process_2" in p.graph["load_2"]

    def test_complex_parallel_structure(self):
        """Test complex nested parallel structure from docs example."""

        @source
        def load():
            pass

        @step
        def transform_a():
            pass

        @step
        def transform_b():
            pass

        @dest
        def sink_a():
            pass

        @dest
        def sink_b1():
            pass

        @dest
        def sink_b2():
            pass

        @dest
        def cleanup():
            pass

        @workflow
        def my_workflow():
            (
                load()
                >> ((transform_a() >> sink_a()) & (transform_b() >> (sink_b1() & sink_b2())))
                >> cleanup()
            )

        p = my_workflow()

        # Verify structure (graph format: {parent: [children]})
        # load's children are transform_a and transform_b
        assert "transform_a_1" in p.graph["load_1"]
        assert "transform_b_1" in p.graph["load_1"]

        # transform_a's child is sink_a
        assert "sink_a_1" in p.graph["transform_a_1"]

        # transform_b's children are sink_b1 and sink_b2
        assert "sink_b1_1" in p.graph["transform_b_1"]
        assert "sink_b2_1" in p.graph["transform_b_1"]

        # all sinks' child is cleanup
        assert "cleanup_1" in p.graph["sink_a_1"]
        assert "cleanup_1" in p.graph["sink_b1_1"]
        assert "cleanup_1" in p.graph["sink_b2_1"]


class TestWorkflowIsolation:
    """Test that multiple workflow runs don't interfere with each other."""

    def test_multiple_workflow_calls_isolated(self):
        @source
        def load():
            pass

        @step
        def process():
            pass

        @workflow
        def my_workflow():
            load() >> process()

        p1 = my_workflow()
        p2 = my_workflow()

        # Both should have same structure but be different objects
        assert p1 is not p2
        assert p1.graph is not p2.graph
        # Graph format: {parent: [children]}
        assert "process_1" in p1.graph["load_1"]
        assert "process_1" in p2.graph["load_1"]

    def test_different_workflows_isolated(self):
        @source
        def load():
            pass

        @step
        def process():
            pass

        @workflow
        def workflow_a():
            load() >> process()

        @workflow
        def workflow_b():
            load() >> process() >> process()

        p_a = workflow_a()
        p_b = workflow_b()

        # Different structures
        assert len(p_a.graph) != len(p_b.graph)


class TestParallelTasksOperators:
    """Test ParallelTasks error handling and edge cases."""

    def test_parallel_rshift_with_invalid_type_raises_error(self):
        """Test that >> with invalid type raises TypeError."""

        @source
        def a():
            return "a"

        @source
        def b():
            return "b"

        @workflow
        def my_workflow():
            parallel = a() & b()
            parallel >> "not a node"

        with pytest.raises(TypeError, match="Cannot chain ParallelTasks"):
            my_workflow()

    def test_parallel_and_with_invalid_type_raises_error(self):
        """Test that & with invalid type raises TypeError."""

        @source
        def a():
            return "a"

        @source
        def b():
            return "b"

        @workflow
        def my_workflow():
            parallel = a() & b()
            parallel & "not a node"

        with pytest.raises(TypeError, match="Cannot combine ParallelTasks"):
            my_workflow()

    def test_parallel_rshift_with_parallel_tasks(self):
        """Test (a & b) >> (c & d) creates correct graph."""

        @source
        def a():
            return "a"

        @source
        def b():
            return "b"

        @step
        def c():
            pass

        @step
        def d():
            pass

        @dest
        def final(*args):
            return list(args)

        @workflow
        def my_workflow():
            return (a() & b()) >> (c() & d()) >> final()

        p = my_workflow()

        # a and b should connect to both c and d
        assert "c_1" in p.graph["a_1"]
        assert "d_1" in p.graph["a_1"]
        assert "c_1" in p.graph["b_1"]
        assert "d_1" in p.graph["b_1"]

        # c and d should both connect to final
        assert "final_1" in p.graph["c_1"]
        assert "final_1" in p.graph["d_1"]


class TestWorkflowRepr:
    """Test Workflow string representation."""

    def test_workflow_repr(self):
        """Test that Workflow has a useful repr."""

        @source
        def load():
            pass

        @step
        def process():
            pass

        @dest
        def export():
            pass

        @workflow
        def my_workflow():
            load() >> process() >> export()

        p = my_workflow()
        repr_str = repr(p)

        assert "Workflow" in repr_str
        assert "my_workflow" in repr_str
        assert "nodes=3" in repr_str
        assert "edges=2" in repr_str


class TestErrorHandling:
    """Test error cases."""

    def test_cannot_connect_nodes_from_different_workflows(self):
        @source
        def load():
            pass

        @step
        def process():
            pass

        # This should work - nodes in same workflow
        @workflow
        def valid_workflow():
            load() >> process()

        valid_workflow()  # Should not raise

        # This should fail - trying to connect across workflow contexts is prevented
        # by the fact that nodes can only be invoked within a workflow context
        with pytest.raises(RuntimeError, match="outside of workflow context"):
            load()

    def test_workflow_returning_parallel_chain(self):
        """Test that a workflow can return a chain ending in parallel tasks.

        This catches a bug where _fetch_node_result assumes chain_end is always
        a NodeFuture, but it can be ParallelTasks when using a >> (b & c).
        """

        @source
        def load():
            return "data"

        @step
        def process_a(data):
            return f"{data}_a"

        @step
        def process_b(data):
            return f"{data}_b"

        @workflow
        def my_workflow():
            return load() >> (process_a() & process_b())

        p = my_workflow()
        # Bug: AttributeError: 'ParallelTasks' object has no attribute 'future_id'
        result = p.run()
        assert result == ("data_a", "data_b")

    def test_parallel_tasks_reuse_not_affected_by_later_operations(self):
        """Test that reusing a ParallelTasks works correctly after extending it.

        Bug: If (a & b) mutates when combined with c, then using the original
        (a & b) in a different part of the workflow gives wrong results.
        """

        @source
        def a():
            return "a"

        @source
        def b():
            return "b"

        @source
        def c():
            return "c"

        @step
        def collect(*args):
            return list(args)

        @workflow
        def test_workflow():
            ab = a() & b()

            # Use ab in one place
            result1 = ab >> collect()

            # Extend ab with c for different use
            abc = ab & c()
            result2 = abc >> collect()

            return result1, result2

        p = test_workflow()
        r1, r2 = p.run()

        # Bug: r1 gets ["a", "b", "c"] instead of ["a", "b"] because ab was mutated
        assert r1 == ["a", "b"], f"Expected ['a', 'b'], got {r1}"
        assert r2 == ["a", "b", "c"], f"Expected ['a', 'b', 'c'], got {r2}"

    def test_indexed_nodefuture_chain_returns_correct_value(self):
        """Test that chaining from an indexed NodeFuture returns the correct final value.

        Bug: When you do `pair[0] >> process()`, the chain tracking breaks because
        the indexed NodeFuture doesn't inherit chain_start/chain_end. This causes
        p.run() to return the wrong value (the indexed value instead of the processed one).
        """

        @source(num_returns=2)
        def load_pair():
            return "a", "b"

        @step
        def process(data):
            return f"{data}_processed"

        @workflow
        def test_workflow():
            pair = load_pair()
            a = pair[0]
            chain = a >> process()
            return chain

        p = test_workflow()
        result = p.run()

        # Bug: Returns "a" instead of "a_processed" because chain tracking is broken
        assert result == "a_processed", f"Expected 'a_processed', got {result}"


class TestCycleDetection:
    """Test that cycles in the DAG are detected at construction time."""

    def test_cycle_detected_at_construction(self):
        """A manually constructed cyclic graph should raise ValueError."""
        from collections import defaultdict

        from avalanche.dag import Node, NodeFuture, NodeType

        # Create dummy nodes
        node_a = Node(lambda: None, NodeType.SOURCE)
        node_b = Node(lambda: None, NodeType.STEP)

        fa = NodeFuture(node_a, "a_1", defaultdict(list))
        fb = NodeFuture(node_b, "b_1", defaultdict(list))

        # Build a cycle: a -> b -> a
        graph = defaultdict(list)
        graph["a_1"] = ["b_1"]
        graph["b_1"] = ["a_1"]

        with pytest.raises(ValueError, match="cycle"):
            Workflow(graph=graph, nodes={"a_1": fa, "b_1": fb}, name="cyclic")

    def test_self_cycle_detected(self):
        """A node referencing itself should raise ValueError."""
        from collections import defaultdict

        from avalanche.dag import Node, NodeFuture, NodeType

        node_a = Node(lambda: None, NodeType.SOURCE)
        fa = NodeFuture(node_a, "a_1", defaultdict(list))

        graph = defaultdict(list)
        graph["a_1"] = ["a_1"]

        with pytest.raises(ValueError, match="cycle"):
            Workflow(graph=graph, nodes={"a_1": fa}, name="self_cycle")

    def test_valid_dag_no_error(self):
        """A valid DAG should construct without error."""
        @source
        def load():
            pass

        @step
        def process():
            pass

        @dest
        def save():
            pass

        @workflow
        def valid():
            load() >> process() >> save()

        p = valid()  # Should not raise
        assert len(p.nodes) == 3


class TestThreadSafety:
    """Test that workflow construction is thread-safe."""

    def test_concurrent_workflow_construction(self):
        """Test that building workflows concurrently doesn't corrupt state.

        This catches a bug where class-level mutable state on Node is shared
        across threads, causing concurrent workflow builds to corrupt each other.

        The test uses time.sleep() to widen the race window and make the bug
        more likely to manifest with Python's GIL.
        """
        import concurrent.futures
        import time

        @source
        def load():
            return "data"

        @step
        def process():
            pass

        @workflow
        def workflow_a():
            # Sleep to widen race window between setting context and using it
            time.sleep(0.001)
            load() >> process()

        @workflow
        def workflow_b():
            time.sleep(0.001)
            load() >> process() >> process()

        results = []
        errors = []

        def build_workflows(n):
            try:
                for _ in range(n):
                    p1 = workflow_a()
                    p2 = workflow_b()
                    # Verify structure is correct
                    assert (
                        len(p1.nodes) == 2
                    ), f"workflow_a should have 2 nodes, got {len(p1.nodes)}"
                    assert (
                        len(p2.nodes) == 3
                    ), f"workflow_b should have 3 nodes, got {len(p2.nodes)}"
                    results.append((p1, p2))
            except Exception as e:
                errors.append(e)

        # Run in parallel threads - use more workers and iterations
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(build_workflows, 20) for _ in range(8)]
            concurrent.futures.wait(futures)

        # Bug: With class-level state, concurrent builds corrupt each other
        assert not errors, f"Errors during concurrent build: {errors}"
        assert len(results) == 160  # 8 threads * 20 iterations
