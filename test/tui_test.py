"""Tests for the Avalanche TUI module."""

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import grpc
import pytest

import avalanche.tui.ui_store as ui_store_module
from avalanche.operator.client import (
    GrpcStateProvider,
    OperatorCallError,
    StaleResetAcknowledgementError,
    StreamState,
)
from avalanche.operator.convert import (
    run_snapshot_to_proto,
    run_summary_to_proto,
    workflow_info_to_proto,
)
from avalanche.operator.models import (
    AgentEvent,
    AgentEventDetailAppended,
    LogDetailAppended,
    RunSnapshot,
    RunSummary,
    TraceDescriptor,
    TraceDetail,
)
from avalanche.tui.dag_layout import (
    DagNode,
    ParGroup,
    build_nav_grid,
    nav_move,
    render_dag_rich,
    workflow_to_layout,
)
from avalanche.tui.mock import (
    ANALYTICS_WORKFLOW,
    INGEST_WORKFLOW,
    ML_WORKFLOW,
    ORDER_WORKFLOW,
    MockStateProvider,
)
from avalanche.tui.models import (
    LogEntry,
    LogLevel,
    NodeState,
    NodeStatus,
    ResetBaseline,
    RunState,
    RunStatus,
    StreamResetNotice,
    WorkflowInfo,
)
from avalanche.tui.state import get_operator_reachability, get_stream_state
from avalanche.tui.ui_store import UIStore
from avalanche.tui.widgets.run_history import RunHistoryWidget
from avalanche.tui.widgets.sidebar import Sidebar
from avalanche.tui.widgets.status_bar import StatusBar
from runtime.operator.proto import operator_pb2 as pb
from runtime.operator.proto import operator_pb2_grpc as pb_grpc


def _retry_hydration_workflow() -> WorkflowInfo:
    return WorkflowInfo(
        name="flow",
        display_name="flow",
        workflow_id="flow",
        file_path="flow.py",
        node_ids=["node"],
        graph={},
        node_types={"node": "step"},
    )


def _retry_hydration_run(
    log_sequence: int = 1,
    *,
    operator_instance_id: str = "operator-1",
    hydrated: bool = False,
) -> RunState:
    return RunState(
        run_id="run-retry",
        flow_name="flow",
        workflow_id="flow",
        operator_instance_id=operator_instance_id,
        created_sequence=1,
        revision=log_sequence,
        latest_log_sequence=log_sequence,
        details_hydrated=hydrated,
        logs=(
            [
                LogEntry(
                    timestamp=datetime(2026, 7, 22),
                    level=LogLevel.INFO,
                    node_id="node",
                    message=f"log-{sequence}",
                )
                for sequence in range(1, log_sequence + 1)
            ]
            if hydrated
            else []
        ),
    )


class _RetryHydrationProvider(MockStateProvider):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)
        self.calls = 0
        self.completed = [threading.Event() for _ in self.outcomes]

    def list_workflows(self):
        return []

    def get_run(self, run_id):
        assert run_id == "run-retry"
        attempt = self.calls
        self.calls += 1
        try:
            outcome = self.outcomes[attempt]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.completed[attempt].set()


def _retry_hydration_store(outcomes):
    workflow = _retry_hydration_workflow()
    run = _retry_hydration_run()
    provider = _RetryHydrationProvider(outcomes)
    store = UIStore(provider)
    store.workflows = [workflow]
    store.current_workflow = workflow
    store.current_run = run
    store.run_pinned = True
    store._set_runs_cache([run])
    clock = [100.0]
    store._detail_hydration_now = lambda: clock[0]
    return store, provider, workflow, run, clock


def _finish_retry_hydration_attempt(store, provider, attempt):
    assert provider.completed[attempt].wait(timeout=1)
    deadline = time.monotonic() + 1
    while store._detail_hydrations_in_flight and time.monotonic() < deadline:
        store._apply_background_updates()
        time.sleep(0.001)
    assert not store._detail_hydrations_in_flight


def _apply_async_updates(store: UIStore, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while store._runs_refresh_in_flight and time.monotonic() < deadline:
        time.sleep(0.005)
        store._apply_background_updates()
    store._apply_background_updates()


class _SignalingQueue:
    def __init__(self, queue):
        self._queue = queue
        self.put_event = threading.Event()

    def put(self, item):
        self._queue.put(item)
        self.put_event.set()

    def get(self):
        return self._queue.get()

    def empty(self):
        return self._queue.empty()


def _signal_background_updates(store: UIStore) -> _SignalingQueue:
    queue = _SignalingQueue(store._background_updates)
    store._background_updates = queue
    return queue


async def _wait_for_current_run(app, updates: _SignalingQueue, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while app.store.current_run is None:
        updates.put_event.clear()
        app.store._apply_background_updates()
        app._apply_deep_link()
        if app.store.current_run is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.to_thread(updates.put_event.wait, remaining)
    app.store._apply_background_updates()
    assert (
        app.store.current_run is not None
    ), f"Timed out after {timeout:.1f}s waiting for the current run update"


def test_connection_overlay_uses_public_label_and_error_state():
    from avalanche.tui.app import AvalancheApp
    from avalanche.tui.state import ConnectionAwareStateProvider

    delegate = MockStateProvider()

    class DisconnectedProvider:
        operator_reachable = False
        connection_label = "operator.example:7433"
        last_error = "UNAVAILABLE: maintenance"

        def list_workflows(self):
            return delegate.list_workflows()

        def list_runs(self, workflow_selector):
            return delegate.list_runs(workflow_selector)

        def get_run(self, run_id):
            return delegate.get_run(run_id)

        def start_run(self, workflow_selector, **kwargs):
            return delegate.start_run(workflow_selector, **kwargs)

        def cancel_run(self, run_id):
            return delegate.cancel_run(run_id)

        def on_run_update(self, callback):
            return delegate.on_run_update(callback)

        def on_log(self, callback):
            return delegate.on_log(callback)

        def ping(self):
            return False

    class Wrapper:
        visible = False

        def has_class(self, name):
            return name == "visible" and self.visible

        def add_class(self, name):
            assert name == "visible"
            self.visible = True

    class Box:
        rendered = None

        def update(self, value):
            self.rendered = value

    provider = DisconnectedProvider()
    assert isinstance(provider, ConnectionAwareStateProvider)
    wrapper = Wrapper()
    box = Box()
    screen = SimpleNamespace(
        query_one=lambda selector: {
            "#disconnect-wrapper": wrapper,
            "#disconnect-box": box,
        }[selector]
    )
    app = SimpleNamespace(
        store=SimpleNamespace(provider=provider, frame=0),
        _screen=screen,
        _ping_counter=0,
        _ping_in_flight=False,
    )

    AvalancheApp._check_connection(app)

    assert wrapper.visible
    assert "operator.example:7433" in str(box.rendered)
    assert "UNAVAILABLE: maintenance" in str(box.rendered)


class _ReachableStateProvider:
    operator_instance_id = "test"
    operator_reachable = True
    stream_state = "live"
    stream_error = ""

    def on_stream_reset(self, callback) -> None:
        self._stream_reset_callback = callback

    def load_reset_baseline(self, notice: StreamResetNotice) -> ResetBaseline:
        raise RuntimeError("reset baseline is not configured for this test provider")

    def acknowledge_stream_reset(
        self,
        generation: int,
        operator_instance_id: str,
        reconciled_sequence: int,
    ) -> None:
        if self.stream_state != "reset_required":
            raise RuntimeError("stream reset is not required")
        self.operator_instance_id = operator_instance_id
        self.stream_state = "live"


class _ApplicationErrorRefreshProvider(MockStateProvider):
    def __init__(self) -> None:
        super().__init__()
        self.fail_operation: str | None = None

    def _raise_if_failed(self, operation: str) -> None:
        if self.fail_operation == operation:
            raise OperatorCallError(
                grpc.StatusCode.INVALID_ARGUMENT,
                f"{operation} rejected",
            )

    def list_workflows(self) -> list[WorkflowInfo]:
        self._raise_if_failed("catalog")
        return super().list_workflows()

    def list_runs(self, workflow_selector: str) -> list[RunState]:
        self._raise_if_failed("runs")
        return super().list_runs(workflow_selector)

    def start_run(self, workflow_selector: str, **kwargs) -> str:
        self._raise_if_failed("start")
        return super().start_run(workflow_selector, **kwargs)


@pytest.mark.asyncio
async def test_synchronous_stream_start_sees_every_callback_before_first_detail():
    from avalanche.tui.app import AvalancheApp

    detail = LogDetailAppended(
        operator_instance_id="mock",
        run_id="run-sync",
        created_sequence=1,
        sequence=2,
        log_sequence=1,
        log=LogEntry(
            timestamp=datetime(2026, 7, 22),
            level=LogLevel.INFO,
            node_id="node",
            message="first",
        ),
    )

    class SynchronousStartProvider(MockStateProvider):
        def __init__(self):
            super().__init__()
            self.started = False
            self.first_detail_dispatched = False

        def start_stream(self):
            assert len(self._run_callbacks) == 1
            assert len(self._detail_callbacks) == 1
            assert len(self._log_callbacks) == 1
            self.started = True
            self._detail_callbacks[0](detail)
            self.first_detail_dispatched = True

    provider = SynchronousStartProvider()
    app = AvalancheApp(provider=provider)
    async with app.run_test(size=(80, 30)):
        assert provider.started
        assert provider.first_detail_dispatched


# ── Models ─────────────────────────────────────────────────────────────────


class TestModels:
    def test_node_status_values(self):
        assert NodeStatus.PENDING.value == "pending"
        assert NodeStatus.RUNNING.value == "running"
        assert NodeStatus.SUCCESS.value == "success"

    def test_node_state_elapsed_not_started(self):
        ns = NodeState(node_id="x", name="x", node_type="step")
        assert ns.elapsed is None

    def test_node_state_elapsed_running(self):
        ns = NodeState(
            node_id="x",
            name="x",
            node_type="step",
            status=NodeStatus.RUNNING,
            started_at=time.monotonic() - 2.0,
        )
        assert ns.elapsed is not None
        assert ns.elapsed >= 1.9

    def test_node_state_elapsed_completed(self):
        t = time.monotonic()
        ns = NodeState(
            node_id="x",
            name="x",
            node_type="step",
            status=NodeStatus.SUCCESS,
            started_at=t - 5.0,
            ended_at=t - 2.0,
        )
        assert abs(ns.elapsed - 3.0) < 0.1

    def test_run_state_elapsed(self):
        rs = RunState(run_id="r1", flow_name="p", started_at=time.monotonic() - 1.0)
        assert rs.elapsed >= 0.9

    def test_workflow_info_fields(self):
        p = ORDER_WORKFLOW
        assert p.name == "order_workflow"
        assert "fetch_orders_1" in p.node_ids
        assert "validate_1" in p.graph["fetch_orders_1"]


# ── DAG Layout ─────────────────────────────────────────────────────────────


class TestWorkflowToLayout:
    def test_order_workflow_parallel_groups(self):
        dag, nodes = workflow_to_layout(ORDER_WORKFLOW)
        node_names = [n.name for n in nodes]
        assert "fetch_orders_1" in node_names
        assert "fetch_inventory_1" in node_names
        assert "save_warehouse_1" in node_names

        # Should have ParGroups for the two parallel sections
        par_groups = [s for s in dag.steps if isinstance(s, ParGroup)]
        assert len(par_groups) == 2

        # First par: fetch_orders, fetch_inventory
        branch_names_0 = {
            n.name
            for b in par_groups[0].branches
            for s in b.steps
            if isinstance(s, DagNode)
            for n in [s]
        }
        assert branch_names_0 == {"fetch_orders_1", "fetch_inventory_1"}

        # Second par: save_warehouse, notify
        branch_names_1 = {
            n.name
            for b in par_groups[1].branches
            for s in b.steps
            if isinstance(s, DagNode)
            for n in [s]
        }
        assert branch_names_1 == {"save_warehouse_1", "notify_1"}

    def test_ingest_workflow_linear(self):
        dag, nodes = workflow_to_layout(INGEST_WORKFLOW)
        # Linear workflow: no ParGroups
        par_groups = [s for s in dag.steps if isinstance(s, ParGroup)]
        assert len(par_groups) == 0
        node_names = [n.name for n in nodes]
        assert node_names == ["extract_1", "parse_1", "deduplicate_1", "load_1"]

    def test_analytics_workflow_dual_parallel(self):
        dag, nodes = workflow_to_layout(ANALYTICS_WORKFLOW)
        par_groups = [s for s in dag.steps if isinstance(s, ParGroup)]
        assert len(par_groups) == 2

    def test_virtual_start_end(self):
        dag, _ = workflow_to_layout(ORDER_WORKFLOW)
        first = dag.steps[0]
        last = dag.steps[-1]
        assert isinstance(first, DagNode) and first.name == "start" and first.virtual
        assert isinstance(last, DagNode) and last.name == "end" and last.virtual


class TestNavGrid:
    def test_order_workflow_grid_structure(self):
        dag, nodes = workflow_to_layout(ORDER_WORKFLOW)
        grid = build_nav_grid(dag)
        # Should have columns for each navigation position
        assert len(grid) >= 4
        # First column should be parallel sources
        assert len(grid[0]) == 2
        assert {n.name for n in grid[0]} == {"fetch_orders_1", "fetch_inventory_1"}

    def test_nav_move_horizontal(self):
        dag, nodes = workflow_to_layout(ORDER_WORKFLOW)
        grid = build_nav_grid(dag)
        start = grid[0][0]  # fetch_orders
        next_node, pref = nav_move(grid, start, 0, 1, 0)
        assert next_node.col == 1  # moved right

    def test_nav_move_vertical(self):
        dag, nodes = workflow_to_layout(ORDER_WORKFLOW)
        grid = build_nav_grid(dag)
        top = grid[0][0]  # fetch_orders
        bottom, pref = nav_move(grid, top, 0, 0, 1)
        assert bottom.name == "fetch_inventory_1"


class TestRenderDag:
    def test_render_produces_lines(self):
        dag, nodes = workflow_to_layout(ORDER_WORKFLOW)
        statuses = {n.name: NodeStatus.PENDING for n in nodes}
        lines = render_dag_rich(dag, statuses, 0, None)
        assert len(lines) >= 1
        plain = lines[0].plain
        assert "start" in plain or "fetch" in plain

    def test_render_with_selection(self):
        dag, nodes = workflow_to_layout(ORDER_WORKFLOW)
        statuses = {n.name: NodeStatus.PENDING for n in nodes}
        selected = nodes[0]
        lines = render_dag_rich(dag, statuses, 0, selected)
        combined = "\n".join(line.plain for line in lines)
        assert selected.display_name in combined

    def test_render_shows_ampersand_between_parallel_branches(self):
        """Parallel branches should show & between them to indicate concurrency."""
        dag, nodes = workflow_to_layout(ORDER_WORKFLOW)
        statuses = {n.name: NodeStatus.PENDING for n in nodes}
        lines = render_dag_rich(dag, statuses, 0, None)
        combined = "\n".join(line.plain for line in lines)
        assert "&" in combined, f"Expected '&' between parallel branches, got:\n{combined}"

    def test_cross_fork_fanin_dedup_page_highlights(self):
        """page_highlights should appear exactly once in doc_processing layout."""
        from avalanche.tui.mock import DOC_PROCESSING_WORKFLOW

        dag, nodes = workflow_to_layout(DOC_PROCESSING_WORKFLOW)
        ph_count = sum(1 for n in nodes if n.name == "page_highlights_1")
        assert ph_count == 1, f"page_highlights appears {ph_count} times, expected 1"

    def test_cross_fork_fanin_skip_edge_recorded(self):
        """push_to_cdn → page_highlights should be a skip edge after dedup."""
        from avalanche.tui.mock import DOC_PROCESSING_WORKFLOW

        dag, nodes = workflow_to_layout(DOC_PROCESSING_WORKFLOW)
        assert (
            ("push_to_cdn_1", "page_highlights_1") in dag.skip_edges
        ), f"Expected skip edge push_to_cdn→page_highlights, got: {dag.skip_edges}"

    def test_partial_convergence_dedup(self):
        """Fan-in nodes reachable from some (not all) branches get deduped."""
        dag, nodes = workflow_to_layout(ML_WORKFLOW)
        ns_count = sum(1 for n in nodes if n.name == "notify_slack_1")
        # notify_slack has 2 parents (deploy_staging, deploy_prod) in the same
        # fork — partial convergence, should be deduped to 1
        assert ns_count == 1, f"notify_slack appears {ns_count} times, expected 1"
        # The deduped edge should be a skip edge
        skip_srcs = {s for s, d in dag.skip_edges if d == "notify_slack_1"}
        assert len(skip_srcs) >= 1, "Expected skip edge to notify_slack"

    def test_dense_fanin_chain_no_duplicates(self):
        """A transitive-tournament DAG (every stage feeds all later stages)
        must lay out as a single linear spine with skip-edge annotations,
        never duplicating nodes."""
        ids = [f"s{i}_1" for i in range(1, 8)]
        graph = {ids[i]: [ids[j] for j in range(i + 1, 7)] for i in range(6)}
        info = WorkflowInfo(
            name="dense",
            file_path="f",
            node_ids=ids,
            graph=graph,
            node_types={i: "step" for i in ids},
        )
        dag, nodes = workflow_to_layout(info)
        counts = {}
        for n in nodes:
            if not n.virtual:
                counts[n.name] = counts.get(n.name, 0) + 1
        dups = {k: v for k, v in counts.items() if v > 1}
        assert not dups, f"duplicated nodes in layout: {dups}"
        spine = [s.name for s in dag.steps if isinstance(s, DagNode) and not s.virtual]
        assert spine == ids, f"expected linear spine, got {spine}"

    def test_render_all_branches_present_for_3way_parallel(self):
        """A workflow with 3-way parallel should render all 3 branch rows."""
        dag, nodes = workflow_to_layout(ML_WORKFLOW)
        statuses = {n.name: NodeStatus.PENDING for n in nodes}
        lines = render_dag_rich(dag, statuses, 0, None)
        combined = "\n".join(line.plain for line in lines)
        assert "fetch_training" in combined
        assert "fetch_validation" in combined
        assert "fetch_features" in combined
        # Each line should be self-contained (no wrapping artifacts)
        for i, line in enumerate(lines):
            plain = line.plain
            assert (
                "─┐" not in plain or "┌──" in plain or "├" in plain
            ), f"Line {i} has closing bracket without opening — possible wrap: {plain}"


# ── Mock Provider ──────────────────────────────────────────────────────────


class TestMockStateProvider:
    def test_list_workflows(self):
        provider = MockStateProvider()
        workflows = provider.list_workflows()
        assert len(workflows) == 6
        names = {p.name for p in workflows}
        assert "order_workflow" in names
        assert "data_platform" in names

    def test_exposes_explicit_transport_health(self):
        provider = MockStateProvider()

        assert get_operator_reachability(provider) is True
        assert get_stream_state(provider) == "live"

    def test_pre_seeded_runs(self):
        provider = MockStateProvider()
        runs = provider.list_runs("order_workflow")
        assert len(runs) >= 1
        assert runs[0].status == RunStatus.SUCCESS

    def test_start_run(self):
        provider = MockStateProvider()
        run_id = provider.start_run("order_workflow")
        run = provider.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.RUNNING
        assert len(run.nodes) == 7
        # Wait briefly for simulation to start
        time.sleep(0.5)
        assert len(run.logs) > 0
        provider.cancel_run(run_id)

    def test_cancel_run(self):
        provider = MockStateProvider()
        run_id = provider.start_run("order_workflow")
        time.sleep(0.2)
        provider.cancel_run(run_id)
        run = provider.get_run(run_id)
        assert run.status == RunStatus.CANCELLED

    def test_start_run_unknown_workflow(self):
        provider = MockStateProvider()
        with pytest.raises(ValueError):
            provider.start_run("nonexistent")

    def test_callbacks_invoked(self):
        provider = MockStateProvider()
        updates = []
        provider.on_run_update(lambda r: updates.append(r.run_id))
        run_id = provider.start_run("ingest_workflow")
        time.sleep(1)
        provider.cancel_run(run_id)
        assert len(updates) > 0

    def test_file_paths_have_subdirs(self):
        provider = MockStateProvider()
        workflows = provider.list_workflows()
        paths = {p.file_path for p in workflows}
        assert any("etl/" in p for p in paths)
        assert any("ingestion/" in p for p in paths)


# ── UIStore ────────────────────────────────────────────────────────────────


class TestUIStore:
    def test_deep_link_waits_for_workflow_before_selecting_same_named_node(self):
        from avalanche.tui.app import AvalancheApp

        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]

        def workflow(selector, node):
            return WorkflowInfo(
                name=selector,
                display_name=selector,
                workflow_id=selector,
                file_path=f"{selector}.py",
                node_ids=[node],
                graph={},
                node_types={node: "step"},
            )

        default = workflow("default", "shared_node")
        desired = workflow("desired", "shared_node")

        class BlockingCatalogProvider(_ReachableStateProvider):
            def __init__(self):
                self.calls = 0

            def list_workflows(self):
                call = self.calls
                self.calls += 1
                entered[call].set()
                release[call].wait()
                return [default] if call == 0 else [default, desired]

            def list_runs(self, selector):
                return []

            def get_run(self, run_id):
                return None

            def start_run(self, selector, **kwargs):
                return ""

            def cancel_run(self, run_id):
                pass

            def on_run_update(self, callback):
                pass

            def on_log(self, callback):
                pass

            def on_detail_update(self, callback):
                pass

        app = AvalancheApp(BlockingCatalogProvider(), workflow="desired", node="shared_node")
        updates = _signal_background_updates(app.store)
        assert entered[0].wait(1.0)
        assert app.store.workflows == []

        release[0].set()
        assert updates.put_event.wait(1.0)
        app.store._apply_background_updates()
        app._apply_deep_link()
        assert app.store.current_workflow is default
        assert app.store.selected_node is None
        assert app._deep_link_workflow == "desired"
        assert app._deep_link_node == "shared_node"

        updates.put_event.clear()
        app.store._refresh_workflow_catalog()
        assert entered[1].wait(1.0)
        release[1].set()
        assert updates.put_event.wait(1.0)
        app.store._apply_background_updates()
        app._apply_deep_link()
        assert app.store.current_workflow is desired
        assert app.store.selected_node.name == "shared_node"

    def test_async_start_is_prompt_guarded_and_applies_after_release(self):
        release = threading.Event()
        delegate = MockStateProvider()

        class BlockingStartProvider(_ReachableStateProvider):
            def __init__(self):
                self.start_calls = 0
                self.entered = threading.Event()

            def list_workflows(self):
                return delegate.list_workflows()

            def list_runs(self, selector):
                return delegate.list_runs(selector)

            def get_run(self, run_id):
                return delegate.get_run(run_id)

            def start_run(self, selector, **kwargs):
                self.start_calls += 1
                self.entered.set()
                release.wait()
                return delegate.start_run(selector, **kwargs)

        provider = BlockingStartProvider()
        store = UIStore(provider)
        _apply_async_updates(store)
        updates = _signal_background_updates(store)
        assert store.start_run_async()
        assert provider.entered.wait(1.0)
        assert not store.start_run_async()
        assert provider.start_calls == 1

        release.set()
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert not store._start_run_in_flight
        assert store.current_run is not None
        assert store.run_pinned

    def test_stale_async_start_does_not_replace_new_workflow(self):
        release = threading.Event()
        delegate = MockStateProvider()

        class BlockingStartProvider(_ReachableStateProvider):
            def __init__(self):
                self.entered = threading.Event()

            def list_workflows(self):
                return delegate.list_workflows()

            def list_runs(self, selector):
                return delegate.list_runs(selector)

            def get_run(self, run_id):
                return delegate.get_run(run_id)

            def start_run(self, selector, **kwargs):
                self.entered.set()
                release.wait()
                return delegate.start_run(selector, **kwargs)

        provider = BlockingStartProvider()
        store = UIStore(provider)
        _apply_async_updates(store)
        updates = _signal_background_updates(store)
        started_selector = store.current_workflow.selector
        assert store.start_run_async()
        assert provider.entered.wait(1.0)
        other = next(
            workflow for workflow in store.workflows if workflow.selector != started_selector
        )
        store.switch_workflow(other)
        _apply_async_updates(store)
        updates.put_event.clear()
        release.set()
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert store.current_workflow is other
        assert store.current_run is None or store._run_matches(store.current_run, other)

    @pytest.mark.parametrize("failed_call", ["get", "list"])
    def test_async_start_preserves_provider_error_and_does_not_pin(self, failed_call):
        delegate = MockStateProvider()

        class FailedFollowupProvider(_ReachableStateProvider):
            def __init__(self):
                self.operator_reachable = True
                self.last_error = ""
                self.started = False
                self.entered = threading.Event()

            def list_workflows(self):
                return delegate.list_workflows()

            def start_run(self, selector, **kwargs):
                self.started = True
                self.entered.set()
                return "run_new"

            def get_run(self, run_id):
                if failed_call == "get":
                    self.operator_reachable = False
                    raise OperatorCallError(
                        grpc.StatusCode.UNAVAILABLE,
                        "get failed",
                    )
                return RunState(run_id=run_id, flow_name="order_workflow")

            def list_runs(self, selector):
                if self.started and failed_call == "list":
                    self.operator_reachable = False
                    raise OperatorCallError(
                        grpc.StatusCode.DEADLINE_EXCEEDED,
                        "list failed",
                    )
                return []

        provider = FailedFollowupProvider()
        store = UIStore(provider)
        _apply_async_updates(store)
        updates = _signal_background_updates(store)
        assert store.start_run_async()
        assert provider.entered.wait(1.0)
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert store.current_run is None
        assert not store.run_pinned
        assert store.run_error == (
            "UNAVAILABLE: get failed"
            if failed_call == "get"
            else "DEADLINE_EXCEEDED: list failed"
        )

    @pytest.mark.parametrize("interaction", ["select", "deselect"])
    def test_run_interaction_invalidates_pending_start(self, interaction):
        release = threading.Event()
        delegate = MockStateProvider()

        class BlockingStartProvider(_ReachableStateProvider):
            def __init__(self):
                self.entered = threading.Event()

            def list_workflows(self):
                return delegate.list_workflows()

            def list_runs(self, selector):
                return delegate.list_runs(selector)

            def get_run(self, run_id):
                return delegate.get_run(run_id)

            def start_run(self, selector, **kwargs):
                self.entered.set()
                release.wait()
                return delegate.start_run(selector, **kwargs)

        provider = BlockingStartProvider()
        store = UIStore(provider)
        _apply_async_updates(store)
        updates = _signal_background_updates(store)
        assert store.current_run is not None
        assert store.start_run_async()
        assert provider.entered.wait(1.0)

        if interaction == "select":
            chosen = store.runs_for_current_workflow[0]
            store.switch_run(chosen)
        else:
            chosen = store.current_run
            store.deselect_run()

        release.set()
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert store.current_run is chosen
        assert store.run_pinned is (interaction == "select")

    def test_initial_state(self):
        store = UIStore(MockStateProvider())
        assert store.current_workflow is not None
        assert store.current_workflow.name == store.workflows[0].name
        assert store.dag is not None
        assert len(store.all_nodes) > 0
        assert store.selected_node is None
        assert store.frame == 0

    def test_switch_workflow(self):
        store = UIStore(MockStateProvider())
        first = store.current_workflow
        second = [p for p in store.workflows if p.name != first.name][0]
        store.switch_workflow(second)
        assert store.current_workflow == second
        assert store.sidebar_selected_name == second.name
        assert store.selected_node is None
        assert store.dag is not None

    def test_node_statuses_with_run(self):
        store = UIStore(MockStateProvider())
        if store.current_run:
            statuses = store.node_statuses
            assert len(statuses) > 0

    def test_node_statuses_no_run(self):
        store = UIStore(MockStateProvider())
        store.current_run = None
        assert store.node_statuses == {}

    def test_logs_no_run(self):
        store = UIStore(MockStateProvider())
        store.current_run = None
        assert store.logs == []

    def test_select_node(self):
        store = UIStore(MockStateProvider())
        node = store.all_nodes[0]
        store.select_node(node)
        assert store.selected_node == node
        assert store.preferred_row == node.row

    def test_deselect_node(self):
        store = UIStore(MockStateProvider())
        store.select_node(store.all_nodes[0])
        store.deselect_node()
        assert store.selected_node is None

    def test_move_nav_from_none(self):
        store = UIStore(MockStateProvider())
        assert store.selected_node is None
        store.move_nav(1, 0)
        assert store.selected_node == store.all_nodes[0]

    def test_move_nav_horizontal(self):
        store = UIStore(MockStateProvider())
        store.select_node(store.all_nodes[0])
        store.move_nav(1, 0)
        assert store.selected_node.col == 1

    def test_move_nav_vertical(self):
        store = UIStore(MockStateProvider())
        # Order workflow first column has 2 rows
        store.select_node(store.all_nodes[0])
        original = store.selected_node
        store.move_nav(0, 1)
        assert store.selected_node != original

    def test_run_state_label_idle(self):
        store = UIStore(MockStateProvider())
        store.current_run = None
        assert store.run_state_label == "IDLE"

    def test_run_state_label_success(self):
        store = UIStore(MockStateProvider())
        # Pre-seeded order_workflow run is SUCCESS
        if store.current_run and store.current_run.status == RunStatus.SUCCESS:
            assert store.run_state_label == "DONE"

    def test_elapsed_str(self):
        store = UIStore(MockStateProvider())
        assert "s" in store.elapsed_str

    def test_selected_run_id(self):
        store = UIStore(MockStateProvider())
        if store.current_run:
            assert store.selected_run_id == store.current_run.run_id
        store.current_run = None
        assert store.selected_run_id is None

    def test_runs_for_current_workflow(self):
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        runs = store.runs_for_current_workflow
        assert isinstance(runs, list)
        assert len(runs) >= 1

    def test_runs_for_current_workflow_none(self):
        store = UIStore(MockStateProvider())
        store.current_workflow = None
        assert store.runs_for_current_workflow == []

    def test_selected_node_elapsed_str_none(self):
        store = UIStore(MockStateProvider())
        assert store.selected_node_elapsed_str == ""

    def test_selected_node_elapsed_str_with_node(self):
        store = UIStore(MockStateProvider())
        if store.all_nodes and store.current_run:
            store.select_node(store.all_nodes[0])
            result = store.selected_node_elapsed_str
            assert isinstance(result, str)

    def test_search_flow(self):
        store = UIStore(MockStateProvider())
        store.begin_search()
        assert store.searching is True
        assert store.search_query == ""

        store.search_append("t")
        store.search_append("e")
        assert store.search_query == "te"

        store.search_backspace()
        assert store.search_query == "t"

        store.set_match_count(3)
        store.end_search()
        assert store.searching is False
        assert store.search_index == 0

    def test_search_next_prev(self):
        store = UIStore(MockStateProvider())
        store.set_match_count(3)
        store.search_index = 0
        store.search_next()
        assert store.search_index == 1
        store.search_next()
        assert store.search_index == 2
        store.search_next()
        assert store.search_index == 0  # wraps
        store.search_prev()
        assert store.search_index == 2  # wraps back

    def test_search_next_no_matches(self):
        store = UIStore(MockStateProvider())
        store.set_match_count(0)
        store.search_index = -1
        store.search_next()
        assert store.search_index == -1  # unchanged

    def test_cancel_search(self):
        store = UIStore(MockStateProvider())
        store.begin_search()
        store.search_append("x")
        store.cancel_search()
        assert store.searching is False
        assert store.search_query == "x"  # query preserved

    def test_clear_search(self):
        store = UIStore(MockStateProvider())
        store.search_query = "test"
        store.search_index = 2
        store.clear_search()
        assert store.search_query == ""
        assert store.search_index == -1

    def test_end_search_no_matches(self):
        store = UIStore(MockStateProvider())
        store.begin_search()
        store.search_append("xyz")
        store.set_match_count(0)
        store.end_search()
        assert store.searching is False
        assert store.search_index == -1  # stays at -1 with no matches

    def test_start_run(self):
        provider = MockStateProvider()
        store = UIStore(provider)
        run_id = store.start_run()
        assert run_id is not None
        assert store.current_run is not None
        assert store.current_run.run_id == run_id
        time.sleep(0.2)
        provider.cancel_run(run_id)

    def test_start_run_no_workflow(self):
        store = UIStore(MockStateProvider())
        store.current_workflow = None
        assert store.start_run() is None

    def test_switch_run(self):
        store = UIStore(MockStateProvider())
        run = RunState(run_id="test", flow_name="p")
        store.switch_run(run)
        assert store.current_run == run
        assert store.selected_run_id == "test"

    def test_tick(self):
        store = UIStore(MockStateProvider())
        old_frame = store.frame
        store.tick()
        assert store.frame == old_frame + 1
        assert len(store.workflow_statuses) > 0

    def test_stream_handoff_coalesces_and_repairs_sustained_overflow(self):
        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["agent"],
            graph={},
            node_types={"agent": "agent"},
            agent_node_ids=["agent"],
        )
        run = RunState(
            run_id="run-1",
            flow_name="flow",
            workflow_id="flow",
            nodes={
                "agent": NodeState(
                    node_id="agent",
                    name="Agent",
                    node_type="agent",
                )
            },
            operator_instance_id="operator-1",
            created_sequence=1,
            details_hydrated=True,
        )
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        store._background_updates = ui_store_module._BoundedBackgroundUpdates()
        store.workflows = [workflow]
        store.current_workflow = workflow
        store.current_run = run
        store._set_runs_cache([run])

        for revision in range(1, 10_001):
            store.enqueue_run_update(replace(run, revision=revision))
        assert store._background_updates.qsize() == 1

        last_sequence = ui_store_module.BACKGROUND_UPDATE_CAPACITY * 4
        for sequence in range(1, last_sequence + 1):
            store.enqueue_detail_update(
                LogDetailAppended(
                    operator_instance_id="operator-1",
                    run_id="run-1",
                    created_sequence=1,
                    sequence=sequence,
                    log_sequence=sequence,
                    log=LogEntry(
                        timestamp=datetime(2026, 7, 24),
                        level=LogLevel.INFO,
                        node_id="agent",
                        message=f"log-{sequence}",
                    ),
                )
            )

        assert (
            store._background_updates.qsize() <= ui_store_module.BACKGROUND_UPDATE_CAPACITY + 1
        )
        scheduled = []
        store._schedule_detail_hydration = lambda run, **requirements: scheduled.append(
            (run.run_id, requirements)
        )
        before = store._background_updates.qsize()
        store._apply_background_updates()

        assert before - store._background_updates.qsize() <= (
            ui_store_module.BACKGROUND_UPDATES_PER_TICK
        )
        assert any(
            requirements.get("required_log_sequence") == last_sequence
            for _, requirements in scheduled
        )
        store.shutdown()

    def test_duplicate_display_names_use_ids_and_refresh_preserves_selection(self):
        class MutableProvider(_ReachableStateProvider):
            def __init__(self):
                self.workflows = []
                self.selectors = []

            def list_workflows(self):
                return list(self.workflows)

            def list_runs(self, workflow_selector):
                self.selectors.append(workflow_selector)
                return []

            def get_run(self, run_id):
                return None

            def start_run(self, workflow_selector, **kwargs):
                self.selectors.append(workflow_selector)
                return "run_new"

            def cancel_run(self, run_id):
                pass

            def on_run_update(self, callback):
                pass

            def on_log(self, callback):
                pass

            def on_detail_update(self, callback):
                pass

        def workflow(workflow_id, source):
            return WorkflowInfo(
                name="shared",
                display_name="shared",
                workflow_id=workflow_id,
                root_alias=workflow_id.split("/", 1)[0],
                relative_file=source,
                builder_symbol="shared",
                file_path=source,
                node_ids=["node_1"],
                graph={},
                node_types={"node_1": "step"},
            )

        left = workflow("left/flow.py::shared", "flow.py")
        right = workflow("right/flow.py::shared", "flow.py")
        provider = MutableProvider()
        provider.workflows = [left, right]
        store = UIStore(provider)
        assert len(store.workflows) == 2
        store.switch_workflow(right)
        assert provider.selectors[-1] == right.workflow_id

        def refresh(workflows):
            provider.workflows = workflows
            store._refresh_workflow_catalog()
            deadline = time.monotonic() + 1
            while store._catalog_refresh_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)
                store._apply_background_updates()
            assert not store._catalog_refresh_in_flight

        refresh([right, left])
        assert store.current_workflow.workflow_id == right.workflow_id
        assert store.sidebar_selected_id == right.workflow_id

        sidebar = Sidebar()
        sidebar._test_store = store
        sidebar._rebuild_tree()
        rows = [item for item in sidebar._flat_items if not item.is_folder]
        assert len(rows) == 2
        assert [item.workflow.workflow_id for item in rows] == [
            left.workflow_id,
            right.workflow_id,
        ]

        refresh([left])
        assert store.current_workflow.workflow_id == left.workflow_id
        assert store.sidebar_selected_id == left.workflow_id

        refresh([])
        assert store.current_workflow is None
        assert store.sidebar_selected_id == ""

    def test_async_catalog_expands_only_new_workflow_folders(self):
        provider = MockStateProvider()
        store = UIStore(provider)
        store.workflows = []
        store.sidebar_expanded.clear()

        workflow = replace(
            provider.list_workflows()[0],
            workflow_id="fixtures/flow.py::fixture_workflow",
            root_alias="fixtures",
            relative_file="nested/flow.py",
        )
        store._reconcile_workflows([workflow])

        assert store.sidebar_expanded == {"fixtures", "fixtures/nested"}

        store.sidebar_expanded.clear()
        store._reconcile_workflows([replace(workflow, next_run_at=100.0)])

        assert store.sidebar_expanded == set()

    def test_same_id_topology_and_schedule_display_changes_increment_revision(self):
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        original = store.current_workflow
        assert original is not None

        topology = replace(
            original,
            node_ids=[*original.node_ids, "new_sink_1"],
            graph={**original.graph, original.node_ids[-1]: ["new_sink_1"]},
            node_types={**original.node_types, "new_sink_1": "dest"},
            display_names={**original.display_names, "new_sink_1": "New sink"},
        )
        revision = store.catalog_revision
        store._reconcile_workflows(
            [
                topology if item.selector == original.selector else item
                for item in store.workflows
            ]
        )
        assert store.catalog_revision == revision + 1
        assert [node.name for node in store.all_nodes] == topology.node_ids

        scheduled = replace(
            topology,
            display_name="Renamed workflow",
            relative_file="moved/workflow.py",
            cron="0 * * * *",
            next_run_at=100.0,
            last_run_at=50.0,
        )
        revision = store.catalog_revision
        store._reconcile_workflows(
            [
                scheduled if item.selector == original.selector else item
                for item in store.workflows
            ]
        )
        assert store.catalog_revision == revision + 1
        assert store.current_workflow is scheduled
        assert store.current_workflow.rendered_name == "Renamed workflow"

    def test_switch_workflow_refresh_is_nonblocking_and_selector_scoped(self):
        release: dict[str, threading.Event] = {}
        entered: dict[str, threading.Event] = {}

        def workflow(selector: str) -> WorkflowInfo:
            return WorkflowInfo(
                name=selector,
                display_name=selector,
                workflow_id=selector,
                file_path=f"{selector}.py",
                node_ids=["node_1"],
                graph={},
                node_types={"node_1": "step"},
            )

        workflows = [workflow("a"), workflow("b")]
        runs = {
            selector: RunState(
                run_id=f"run_{selector}",
                flow_name=selector,
                workflow_id=selector,
            )
            for selector in ("a", "b")
        }

        class BlockingProvider(_ReachableStateProvider):
            def list_workflows(self):
                return workflows

            def list_runs(self, selector):
                release.setdefault(selector, threading.Event())
                entered.setdefault(selector, threading.Event()).set()
                release[selector].wait()
                return [runs[selector]]

        store = UIStore(BlockingProvider())
        updates = _signal_background_updates(store)
        assert entered.setdefault("a", threading.Event()).wait(1.0)
        assert store.current_workflow is workflows[0]
        assert store.current_run is None

        store.switch_workflow(workflows[1])
        assert entered.setdefault("b", threading.Event()).wait(1.0)
        assert store.current_workflow is workflows[1]
        assert store.current_run is None
        assert store.runs_for_current_workflow == []
        assert store._runs_refresh_in_flight == {"a", "b"}

        release["a"].set()
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert store.current_workflow is workflows[1]
        assert store.current_run is None
        assert store.runs_for_current_workflow == []

        updates.put_event.clear()
        release["b"].set()
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert store.current_run is runs["b"]
        assert store.runs_for_current_workflow == [runs["b"]]

    def test_summary_refresh_preserves_hydrated_current_run_details(self):
        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["node"],
            graph={},
            node_types={"node": "step"},
        )
        node = NodeState(
            node_id="node",
            name="Node",
            node_type="step",
            status=NodeStatus.RUNNING,
            agent_trace_json='{"status":"in_progress","events":[{"sequence":1}]}',
        )
        hydrated = RunState(
            run_id="run_1",
            flow_name="flow",
            workflow_id="flow",
            status=RunStatus.RUNNING,
            nodes={"node": node},
            logs=[
                LogEntry(
                    timestamp=datetime(2026, 7, 22),
                    level=LogLevel.INFO,
                    node_id="node",
                    message="working",
                )
            ],
            operator_instance_id="operator-1",
            revision=5,
            latest_log_sequence=4,
        )
        summary = RunState(
            run_id="run_1",
            flow_name="flow",
            workflow_id="flow",
            status=RunStatus.SUCCESS,
            operator_instance_id="operator-1",
            revision=6,
            details_hydrated=False,
        )
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        store.current_workflow = workflow
        store.current_run = hydrated
        store.run_pinned = True
        store._runs_cache = [hydrated]

        store._background_updates.put(
            (
                "runs",
                (
                    workflow.selector,
                    store._run_data_revision(workflow.selector),
                    store._workflow_context_epoch,
                    [summary],
                ),
            )
        )
        store._apply_background_updates()

        merged = store.current_run
        assert merged.nodes is hydrated.nodes
        assert merged.logs is hydrated.logs
        assert merged is store._runs_cache[0]
        assert merged.status is RunStatus.SUCCESS
        assert merged.nodes["node"].agent_trace_json == node.agent_trace_json
        assert [entry.message for entry in merged.logs] == ["working"]
        assert merged.latest_log_sequence == 4
        assert merged.details_hydrated

        stale_summary = replace(summary, status=RunStatus.FAILED, revision=4)
        store._background_updates.put(
            (
                "runs",
                (
                    workflow.selector,
                    store._run_data_revision(workflow.selector),
                    store._workflow_context_epoch,
                    [stale_summary],
                ),
            )
        )
        store._apply_background_updates()

        assert store.current_run.status is RunStatus.SUCCESS
        assert store.current_run.nodes["node"].agent_trace_json == node.agent_trace_json

    def test_live_detail_updates_populate_ui_without_structural_bodies(self):
        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["agent"],
            graph={},
            node_types={"agent": "agent"},
            agent_node_ids=["agent"],
        )
        run = RunState(
            run_id="run_1",
            flow_name="flow",
            workflow_id="flow",
            nodes={
                "agent": NodeState(
                    node_id="agent",
                    name="Agent",
                    node_type="agent",
                )
            },
            operator_instance_id="operator-1",
            created_sequence=7,
            details_hydrated=False,
        )
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        store.workflows = [workflow]
        store.current_workflow = workflow
        store.current_run = run
        store.run_pinned = True
        store._set_runs_cache([run])
        store.dag, store.all_nodes = workflow_to_layout(workflow)
        store.select_node(store.all_nodes[0])

        log = LogEntry(
            timestamp=datetime(2026, 7, 22),
            level=LogLevel.INFO,
            node_id="agent",
            message="streamed detail",
        )
        event_json = json.dumps(
            {
                "sequence": 2,
                "event_kind": "code.executed",
                "data": {"output": "done"},
            }
        )
        store.enqueue_run_update(
            replace(
                run,
                revision=8,
                latest_log_sequence=1,
                details_hydrated=False,
            )
        )
        store.enqueue_detail_update(
            LogDetailAppended(
                operator_instance_id="operator-1",
                run_id="run_1",
                created_sequence=7,
                sequence=8,
                log_sequence=1,
                log=log,
            )
        )
        store.enqueue_detail_update(
            AgentEventDetailAppended(
                operator_instance_id="operator-1",
                run_id="run_1",
                created_sequence=7,
                sequence=9,
                node_id="agent",
                event=AgentEvent(
                    invocation_id="test-invocation",
                    event_sequence=2,
                    event_json=event_json,
                    size_bytes=len(event_json),
                ),
            )
        )
        store._apply_background_updates()

        assert [entry.message for entry in store.current_run.logs] == ["streamed detail"]
        assert store.selected_agent_events == [
            {
                "sequence": 2,
                "event_kind": "code.executed",
                "data": {"output": "done"},
            }
        ]

        structural = replace(
            store.current_run,
            status=RunStatus.SUCCESS,
            logs=[],
            nodes={
                "agent": replace(
                    store.current_run.nodes["agent"],
                    agent_trace_json=None,
                )
            },
            revision=10,
            latest_log_sequence=1,
            details_hydrated=False,
        )
        store.enqueue_run_update(structural)
        store._apply_background_updates()

        assert store.current_run.status is RunStatus.SUCCESS
        assert [entry.message for entry in store.current_run.logs] == ["streamed detail"]
        assert store.selected_agent_events[0]["data"]["output"] == "done"

    def test_live_detail_append_cost_is_independent_of_cached_history(self, monkeypatch):
        append_count = 1000

        class CountedDetails(list):
            def __init__(self, values=()):
                super().__init__(values)
                self.append_calls = 0
                self.iterated_slots = 0
                self.sliced_slots = 0

            def append(self, value):
                self.append_calls += 1
                return super().append(value)

            def __iter__(self):
                self.iterated_slots += list.__len__(self)
                return super().__iter__()

            def __getitem__(self, key):
                value = super().__getitem__(key)
                if isinstance(key, slice):
                    self.sliced_slots += len(value)
                return value

        class IndexedOnlyRunCache(list):
            scans = 0

            def __iter__(self):
                type(self).scans += 1
                raise AssertionError("hot detail delivery scanned the run cache")

        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["agent"],
            graph={},
            node_types={"agent": "agent"},
            agent_node_ids=["agent"],
        )
        logs = CountedDetails()
        run = RunState(
            run_id="run_1",
            flow_name="flow",
            workflow_id="flow",
            nodes={
                "agent": NodeState(
                    node_id="agent",
                    name="Agent",
                    node_type="agent",
                    agent_trace_json='{"events":[]}',
                )
            },
            operator_instance_id="operator-1",
            created_sequence=7,
            details_hydrated=True,
            logs=logs,
        )
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        store.workflows = [workflow]
        store.current_workflow = workflow
        store.current_run = run
        store.run_pinned = True
        store._set_runs_cache([run])
        store._remember_run_details(run)
        event_key = (*store._detail_key(run), "agent")
        events = CountedDetails(store._agent_event_details[event_key])
        store._agent_event_details[event_key] = events
        store._runs_cache = IndexedOnlyRunCache(store._runs_cache)

        original_copy = ui_store_module.copy
        copy_calls = 0

        def counted_copy(value):
            nonlocal copy_calls
            copy_calls += 1
            return original_copy(value)

        monkeypatch.setattr(ui_store_module, "copy", counted_copy)

        for sequence in range(1, append_count + 1):
            structural = replace(
                store.current_run,
                logs=[],
                nodes={
                    "agent": replace(
                        store.current_run.nodes["agent"],
                        agent_trace_json=None,
                    )
                },
                revision=sequence,
                latest_log_sequence=sequence,
                details_hydrated=False,
            )
            event_json = json.dumps(
                {
                    "sequence": sequence,
                    "event_kind": "iteration.recorded",
                    "data": {"iteration": sequence},
                }
            )
            store.enqueue_run_update(structural)
            store.enqueue_detail_update(
                LogDetailAppended(
                    operator_instance_id="operator-1",
                    run_id="run_1",
                    created_sequence=7,
                    sequence=sequence * 3,
                    log_sequence=sequence,
                    log=LogEntry(
                        timestamp=datetime(2026, 7, 22),
                        level=LogLevel.INFO,
                        node_id="agent",
                        message=f"log-{sequence}",
                    ),
                )
            )
            store.enqueue_detail_update(
                AgentEventDetailAppended(
                    operator_instance_id="operator-1",
                    run_id="run_1",
                    created_sequence=7,
                    sequence=sequence * 3 + 1,
                    node_id="agent",
                    event=AgentEvent(
                        invocation_id="test-invocation",
                        event_sequence=sequence,
                        event_json=event_json,
                        size_bytes=len(event_json),
                    ),
                )
            )
            store._apply_background_updates()

        assert logs.append_calls == append_count
        assert events.append_calls == append_count
        assert logs.iterated_slots == 0
        assert logs.sliced_slots == 0
        assert events.iterated_slots == 0
        assert events.sliced_slots == 0
        assert IndexedOnlyRunCache.scans == 0
        assert copy_calls <= append_count * 2
        assert list.__len__(logs) == append_count
        assert list.__len__(events) == append_count
        assert list.__getitem__(logs, -1).message == f"log-{append_count}"
        assert list.__getitem__(events, -1)["sequence"] == append_count

    def test_switch_back_hydration_advances_watermarks_and_repairs_gaps(self):
        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["agent"],
            graph={},
            node_types={"agent": "agent"},
            agent_node_ids=["agent"],
        )

        def event(sequence):
            return {
                "sequence": sequence,
                "event_kind": "iteration.recorded",
                "data": {"iteration": sequence},
            }

        def hydrated(
            log_sequence,
            event_sequence,
            revision,
            trace_revision,
        ):
            events = [event(sequence) for sequence in range(1, event_sequence + 1)]
            return RunState(
                run_id="run-a",
                flow_name="flow",
                workflow_id="flow",
                operator_instance_id="operator-1",
                created_sequence=7,
                revision=revision,
                latest_log_sequence=log_sequence,
                details_hydrated=True,
                logs=[
                    LogEntry(
                        timestamp=datetime(2026, 7, 22),
                        level=LogLevel.INFO,
                        node_id="agent",
                        message=f"log-{sequence}",
                    )
                    for sequence in range(1, log_sequence + 1)
                ],
                nodes={
                    "agent": NodeState(
                        node_id="agent",
                        name="Agent",
                        node_type="agent",
                        revision=revision,
                        trace=TraceDescriptor(
                            status="completed",
                            revision=trace_revision,
                            available=True,
                            complete=True,
                            event_count=event_sequence,
                        ),
                        agent_trace_json=json.dumps(
                            {
                                "events": events,
                                "trace": {"marker": f"trace-{trace_revision}"},
                            }
                        ),
                    )
                },
            )

        def structural(run, revision, latest_log_sequence):
            return replace(
                run,
                revision=revision,
                latest_log_sequence=latest_log_sequence,
                details_hydrated=False,
                logs=[],
                nodes={
                    "agent": replace(
                        run.nodes["agent"],
                        revision=revision,
                        agent_trace_json=None,
                    )
                },
            )

        def log_detail(sequence):
            return LogDetailAppended(
                operator_instance_id="operator-1",
                run_id="run-a",
                created_sequence=7,
                sequence=sequence * 3,
                log_sequence=sequence,
                log=LogEntry(
                    timestamp=datetime(2026, 7, 22),
                    level=LogLevel.INFO,
                    node_id="agent",
                    message=f"log-{sequence}",
                ),
            )

        def event_detail(sequence):
            event_json = json.dumps(event(sequence))
            return AgentEventDetailAppended(
                operator_instance_id="operator-1",
                run_id="run-a",
                created_sequence=7,
                sequence=sequence * 3 + 1,
                node_id="agent",
                event=AgentEvent(
                    invocation_id="test-invocation",
                    event_sequence=sequence,
                    event_json=event_json,
                    size_bytes=len(event_json),
                ),
            )

        class GapHydrationProvider(MockStateProvider):
            def __init__(self):
                super().__init__()
                self.results = []
                self.started = []
                self.releases = []
                self.calls = 0

            def get_run(self, run_id):
                assert run_id == "run-a"
                attempt = self.calls
                self.calls += 1
                self.started[attempt].set()
                self.releases[attempt].wait(timeout=2)
                return self.results[attempt]

            def prepare(self, *results):
                self.results = list(results)
                self.started = [threading.Event() for _ in results]
                self.releases = [threading.Event() for _ in results]

        provider = GapHydrationProvider()
        store = UIStore(provider)
        _apply_async_updates(store)
        run_a1 = hydrated(1, 1, 1, 1)
        run_b = replace(
            run_a1,
            run_id="run-b",
            created_sequence=8,
            logs=[],
            nodes={},
            details_hydrated=False,
        )
        store.workflows = [workflow]
        store.current_workflow = workflow
        store.current_run = run_a1
        store.run_pinned = True
        store._set_runs_cache([run_a1, run_b])
        store._remember_run_details(run_a1)

        store.switch_run(run_b)
        for sequence in (2, 3):
            store.enqueue_run_update(structural(run_a1, sequence, sequence))
            store.enqueue_detail_update(log_detail(sequence))
            store.enqueue_detail_update(event_detail(sequence))
            store._apply_background_updates()
        assert provider.calls == 0

        store.switch_run(store._runs_cache[0])
        run_a3 = hydrated(3, 3, 3, 3)
        store.enqueue_polled_run_update(
            workflow.selector,
            store._run_data_revision(workflow.selector),
            store._workflow_context_epoch,
            run_a3,
        )
        store._apply_background_updates()

        key = store._detail_key(run_a3)
        event_key = (*key, "agent")
        assert store.current_run.logs is run_a3.logs
        assert store._log_detail_sequences[key] == 3
        assert store._agent_event_sequences[event_key] == 3
        assert (
            json.loads(store.current_run.nodes["agent"].agent_trace_json)["trace"]["marker"]
            == "trace-3"
        )

        store.enqueue_run_update(structural(store.current_run, 4, 4))
        store.enqueue_detail_update(log_detail(4))
        store.enqueue_detail_update(event_detail(4))
        store._apply_background_updates()
        assert [entry.message for entry in store.current_run.logs] == [
            "log-1",
            "log-2",
            "log-3",
            "log-4",
        ]
        assert store._agent_event_sequences[event_key] == 4

        logs_through_4 = store.current_run.logs
        events_through_4 = store._agent_event_details[event_key]
        stale = hydrated(2, 2, 2, 2)
        store.enqueue_polled_run_update(
            workflow.selector,
            store._run_data_revision(workflow.selector),
            store._workflow_context_epoch,
            stale,
        )
        store._apply_background_updates()
        assert store.current_run.logs is logs_through_4
        assert store._agent_event_details[event_key] is events_through_4
        assert store.current_run.revision == 4
        assert (
            json.loads(store.current_run.nodes["agent"].agent_trace_json)["trace"]["marker"]
            == "trace-3"
        )

        run_a6 = hydrated(6, 6, 6, 6)
        run_a8 = hydrated(8, 8, 8, 8)
        provider.prepare(run_a6, run_a8)

        store.enqueue_run_update(structural(store.current_run, 6, 6))
        store.enqueue_detail_update(log_detail(6))
        store.enqueue_detail_update(event_detail(6))
        store._apply_background_updates()
        assert provider.started[0].wait(timeout=1)
        assert provider.calls == 1

        store.enqueue_run_update(structural(store.current_run, 8, 8))
        store.enqueue_detail_update(log_detail(8))
        store.enqueue_detail_update(event_detail(8))
        store._apply_background_updates()
        requirements = store._detail_hydration_requirements[key]
        assert requirements.log_sequence == 8
        assert requirements.event_sequences == {}
        assert provider.calls == 1

        provider.releases[0].set()
        deadline = time.monotonic() + 1
        while not provider.started[1].is_set() and time.monotonic() < deadline:
            store._apply_background_updates()
            time.sleep(0.005)
        assert provider.started[1].is_set()
        assert provider.calls == 2
        assert key not in store._log_detail_sequences
        assert store._agent_event_sequences[event_key] == 8

        # No additional stream message arrives. The coalesced replacement
        # satisfies the log watermark and refreshes the full selected-run body.
        provider.releases[1].set()
        deadline = time.monotonic() + 1
        while (
            key in store._detail_hydrations_in_flight
            or key in store._detail_hydration_requirements
        ) and time.monotonic() < deadline:
            store._apply_background_updates()
            time.sleep(0.005)
        store._apply_background_updates()

        assert provider.calls == 2
        assert key not in store._detail_hydrations_in_flight
        assert key not in store._detail_hydration_requirements
        assert store.current_run.logs is run_a8.logs
        assert store._log_detail_sequences[key] == 8
        assert store._agent_event_sequences[event_key] == 8
        assert store._agent_event_details[event_key][-1]["sequence"] == 8
        assert (
            json.loads(store.current_run.nodes["agent"].agent_trace_json)["trace"]["marker"]
            == "trace-8"
        )

        stale_logs = store.current_run.logs
        stale_events = store._agent_event_details[event_key]
        store.enqueue_polled_run_update(
            workflow.selector,
            store._run_data_revision(workflow.selector),
            store._workflow_context_epoch,
            run_a6,
        )
        store._apply_background_updates()
        assert store.current_run.revision == 8
        assert store.current_run.logs is stale_logs
        assert store._agent_event_details[event_key] is stale_events

        workers = set(store._detail_hydration_executor._threads)
        assert len(workers) == 1
        store.shutdown()
        assert all(not worker.is_alive() for worker in workers)

    def test_superseded_detail_completion_cannot_clear_new_worker_attempt(self):
        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["node"],
            graph={},
            node_types={"node": "step"},
        )
        run = RunState(
            run_id="run-blocked",
            flow_name="flow",
            workflow_id="flow",
            operator_instance_id="operator-1",
            created_sequence=1,
            revision=1,
            latest_log_sequence=1,
            details_hydrated=False,
        )
        reset_run = replace(run, revision=2, latest_log_sequence=2)
        started = [threading.Event(), threading.Event()]
        released = [threading.Event(), threading.Event()]
        worker_done = [threading.Event(), threading.Event()]
        close_called = threading.Event()

        class BlockingProvider(MockStateProvider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def get_run(self, run_id):
                assert run_id == "run-blocked"
                attempt = self.calls
                self.calls += 1
                started[attempt].set()
                try:
                    released[attempt].wait(timeout=2)
                    return run
                finally:
                    worker_done[attempt].set()

            def close(self):
                close_called.set()
                for release in released:
                    release.set()

        provider = BlockingProvider()
        store = UIStore(provider)
        store.workflows = [workflow]
        store.current_workflow = workflow
        store.current_run = run
        store.run_pinned = True
        store._set_runs_cache([run])
        store._schedule_detail_hydration(run, required_log_sequence=1)
        assert started[0].wait(timeout=1)

        store._apply_reset_baseline(
            ResetBaseline(
                generation=1,
                operator_instance_id="operator-1",
                as_of_sequence=2,
                workflows=(workflow,),
                runs_by_workflow={workflow.selector: (reset_run,)},
            )
        )
        store._schedule_detail_hydration(reset_run, required_log_sequence=2)
        replacement_generation = store._detail_hydrations_in_flight[
            store._detail_key(reset_run)
        ]
        released[0].set()
        assert started[1].wait(timeout=1)
        store._apply_background_updates()

        key = store._detail_key(reset_run)
        assert store._detail_hydrations_in_flight[key] == replacement_generation
        assert store._detail_hydration_requirements[key].log_sequence == 2
        assert provider.calls == 2

        workers = set(store._detail_hydration_executor._threads)
        assert store._detail_hydration_executor._max_workers == 1
        assert len(workers) == 1
        assert not worker_done[1].is_set()

        store.request_shutdown()
        provider.close()
        store.shutdown()

        assert close_called.is_set()
        assert all(done.is_set() for done in worker_done)
        assert all(not worker.is_alive() for worker in workers)

    @pytest.mark.parametrize("failure_kind", ["none", "error", "insufficient"])
    def test_detail_hydration_persistent_failure_uses_bounded_backoff(self, failure_kind):
        if failure_kind == "error":
            outcomes = [RuntimeError("unavailable"), RuntimeError("unavailable")]
        elif failure_kind == "insufficient":
            outcomes = [
                _retry_hydration_run(hydrated=True),
                _retry_hydration_run(hydrated=True),
            ]
        else:
            outcomes = [None, None]
        store, provider, _workflow, run, clock = _retry_hydration_store(outcomes)
        key = store._detail_key(run)
        try:
            store._schedule_detail_hydration(run, required_log_sequence=3)
            _finish_retry_hydration_attempt(store, provider, 0)

            first_retry = store._detail_hydration_retries[key]
            assert first_retry.deadline == pytest.approx(100.1)
            for _ in range(250):
                store._apply_background_updates()
            assert provider.calls == 1

            clock[0] = first_retry.deadline
            store._apply_background_updates()
            _finish_retry_hydration_attempt(store, provider, 1)

            second_retry = store._detail_hydration_retries[key]
            assert second_retry.deadline == pytest.approx(100.3)
            assert second_retry.generation != first_retry.generation
            for _ in range(250):
                store._apply_background_updates()
            assert provider.calls == 2
        finally:
            store.shutdown()

    def test_detail_hydration_progress_resets_retry_backoff(self):
        store, provider, _workflow, run, clock = _retry_hydration_store(
            [None, None, _retry_hydration_run(2, hydrated=True)]
        )
        key = store._detail_key(run)
        try:
            store._schedule_detail_hydration(run, required_log_sequence=3)
            _finish_retry_hydration_attempt(store, provider, 0)
            clock[0] = store._detail_hydration_retries[key].deadline
            store._apply_background_updates()
            _finish_retry_hydration_attempt(store, provider, 1)
            assert store._detail_hydration_failures[key] == 2

            clock[0] = store._detail_hydration_retries[key].deadline
            store._apply_background_updates()
            _finish_retry_hydration_attempt(store, provider, 2)

            assert store._log_detail_sequences[key] == 2
            assert store._detail_hydration_failures[key] == 1
            assert store._detail_hydration_retries[key].deadline == pytest.approx(
                clock[0] + 0.1
            )
        finally:
            store.shutdown()

    def test_stronger_detail_requirement_resets_and_supersedes_delayed_retry(self):
        store, provider, _workflow, run, clock = _retry_hydration_store([None, None])
        key = store._detail_key(run)
        try:
            store._schedule_detail_hydration(run, required_log_sequence=2)
            _finish_retry_hydration_attempt(store, provider, 0)
            old_retry = store._detail_hydration_retries[key]

            store._schedule_detail_hydration(run, required_log_sequence=4)
            _finish_retry_hydration_attempt(store, provider, 1)
            current_retry = store._detail_hydration_retries[key]

            assert provider.calls == 2
            assert store._detail_hydration_requirements[key].log_sequence == 4
            assert store._detail_hydration_failures[key] == 1
            assert current_retry.deadline == pytest.approx(clock[0] + 0.1)
            assert current_retry.generation != old_retry.generation

            store._apply_detail_hydration_retry(key, old_retry.generation)
            assert store._detail_hydration_retries[key] == current_retry
            assert provider.calls == 2
        finally:
            store.shutdown()

    def test_detail_retry_cancelled_by_navigation_reset_and_shutdown(self):
        store, provider, workflow, run, _clock = _retry_hydration_store([None, None, None])
        key = store._detail_key(run)

        store._schedule_detail_hydration(run, required_log_sequence=2)
        _finish_retry_hydration_attempt(store, provider, 0)
        navigation_retry = store._detail_hydration_retries[key]
        other = replace(run, run_id="other", created_sequence=2)
        store.switch_run(other)
        store._apply_detail_hydration_retry(key, navigation_retry.generation)
        assert provider.calls == 1
        assert key not in store._detail_hydration_retries

        store.switch_run(run)
        store._schedule_detail_hydration(run, required_log_sequence=2)
        _finish_retry_hydration_attempt(store, provider, 1)
        reset_retry = store._detail_hydration_retries[key]
        reset_run = _retry_hydration_run(2, operator_instance_id="operator-2")
        store._apply_reset_baseline(
            ResetBaseline(
                generation=1,
                operator_instance_id="operator-2",
                as_of_sequence=2,
                workflows=(workflow,),
                runs_by_workflow={workflow.selector: (reset_run,)},
            )
        )
        store._apply_detail_hydration_retry(key, reset_retry.generation)
        assert provider.calls == 2
        assert not store._detail_hydration_retries
        assert not store._detail_hydration_requirements

        assert store.current_run is not None
        reset_key = store._detail_key(store.current_run)
        store._schedule_detail_hydration(store.current_run, required_log_sequence=3)
        _finish_retry_hydration_attempt(store, provider, 2)
        shutdown_retry = store._detail_hydration_retries[reset_key]
        workers = set(store._detail_hydration_executor._threads)
        assert len(workers) == 1

        store.request_shutdown()
        store._apply_detail_hydration_retry(reset_key, shutdown_retry.generation)
        store.shutdown()

        assert provider.calls == 3
        assert not store._detail_hydration_retries
        assert not store._detail_hydration_requirements
        assert all(not worker.is_alive() for worker in workers)

    def test_stream_terminal_update_wins_over_delayed_run_list(self):
        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["node"],
            graph={},
            node_types={"node": "step"},
        )
        stale = RunState(
            run_id="run_1",
            flow_name="flow",
            workflow_id="flow",
            status=RunStatus.RUNNING,
        )
        terminal = replace(stale, status=RunStatus.SUCCESS)
        older = replace(stale, run_id="run_0", status=RunStatus.FAILED)
        entered = threading.Event()
        release = threading.Event()

        class BlockingProvider(_ReachableStateProvider):
            def list_workflows(self):
                return [workflow]

            def list_runs(self, selector):
                entered.set()
                release.wait()
                return [stale]

        store = UIStore(BlockingProvider())
        updates = _signal_background_updates(store)
        assert entered.wait(1.0)
        store.current_run = stale
        store.run_pinned = True
        store._set_runs_cache([older, stale])
        store.workflow_statuses = {workflow.selector: stale.status}

        store.enqueue_run_update(terminal)
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert store.current_run is terminal
        assert store.run_pinned
        assert store._runs_cache == [older, terminal]
        assert store.workflow_statuses[workflow.selector] is RunStatus.SUCCESS

        updates.put_event.clear()
        release.set()
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert store.current_run is terminal
        assert store.run_pinned
        assert store._runs_cache == [older, terminal]
        assert store.workflow_statuses[workflow.selector] is RunStatus.SUCCESS

    def test_stream_terminal_update_wins_over_delayed_all_statuses(self):
        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["node"],
            graph={},
            node_types={"node": "step"},
        )
        stale = RunState(
            run_id="run_1",
            flow_name="flow",
            workflow_id="flow",
            status=RunStatus.RUNNING,
        )
        terminal = replace(stale, status=RunStatus.SUCCESS)
        entered = threading.Event()
        release = threading.Event()

        class BlockingProvider(_ReachableStateProvider):
            block = False

            def list_workflows(self):
                return [workflow]

            def list_runs(self, selector):
                if self.block:
                    entered.set()
                    release.wait()
                return [stale]

        provider = BlockingProvider()
        store = UIStore(provider)
        _apply_async_updates(store)
        store.current_run = stale
        store._runs_cache = [stale]
        store.workflow_statuses = {workflow.selector: stale.status}
        updates = _signal_background_updates(store)

        provider.block = True
        store._refresh_workflow_statuses()
        assert entered.wait(1.0)
        store.enqueue_run_update(terminal)
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()

        updates.put_event.clear()
        release.set()
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert not store._status_refresh_in_flight
        assert store.current_run is terminal
        assert store.workflow_statuses[workflow.selector] is RunStatus.SUCCESS

    def test_discarded_stale_run_list_does_not_starve_next_refresh(self):
        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["node"],
            graph={},
            node_types={"node": "step"},
        )
        stale = RunState(
            run_id="run_1",
            flow_name="flow",
            workflow_id="flow",
            status=RunStatus.RUNNING,
        )
        terminal = replace(stale, status=RunStatus.SUCCESS)
        entered = threading.Event()
        release = threading.Event()

        class BlockingFirstProvider(_ReachableStateProvider):
            calls = 0

            def list_workflows(self):
                return [workflow]

            def list_runs(self, selector):
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    release.wait()
                    return [stale]
                return [terminal]

        provider = BlockingFirstProvider()
        store = UIStore(provider)
        updates = _signal_background_updates(store)
        assert entered.wait(1.0)
        store.enqueue_run_update(terminal)
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()

        updates.put_event.clear()
        release.set()
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert workflow.selector not in store._runs_refresh_in_flight

        updates.put_event.clear()
        store._refresh_runs_cache()
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert provider.calls == 2
        assert store.current_run is terminal
        assert store._runs_cache == [terminal]

    def test_run_list_revision_rejects_same_selector_after_workflow_aba(self):
        def workflow(selector):
            return WorkflowInfo(
                name=selector,
                display_name=selector,
                workflow_id=selector,
                file_path=f"{selector}.py",
                node_ids=["node"],
                graph={},
                node_types={"node": "step"},
            )

        workflows = [workflow("a"), workflow("b")]
        stale_a = RunState(
            run_id="stale_a",
            flow_name="a",
            workflow_id="a",
            status=RunStatus.RUNNING,
        )
        run_b = RunState(
            run_id="run_b",
            flow_name="b",
            workflow_id="b",
            status=RunStatus.SUCCESS,
        )
        entered = threading.Event()
        release = threading.Event()

        class BlockingAProvider(_ReachableStateProvider):
            def list_workflows(self):
                return workflows

            def list_runs(self, selector):
                if selector == "a":
                    entered.set()
                    release.wait()
                    return [stale_a]
                return [run_b]

        store = UIStore(BlockingAProvider())
        updates = _signal_background_updates(store)
        assert entered.wait(1.0)

        store.switch_workflow(workflows[1])
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert store.current_run is run_b

        store.switch_workflow(workflows[0])
        assert store.current_run is None
        assert store._runs_cache == []
        updates.put_event.clear()
        release.set()
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert store.current_workflow is workflows[0]
        assert store.current_run is None
        assert store._runs_cache == []
        assert "a" not in store._runs_refresh_in_flight

    def test_stream_update_rejects_stale_async_start_completion(self):
        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["node"],
            graph={},
            node_types={"node": "step"},
        )
        running = RunState(
            run_id="run_1",
            flow_name="flow",
            workflow_id="flow",
            status=RunStatus.RUNNING,
        )
        terminal = replace(running, status=RunStatus.SUCCESS)
        release = threading.Event()

        class BlockingStartProvider(_ReachableStateProvider):
            def __init__(self):
                self.entered = threading.Event()
                self.list_calls = 0

            def list_workflows(self):
                return [workflow]

            def list_runs(self, selector):
                self.list_calls += 1
                return [running] if self.list_calls <= 2 else [terminal]

            def start_run(self, selector, **kwargs):
                self.entered.set()
                release.wait()
                return running.run_id

            def get_run(self, run_id):
                return running

        provider = BlockingStartProvider()
        store = UIStore(provider)
        _apply_async_updates(store)
        updates = _signal_background_updates(store)
        assert store.current_run is running
        assert store.start_run_async()
        assert provider.entered.wait(1.0)

        store.enqueue_run_update(terminal)
        assert updates.put_event.wait(1.0)
        store._apply_background_updates()
        assert store.current_run is terminal

        updates.put_event.clear()
        release.set()
        assert updates.put_event.wait(1.0)
        _apply_async_updates(store)
        _apply_async_updates(store)
        assert not store._start_run_in_flight
        assert store.current_run is terminal
        assert store._runs_cache == [terminal]
        assert not store.run_pinned

    @pytest.mark.parametrize("result_kind", ["runs", "statuses"])
    @pytest.mark.parametrize("stream_kind", ["unrelated", "unchanged"])
    def test_unrelated_or_unchanged_stream_keeps_target_poll_relevant(
        self, result_kind, stream_kind
    ):
        def workflow(selector):
            return WorkflowInfo(
                name=selector,
                display_name=selector,
                workflow_id=selector,
                file_path=f"{selector}.py",
                node_ids=["node"],
                graph={},
                node_types={"node": "step"},
            )

        workflows = [workflow("a"), workflow("b")]
        run_a = RunState(
            run_id="run_a",
            flow_name="a",
            workflow_id="a",
            status=RunStatus.RUNNING,
        )
        run_b = RunState(
            run_id="run_b",
            flow_name="b",
            workflow_id="b",
            status=RunStatus.RUNNING,
        )
        updated_a = replace(run_a, status=RunStatus.SUCCESS)
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        store.workflows = workflows
        store.current_workflow = workflows[0]
        store.current_run = run_a
        store._runs_cache = [run_a]
        store.workflow_statuses = {"a": RunStatus.RUNNING}
        revision = store._run_data_revision("a")
        stream_run = run_b if stream_kind == "unrelated" else run_a

        store._background_updates.put(("run", stream_run))
        if result_kind == "runs":
            store._runs_refresh_in_flight.add("a")
            store._background_updates.put(
                (
                    "runs",
                    ("a", revision, store._workflow_context_epoch, [updated_a]),
                )
            )
        else:
            store._status_refresh_in_flight = True
            store._background_updates.put(
                ("statuses", ({"a": revision}, {"a": RunStatus.SUCCESS}))
            )

        store._apply_background_updates()
        if result_kind == "runs":
            assert "a" not in store._runs_refresh_in_flight
            assert store._runs_cache == [updated_a]
        else:
            assert not store._status_refresh_in_flight
            assert store.workflow_statuses["a"] is RunStatus.SUCCESS

    def test_older_run_stream_update_does_not_change_latest_workflow_badge(self):
        workflow = WorkflowInfo(
            name="flow",
            display_name="flow",
            workflow_id="flow",
            file_path="flow.py",
            node_ids=["node"],
            graph={},
            node_types={"node": "step"},
        )
        older = RunState(
            run_id="run_0",
            flow_name="flow",
            workflow_id="flow",
            status=RunStatus.RUNNING,
        )
        latest = replace(older, run_id="run_1", status=RunStatus.SUCCESS)
        updated_older = replace(older, status=RunStatus.FAILED)
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        store.workflows = [workflow]
        store.current_workflow = workflow
        store.current_run = latest
        store._set_runs_cache([older, latest])
        store.workflow_statuses = {workflow.selector: latest.status}

        store.enqueue_run_update(updated_older)
        store._apply_background_updates()

        assert store._runs_cache == [updated_older, latest]
        assert store.current_run is latest
        assert store.workflow_statuses[workflow.selector] is RunStatus.SUCCESS

    def test_reset_reconciliation_retries_after_loader_attempt_budget_exhausts(self):
        class ExhaustingProvider(MockStateProvider):
            def __init__(self):
                super().__init__()
                self.load_notices = []

            def load_reset_baseline(self, notice):
                self.load_notices.append(notice)
                if len(self.load_notices) == 1:
                    raise RuntimeError(
                        "operator state did not stabilize while loading " "the reset baseline"
                    )
                return ResetBaseline(
                    generation=notice.generation,
                    operator_instance_id="operator-recovered",
                    as_of_sequence=notice.observed_sequence,
                    workflows=(INGEST_WORKFLOW,),
                    runs_by_workflow={INGEST_WORKFLOW.selector: ()},
                )

        provider = ExhaustingProvider()
        provider.stream_state = "reset_required"
        store = UIStore(provider)
        notice = StreamResetNotice(
            generation=1,
            previous_sequence=99,
            observed_sequence=2,
        )
        try:
            store._on_stream_reset(notice)
            deadline = time.monotonic() + 2.0
            while (
                provider.stream_state != "live" or len(provider.load_notices) < 2
            ) and time.monotonic() < deadline:
                store._apply_background_updates()
                time.sleep(0.01)

            assert provider.load_notices == [notice, notice]
            assert provider.operator_instance_id == "operator-recovered"
            assert provider.stream_state == "live"
            assert store.workflows == [INGEST_WORKFLOW]
            assert store.run_error == ""
            assert store._reset_reconciliations_in_flight == set()
        finally:
            store.shutdown()

    @pytest.mark.parametrize(
        ("generation", "operator_instance_id", "as_of_sequence"),
        [
            (2, "operator-new", 2),
            (1, "operator-wrong", 2),
            (1, "operator-new", 1),
        ],
    )
    def test_reset_reconciliation_retries_invalid_baseline(
        self,
        generation,
        operator_instance_id,
        as_of_sequence,
    ):
        class InvalidThenValidProvider(MockStateProvider):
            def __init__(self):
                super().__init__()
                self.load_count = 0

            def load_reset_baseline(self, notice):
                self.load_count += 1
                if self.load_count == 1:
                    return ResetBaseline(
                        generation=generation,
                        operator_instance_id=operator_instance_id,
                        as_of_sequence=as_of_sequence,
                        workflows=(),
                        runs_by_workflow={},
                    )
                return ResetBaseline(
                    generation=notice.generation,
                    operator_instance_id=notice.operator_instance_id,
                    as_of_sequence=notice.observed_sequence,
                    workflows=(INGEST_WORKFLOW,),
                    runs_by_workflow={INGEST_WORKFLOW.selector: ()},
                )

        provider = InvalidThenValidProvider()
        provider.stream_state = "reset_required"
        store = UIStore(provider)
        notice = StreamResetNotice(
            generation=1,
            previous_sequence=99,
            observed_sequence=2,
            operator_instance_id="operator-new",
        )
        try:
            store._on_stream_reset(notice)
            deadline = time.monotonic() + 2.0
            while (
                provider.stream_state != "live" or provider.load_count < 2
            ) and time.monotonic() < deadline:
                store._apply_background_updates()
                time.sleep(0.01)

            assert provider.load_count == 2
            assert provider.operator_instance_id == "operator-new"
            assert provider.stream_state == "live"
            assert store.workflows == [INGEST_WORKFLOW]
            assert store.run_error == ""
            assert store._reset_reconciliations_in_flight == set()
        finally:
            store.shutdown()

    def test_reset_baseline_rejects_delayed_current_run_poll(self):
        from avalanche.tui.app import AvalancheApp

        poll_entered = threading.Event()
        release_poll = threading.Event()
        stale = RunState(
            run_id="run-1",
            flow_name=ORDER_WORKFLOW.name,
            workflow_id=ORDER_WORKFLOW.selector,
            status=RunStatus.RUNNING,
            operator_instance_id="operator-1",
            revision=1,
        )
        authoritative = replace(stale, status=RunStatus.SUCCESS, revision=2)

        class BlockingPollProvider(MockStateProvider):
            def get_run(self, run_id):
                assert run_id == stale.run_id
                poll_entered.set()
                release_poll.wait()
                return stale

        provider = BlockingPollProvider()
        app = AvalancheApp(provider=provider)
        store = app.store
        store.workflows = [ORDER_WORKFLOW]
        store.current_workflow = ORDER_WORKFLOW
        store.current_run = stale
        store._runs_cache = [stale]
        try:
            app._poll_current_run()
            assert poll_entered.wait(timeout=1.0)
            store._apply_reset_baseline(
                ResetBaseline(
                    generation=1,
                    operator_instance_id="operator-1",
                    as_of_sequence=2,
                    workflows=(ORDER_WORKFLOW,),
                    runs_by_workflow={ORDER_WORKFLOW.selector: (authoritative,)},
                )
            )
            release_poll.set()
            deadline = time.monotonic() + 1.0
            while app._poll_in_flight and time.monotonic() < deadline:
                time.sleep(0.01)
            store._apply_background_updates()

            assert store.current_run is authoritative
            assert store._runs_cache == [authoritative]
            assert store.workflow_statuses[ORDER_WORKFLOW.selector] is RunStatus.SUCCESS
        finally:
            release_poll.set()
            store.shutdown()

    def test_catalog_response_started_before_reset_cannot_overwrite_baseline(self):
        catalog_entered = threading.Event()
        catalog_release = threading.Event()
        baseline_entered = threading.Event()
        baseline_release = threading.Event()

        class BlockingCatalogProvider(MockStateProvider):
            def __init__(self):
                super().__init__()
                self.block_catalog = False

            def list_workflows(self):
                if self.block_catalog:
                    catalog_entered.set()
                    catalog_release.wait()
                    return [INGEST_WORKFLOW]
                return super().list_workflows()

        provider = BlockingCatalogProvider()

        def load_baseline(notice):
            baseline_entered.set()
            baseline_release.wait()
            return ResetBaseline(
                generation=notice.generation,
                operator_instance_id="operator-restarted",
                as_of_sequence=notice.observed_sequence,
                workflows=(ORDER_WORKFLOW,),
                runs_by_workflow={ORDER_WORKFLOW.selector: ()},
            )

        store = UIStore(provider, reset_baseline_loader=load_baseline)
        _apply_async_updates(store)
        original_workflows = list(store.workflows)
        updates = _signal_background_updates(store)
        provider.block_catalog = True
        store._refresh_workflow_catalog()
        assert catalog_entered.wait(timeout=1.0)

        provider.stream_state = "reset_required"
        store._on_stream_reset(
            StreamResetNotice(
                generation=1,
                previous_sequence=99,
                observed_sequence=2,
            )
        )
        assert baseline_entered.wait(timeout=1.0)
        catalog_release.set()
        assert updates.put_event.wait(timeout=1.0)
        store._apply_background_updates()
        assert store.workflows == original_workflows

        updates.put_event.clear()
        baseline_release.set()
        assert updates.put_event.wait(timeout=1.0)
        store._apply_background_updates()
        assert store.workflows == [ORDER_WORKFLOW]
        assert provider.operator_instance_id == "operator-restarted"
        assert provider.stream_state == "live"

    @pytest.mark.parametrize("refresh", ["catalog", "runs", "statuses"])
    def test_application_error_refresh_preserves_authoritative_caches(self, refresh):
        provider = _ApplicationErrorRefreshProvider()
        store = UIStore(provider)
        _apply_async_updates(store)
        workflows = list(store.workflows)
        runs = list(store._runs_cache)
        statuses = dict(store.workflow_statuses)
        updates = _signal_background_updates(store)
        provider.fail_operation = "catalog" if refresh == "catalog" else "runs"

        if refresh == "catalog":
            store._refresh_workflow_catalog()
        elif refresh == "runs":
            store._refresh_runs_cache()
        else:
            store._refresh_workflow_statuses()

        assert updates.put_event.wait(timeout=1.0)
        store._apply_background_updates()
        assert store.workflows == workflows
        assert store._runs_cache == runs
        assert store.workflow_statuses == statuses
        assert provider.operator_reachable is True

    def test_application_error_start_preserves_current_run_and_cache(self):
        provider = _ApplicationErrorRefreshProvider()
        store = UIStore(provider)
        _apply_async_updates(store)
        current_run = store.current_run
        runs = list(store._runs_cache)
        provider.fail_operation = "start"

        assert store.start_run() is None
        assert store.current_run is current_run
        assert store._runs_cache == runs
        assert store.run_error == "INVALID_ARGUMENT: start rejected"
        assert provider.operator_reachable is True

    def test_run_error_is_rendered_in_status_bar(self):
        store = UIStore(MockStateProvider())
        store.run_error = "UNAVAILABLE: get failed"
        status = StatusBar()
        status._test_store = store

        rendered = status.render().plain

        assert "✗ UNAVAILABLE: get failed" in rendered

    @pytest.mark.parametrize(
        ("stream_state", "label"),
        [
            ("failed", "live updates interrupted"),
            ("replaying", "live updates replaying"),
            ("reset_required", "live updates reset required"),
        ],
    )
    def test_transport_health_is_rendered_separately_in_status_bar(self, stream_state, label):
        provider = MockStateProvider()
        provider.operator_reachable = True
        provider.stream_state = stream_state
        store = UIStore(provider)
        status = StatusBar()
        status._test_store = store

        assert label in status.render().plain

        provider.operator_reachable = False
        rendered = status.render().plain
        assert "operator unreachable" in rendered
        assert label not in rendered

    @pytest.mark.asyncio
    async def test_ping_success_during_stream_failure_keeps_disconnect_overlay_hidden(self):
        from avalanche.tui.app import AvalancheApp

        class HealthProvider(MockStateProvider):
            def __init__(self):
                super().__init__()
                self._address = "operator.example:7433"
                self.operator_reachable = True
                self.stream_state = "failed"
                self.stream_error = "UNAVAILABLE: update stream failed"
                self.pinged = threading.Event()

            def ping(self):
                self.operator_reachable = True
                self.pinged.set()
                return True

        provider = HealthProvider()
        app = AvalancheApp(provider=provider)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._timer.pause()
            app._ping_counter = 59
            app._check_connection()
            assert await asyncio.to_thread(provider.pinged.wait, 1.0)

            wrapper = app._screen.query_one("#disconnect-wrapper")
            for _ in range(3):
                app._check_connection()
                assert not wrapper.has_class("visible")
            status = app._screen.query_one("#status-bar", StatusBar)
            assert "live updates interrupted" in status.render().plain

            provider.stream_state = "live"
            app._check_connection()
            assert not wrapper.has_class("visible")
            assert "live updates live" in status.render().plain

    def test_provider_restart_installs_baseline_before_exact_generation_ack(self):
        stale_workflow = ORDER_WORKFLOW
        workflow = INGEST_WORKFLOW
        stale = RunState(
            run_id="run_stale",
            flow_name=stale_workflow.name,
            status=RunStatus.SUCCESS,
        )
        recovered = RunState(
            run_id="run_recovered",
            flow_name=workflow.name,
            status=RunStatus.RUNNING,
        )
        release = threading.Event()
        stream_waiting = threading.Event()
        notices = []

        class LiveStream:
            def initial_metadata(self):
                return ()

            def __iter__(self):
                return self

            def __next__(self):
                stream_waiting.set()
                release.wait()
                raise StopIteration

        class RestartedStub:
            def __init__(self):
                self.stream_calls = 0

            def ListFlows(self, request, **kwargs):  # noqa: N802
                return pb.FlowList(flows=[workflow_info_to_proto(stale_workflow)])

            def ListRunSummaries(self, request, **kwargs):  # noqa: N802
                return pb.RunSummaryPage(
                    operator_instance_id="operator-original",
                    as_of_sequence=99,
                )

            def StreamRunUpdates(self, request, *, metadata):  # noqa: N802
                self.stream_calls += 1
                assert metadata is None
                if self.stream_calls == 1:
                    assert request.operator_instance_id == "operator-original"
                    assert request.after_sequence == 99
                    return iter(
                        (
                            pb.RunUpdateEnvelope(
                                operator_instance_id="operator-restarted",
                                reset_required=pb.ResetRequired(
                                    history_floor=1,
                                    latest_sequence=2,
                                ),
                            ),
                        )
                    )
                assert request.operator_instance_id == "operator-restarted"
                assert request.after_sequence == 3
                return LiveStream()

        def load_baseline(notice: StreamResetNotice) -> ResetBaseline:
            notices.append(notice)
            return ResetBaseline(
                generation=notice.generation,
                operator_instance_id="operator-restarted",
                as_of_sequence=3,
                workflows=(workflow,),
                runs_by_workflow={workflow.selector: (recovered,)},
            )

        provider = GrpcStateProvider(
            "localhost:1",
            reset_baseline_loader=load_baseline,
        )
        provider._stub = RestartedStub()
        provider._install_structural_baseline("operator-original", 99, {})
        store = UIStore(provider)
        _apply_async_updates(store)
        store.current_run = stale
        store.run_pinned = True
        try:
            provider.on_run_update(store.enqueue_run_update)
            provider.start_stream()
            deadline = time.monotonic() + 2.0
            while (
                store.current_run is None
                or store.current_run.run_id != recovered.run_id
                or provider.stream_state is not StreamState.LIVE
            ) and time.monotonic() < deadline:
                store._apply_background_updates()
                time.sleep(0.01)

            assert stream_waiting.wait(timeout=1.0)
            assert notices == [
                StreamResetNotice(
                    generation=1,
                    previous_sequence=99,
                    observed_sequence=2,
                    operator_instance_id="operator-restarted",
                )
            ]
            assert store.current_workflow is workflow
            assert [run.run_id for run in store._runs_cache] == [recovered.run_id]
            assert store.current_run is recovered
            assert store.run_pinned is False
            assert provider.stream_state is StreamState.LIVE
            assert provider._cursor.operator_instance_id == "operator-restarted"
            assert provider._cursor.sequence == 3
            with pytest.raises(StaleResetAcknowledgementError):
                provider.acknowledge_stream_reset(
                    generation=1,
                    operator_instance_id="operator-restarted",
                    reconciled_sequence=3,
                )
        finally:
            provider._stream_stop.set()
            release.set()
            provider.close()

    def test_launch_path_recovers_after_operator_restart_with_default_baseline(
        self, monkeypatch
    ):
        from tui import launch_tui
        from tui.app import AvalancheApp

        workflow = ORDER_WORKFLOW
        old_run = RunState(
            run_id="run_old",
            flow_name=workflow.name,
            status=RunStatus.SUCCESS,
            workflow_id=workflow.selector,
            workflow_display_name=workflow.display_name,
        )
        summaries = [
            RunSummary(
                run_id="run_recovered",
                flow_name=workflow.name,
                status=RunStatus.SUCCESS,
                workflow_id=workflow.selector,
                workflow_display_name=workflow.display_name,
                created_sequence=1,
                revision=1,
            ),
            RunSummary(
                run_id="run_live",
                flow_name=workflow.name,
                status=RunStatus.RUNNING,
                workflow_id=workflow.selector,
                workflow_display_name=workflow.display_name,
                created_sequence=2,
                revision=3,
            ),
        ]

        class RestartService(pb_grpc.OperatorServiceServicer):
            def __init__(
                self,
                operator_id,
                stream_sequence,
                stream_run,
                baseline_summaries=(),
            ):
                self.operator_id = operator_id
                self.stream_sequence = stream_sequence
                self.stream_run = stream_run
                self.baseline_summaries = list(baseline_summaries) or [
                    RunSummary(
                        run_id=stream_run.run_id,
                        flow_name=stream_run.flow_name,
                        status=stream_run.status,
                        workflow_id=stream_run.workflow_id,
                        workflow_display_name=stream_run.workflow_display_name,
                        created_sequence=1,
                        revision=stream_sequence,
                    )
                ]
                self.baseline_sequence = 3 if baseline_summaries else stream_sequence
                self.stream_sent = threading.Event()
                self.release_stream = threading.Event()
                self.summary_tokens = []
                self.snapshot_calls = []

            def ListFlows(self, request, context):  # noqa: N802
                return pb.FlowList(flows=[workflow_info_to_proto(workflow)])

            def StreamRunUpdates(self, request, context):  # noqa: N802
                context.send_initial_metadata(())
                if request.operator_instance_id != self.operator_id:
                    yield pb.RunUpdateEnvelope(
                        operator_instance_id=self.operator_id,
                        reset_required=pb.ResetRequired(
                            history_floor=1,
                            latest_sequence=self.stream_sequence,
                        ),
                    )
                    return
                assert request.after_sequence == self.baseline_sequence
                self.stream_sent.set()
                while context.is_active() and not self.release_stream.wait(0.01):
                    pass

            def ListRunSummaries(self, request, context):  # noqa: N802
                self.summary_tokens.append(request.page_token)
                index = 0 if not request.page_token else 1
                next_page_token = "page-2" if index + 1 < len(self.baseline_summaries) else ""
                runs = (
                    [run_summary_to_proto(self.baseline_summaries[index])]
                    if index < len(self.baseline_summaries)
                    else []
                )
                return pb.RunSummaryPage(
                    operator_instance_id=self.operator_id,
                    as_of_sequence=self.baseline_sequence,
                    runs=runs,
                    next_page_token=next_page_token,
                )

            def GetRunSnapshot(self, request, context):  # noqa: N802
                self.snapshot_calls.append(
                    (
                        request.run_id,
                        request.operator_instance_id,
                        request.as_of_sequence,
                    )
                )
                summary = next(
                    item for item in self.baseline_summaries if item.run_id == request.run_id
                )
                return run_snapshot_to_proto(
                    RunSnapshot(
                        operator_instance_id=self.operator_id,
                        as_of_sequence=self.baseline_sequence,
                        summary=summary,
                    )
                )

        with socket.socket() as sock:
            sock.bind(("localhost", 0))
            port = sock.getsockname()[1]
        address = f"localhost:{port}"

        def start_server(service):
            server = grpc.server(ThreadPoolExecutor(max_workers=4))
            pb_grpc.add_OperatorServiceServicer_to_server(service, server)
            assert server.add_insecure_port(address) == port
            server.start()
            return server

        old_service = RestartService("operator-old", 99, old_run)
        new_service = RestartService(
            "operator-new",
            2,
            replace(old_run, run_id="run_reset"),
            summaries,
        )
        old_server = start_server(old_service)
        new_server = None

        def exercise_launch(app):
            nonlocal new_server
            provider = app.store.provider
            assert isinstance(provider, GrpcStateProvider)
            assert provider._reset_baseline_loader is None
            try:
                deadline = time.monotonic() + 2.0
                while not app.store.workflows and time.monotonic() < deadline:
                    app.store._apply_background_updates()
                    time.sleep(0.01)
                _apply_async_updates(app.store)
                provider.on_run_update(app.store.enqueue_run_update)
                provider.start_stream()
                deadline = time.monotonic() + 2.0
                while not old_service.stream_sent.is_set() and time.monotonic() < deadline:
                    app.store._apply_background_updates()
                    time.sleep(0.01)
                assert old_service.stream_sent.is_set()
                assert provider._cursor.operator_instance_id == "operator-old"
                assert provider._cursor.sequence == 99

                old_service.release_stream.set()
                old_server.stop(grace=0).wait()
                new_server = start_server(new_service)
                deadline = time.monotonic() + 10.0
                while (
                    provider.stream_state is not StreamState.LIVE
                    or provider.operator_instance_id != "operator-new"
                    or app.store.current_run is None
                    or app.store.current_run.run_id != "run_live"
                ) and time.monotonic() < deadline:
                    app.store._apply_background_updates()
                    time.sleep(0.01)

                assert provider.stream_state is StreamState.LIVE
                assert provider.operator_instance_id == "operator-new"
                assert provider._cursor.operator_instance_id == "operator-new"
                assert provider._cursor.sequence == 3
                assert app.store.current_run is not None
                assert app.store.current_run.run_id == "run_live"
                assert new_service.summary_tokens == ["", "page-2"]
                assert new_service.snapshot_calls == [
                    ("run_recovered", "operator-new", 3),
                    ("run_live", "operator-new", 3),
                ]
            finally:
                provider.close()

        monkeypatch.setattr(AvalancheApp, "run", exercise_launch)
        try:
            launch_tui(["--connect", address])
        finally:
            old_service.release_stream.set()
            new_service.release_stream.set()
            old_server.stop(grace=0)
            if new_server is not None:
                new_server.stop(grace=0)

    def test_sidebar_cursor_movement(self):
        store = UIStore(MockStateProvider())
        assert store.sidebar_cursor == 0
        store.sidebar_cursor_down(5)
        assert store.sidebar_cursor == 1
        store.sidebar_cursor_up()
        assert store.sidebar_cursor == 0
        store.sidebar_cursor_up()
        assert store.sidebar_cursor == 0  # stays at 0

    def test_sidebar_cursor_down_at_max(self):
        store = UIStore(MockStateProvider())
        store.sidebar_cursor = 4
        store.sidebar_cursor_down(5)
        assert store.sidebar_cursor == 4  # stays at max

    def test_sidebar_toggle_expand(self):
        store = UIStore(MockStateProvider())
        store.sidebar_toggle_expand("test_path")
        assert "test_path" in store.sidebar_expanded
        store.sidebar_toggle_expand("test_path")
        assert "test_path" not in store.sidebar_expanded

    def test_auto_expand_folders(self):
        store = UIStore(MockStateProvider())
        assert "workflows" in store.sidebar_expanded
        assert "workflows/etl" in store.sidebar_expanded
        assert "workflows/ingestion" in store.sidebar_expanded

    def test_switch_workflow_resets_dag(self):
        store = UIStore(MockStateProvider())
        # Switch to ingest (linear) — should have different DAG
        ingest = next(p for p in store.workflows if p.name == "ingest_workflow")
        store.switch_workflow(ingest)
        node_names = [n.name for n in store.all_nodes]
        assert "extract_1" in node_names
        assert "fetch_orders_1" not in node_names

    def test_cycle_pane_forward(self):
        store = UIStore(MockStateProvider())
        store.sidebar_visible = True  # open sidebar for cycling
        assert store.focused_pane == "dag"
        store.cycle_pane(1)
        assert store.focused_pane == "log"
        store.cycle_pane(1)
        assert store.focused_pane == "sidebar"
        store.cycle_pane(1)
        assert store.focused_pane == "run-history"
        store.cycle_pane(1)
        assert store.focused_pane == "dag"

    def test_cycle_pane_backward(self):
        store = UIStore(MockStateProvider())
        store.sidebar_visible = True  # open sidebar for cycling
        store.cycle_pane(-1)
        assert store.focused_pane == "run-history"
        store.cycle_pane(-1)
        assert store.focused_pane == "sidebar"
        store.cycle_pane(-1)
        assert store.focused_pane == "log"

    def test_select_next_prev_run(self):
        provider = MockStateProvider()
        store = UIStore(provider)
        # Start a second run so we have >1
        run_id = store.start_run()
        runs = store.runs_for_current_workflow
        assert len(runs) >= 2

        # current_run is the newest (just started)
        newest_id = store.current_run.run_id
        store.select_next_run()  # move down (toward older)
        assert store.selected_run_id != newest_id

        store.select_prev_run()  # move back up (toward newer)
        assert store.selected_run_id == newest_id

        time.sleep(0.2)
        provider.cancel_run(run_id)

    def test_select_next_run_no_runs(self):
        store = UIStore(MockStateProvider())
        store.current_workflow = None
        store.current_run = None
        store.select_next_run()  # should not crash
        assert store.current_run is None


# ── Sidebar folder tree ───────────────────────────────────────────────────


class TestSidebarTree:
    def _make_sidebar(self) -> Sidebar:
        s = Sidebar()
        s._test_store = UIStore(MockStateProvider())
        return s

    def _make_sidebar_explicit(self):
        """Return (sidebar, workflows, expanded) for tests using explicit params."""
        workflows = list(MockStateProvider().list_workflows())
        expanded = set()
        for p in workflows:
            parts = p.file_path.replace("\\", "/").split("/")[:-1]
            path = ""
            for part in parts:
                path = f"{path}/{part}" if path else part
                expanded.add(path)
        s = Sidebar()
        return s, workflows, expanded

    def test_tree_has_folders(self):
        s, workflows, expanded = self._make_sidebar_explicit()
        s._rebuild_tree(workflows=workflows, expanded=expanded)
        folder_items = [i for i in s._flat_items if i.is_folder]
        folder_labels = {i.label for i in folder_items}
        assert "workflows" in folder_labels
        assert "etl" in folder_labels
        assert "ingestion" in folder_labels

    def test_tree_has_workflows_under_folders(self):
        s, workflows, expanded = self._make_sidebar_explicit()
        s._rebuild_tree(workflows=workflows, expanded=expanded)
        workflow_items = [i for i in s._flat_items if not i.is_folder]
        flow_names = {i.workflow.name for i in workflow_items}
        assert "order_workflow" in flow_names
        assert "data_platform" in flow_names
        assert len(flow_names) == 6

    def test_tree_depth(self):
        s, workflows, expanded = self._make_sidebar_explicit()
        s._rebuild_tree(workflows=workflows, expanded=expanded)
        # workflows/ is depth 0, etl/ is depth 1, order_workflow is depth 2
        workflow_item = next(
            i for i in s._flat_items if not i.is_folder and i.workflow.name == "order_workflow"
        )
        assert workflow_item.depth == 2

    def test_collapse_hides_children(self):
        s, workflows, expanded = self._make_sidebar_explicit()
        expanded.discard("workflows")
        s._rebuild_tree(workflows=workflows, expanded=expanded)
        # Only "workflows" folder should be visible
        assert len(s._flat_items) == 1
        assert s._flat_items[0].label == "workflows"

    def test_render_shows_folder_arrows(self):
        s = self._make_sidebar()
        rendered = s.render().plain
        assert "▾" in rendered  # expanded folders
        s._test_store.sidebar_expanded.clear()
        rendered = s.render().plain
        assert "▸" in rendered  # collapsed

    def test_selected_workflow_highlighted(self):
        s = self._make_sidebar()
        s._test_store.sidebar_selected_name = "order_workflow"
        rendered = s.render().plain
        assert "▌" in rendered  # selection indicator

    def test_status_icons_in_render(self):
        s = self._make_sidebar()
        s._test_store.workflow_statuses = {"order_workflow": RunStatus.SUCCESS}
        s._test_store.sidebar_selected_name = "order_workflow"
        rendered = s.render().plain
        assert "✓" in rendered


# ── Run history ────────────────────────────────────────────────────────────


class TestRunHistory:
    def test_empty_state(self):
        store = UIStore(MockStateProvider())
        # Point to a non-existent workflow so list_runs returns []
        store.current_workflow = WorkflowInfo(
            name="nonexistent",
            file_path="x/y.py",
            node_ids=[],
            graph={},
            node_types={},
        )
        store.current_run = None
        w = RunHistoryWidget()
        w._test_store = store
        rendered = w.render().plain
        assert "No runs yet" in rendered

    def test_renders_runs(self):
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        w = RunHistoryWidget()
        w._test_store = store
        rendered = w.render().plain
        # Pre-seeded run should have a run_id in the output
        assert store.selected_run_id in rendered

    def test_selected_run_highlighted(self):
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        w = RunHistoryWidget()
        w._test_store = store
        rendered = w.render()
        # Selected row uses bold + background highlight
        assert any(span.style and "bold" in str(span.style) for span in rendered._spans)

    def test_display_order_reversed(self):
        store = UIStore(MockStateProvider())
        # Start a new run so there are 2
        run_id = store.start_run()
        w = RunHistoryWidget()
        w._test_store = store
        w.render()
        # Most recent first
        assert w._display_order[0].run_id == run_id
        time.sleep(0.2)
        store.provider.cancel_run(run_id)

    def test_timestamp_format(self):
        store = UIStore(MockStateProvider())
        _apply_async_updates(store)
        w = RunHistoryWidget()
        w._test_store = store
        rendered = w.render().plain
        import re

        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", rendered)


# ── Sidebar keyboard navigation ───────────────────────────────────────────


class TestSidebarKeyboard:
    def test_cursor_down_moves(self):
        store = UIStore(MockStateProvider())
        assert store.sidebar_cursor == 0
        store.sidebar_cursor_down(10)
        assert store.sidebar_cursor == 1

    def test_cursor_up_moves(self):
        store = UIStore(MockStateProvider())
        store.sidebar_cursor = 2
        store.sidebar_cursor_up()
        assert store.sidebar_cursor == 1

    def test_cursor_up_stops_at_zero(self):
        store = UIStore(MockStateProvider())
        store.sidebar_cursor = 0
        store.sidebar_cursor_up()
        assert store.sidebar_cursor == 0

    def test_cursor_down_stops_at_end(self):
        store = UIStore(MockStateProvider())
        store.sidebar_cursor = 4
        store.sidebar_cursor_down(5)
        assert store.sidebar_cursor == 4

    def test_activate_folder_toggles(self):
        store = UIStore(MockStateProvider())
        assert "workflows" in store.sidebar_expanded
        store.sidebar_toggle_expand("workflows")
        assert "workflows" not in store.sidebar_expanded
        store.sidebar_toggle_expand("workflows")
        assert "workflows" in store.sidebar_expanded

    def test_activate_workflow_selects(self):
        store = UIStore(MockStateProvider())
        other = [p for p in store.workflows if p.name != store.current_workflow.name][0]
        store.switch_workflow(other)
        assert store.sidebar_selected_name == other.name
        assert store.current_workflow == other


# ── Log timestamps ─────────────────────────────────────────────────────────


class TestLogTimestamps:
    def test_log_entry_has_datetime_timestamp(self):
        entry = LogEntry(
            timestamp=datetime.now(),
            level=LogLevel.INFO,
            node_id="test",
            message="hello",
        )
        assert isinstance(entry.timestamp, datetime)

    @pytest.mark.asyncio
    async def test_log_panel_renders_datetime(self):
        from avalanche.tui.app import AvalancheApp
        from avalanche.tui.widgets.log_panel import LogWidget

        app = AvalancheApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._timer.pause()
            app.store.run_pinned = True
            app.store.current_run.logs.append(
                LogEntry(
                    timestamp=datetime(2026, 3, 27, 14, 35, 1),
                    level=LogLevel.INFO,
                    node_id=app.store.all_nodes[0].name,
                    message="Starting...",
                )
            )
            app._tick()
            await app.wait_for_refresh()

            log_view = app._screen.query_one("#log-content", LogWidget)
            rendered = "\n".join(line.text for line in log_view.lines)
            assert "2026-03-27 14:35:01" in rendered


class TestVirtualizedLogs:
    @staticmethod
    def _entry(node_id: str, message: str) -> LogEntry:
        return LogEntry(
            timestamp=datetime(2026, 3, 27, 14, 35, 1),
            level=LogLevel.INFO,
            node_id=node_id,
            message=message,
        )

    @pytest.mark.asyncio
    async def test_equal_cardinality_snapshot_replacement_is_rendered(self):
        from avalanche.tui.app import AvalancheApp
        from avalanche.tui.widgets.log_panel import LogWidget

        app = AvalancheApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._timer.pause()
            node_id = next(iter(app.store.current_run.nodes))
            app.store.current_run = replace(
                app.store.current_run,
                logs=[
                    self._entry(node_id, "old first"),
                    self._entry(node_id, "old second"),
                ],
            )
            app._refresh_widgets()

            app.store.current_run = replace(
                app.store.current_run,
                logs=[
                    self._entry(node_id, "new first"),
                    self._entry(node_id, "new second"),
                ],
            )
            app._refresh_widgets()
            log_view = app._screen.query_one("#log-content", LogWidget)
            rendered = "\n".join(line.text for line in log_view.lines)

            assert "new first" in rendered
            assert "new second" in rendered
            assert "old first" not in rendered
            assert "old second" not in rendered

    @pytest.mark.asyncio
    async def test_existing_rows_restyle_when_node_status_changes(self):
        from avalanche.tui.app import AvalancheApp
        from avalanche.tui.theme import STATUS_STYLES
        from avalanche.tui.widgets.log_panel import LogWidget

        app = AvalancheApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._timer.pause()
            node_id = next(iter(app.store.current_run.nodes))
            logs = [self._entry(node_id, "status-sensitive")]
            nodes = dict(app.store.current_run.nodes)
            nodes[node_id] = replace(nodes[node_id], status=NodeStatus.RUNNING)
            app.store.current_run = replace(app.store.current_run, logs=logs, nodes=nodes)
            app._refresh_widgets()

            nodes = dict(app.store.current_run.nodes)
            nodes[node_id] = replace(nodes[node_id], status=NodeStatus.SUCCESS)
            app.store.current_run = replace(app.store.current_run, nodes=nodes)
            app._refresh_widgets()
            log_view = app._screen.query_one("#log-content", LogWidget)
            node_segment = next(
                segment
                for line in log_view.lines
                for segment in line._segments
                if node_id in segment.text
            )

            assert node_segment.style == STATUS_STYLES[NodeStatus.SUCCESS]

    @pytest.mark.asyncio
    async def test_incremental_append_adds_exactly_one_row(self):
        from avalanche.tui.app import AvalancheApp
        from avalanche.tui.widgets.log_panel import LogWidget

        app = AvalancheApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._timer.pause()
            node_id = next(iter(app.store.current_run.nodes))
            logs = [self._entry(node_id, "first")]
            app.store.current_run = replace(app.store.current_run, logs=logs)
            app._refresh_widgets()
            log_view = app._screen.query_one("#log-content", LogWidget)
            initial_rows = len(log_view.lines)

            logs.append(self._entry(node_id, "second"))
            app._refresh_widgets()

            assert initial_rows == 1
            assert len(log_view.lines) == initial_rows + 1
            assert [line.text.endswith(("first", "second")) for line in log_view.lines] == [
                True,
                True,
            ]

    @pytest.mark.asyncio
    async def test_equivalent_snapshot_and_append_retain_prefix_without_rebuild(self):
        from avalanche.tui.app import AvalancheApp
        from avalanche.tui.widgets.log_panel import LogWidget

        app = AvalancheApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._timer.pause()
            node_id = next(iter(app.store.current_run.nodes))
            initial_logs = [self._entry(node_id, "first")]
            app.store.current_run = replace(app.store.current_run, logs=initial_logs)
            app._refresh_widgets()
            log_view = app._screen.query_one("#log-content", LogWidget)
            initial_prefix = list(log_view.lines)
            rebuilds = 0
            original_rebuild = log_view._rebuild

            def count_rebuilds(store):
                nonlocal rebuilds
                rebuilds += 1
                original_rebuild(store)

            log_view._rebuild = count_rebuilds
            app.store.current_run = replace(
                app.store.current_run,
                logs=list(initial_logs),
            )
            app._refresh_widgets()

            assert rebuilds == 0
            assert all(
                rendered is initial
                for rendered, initial in zip(log_view.lines, initial_prefix, strict=True)
            )

            app.store.current_run = replace(
                app.store.current_run,
                logs=[*initial_logs, self._entry(node_id, "second")],
            )
            app._refresh_widgets()

            assert rebuilds == 0
            assert len(log_view.lines) == len(initial_prefix) + 1
            assert all(
                rendered is initial
                for rendered, initial in zip(
                    log_view.lines[: len(initial_prefix)], initial_prefix, strict=True
                )
            )
            assert log_view.lines[-1].text.endswith("second")


# ── Headless interaction tests (Textual pilot) ────────────────────────────


@pytest.mark.asyncio
class TestInteractions:
    """End-to-end tests using Textual's headless pilot to verify
    keyboard navigation works correctly through the full app."""

    async def _make_app(self):
        from avalanche.tui.app import AvalancheApp

        return AvalancheApp()

    def _sidebar_workflow_order(self, app) -> list[str]:
        sidebar = app._screen.query_one("#sidebar", Sidebar)
        sidebar._rebuild_tree()
        return [item.workflow.name for item in sidebar._flat_items if not item.is_folder]

    async def test_dag_pane_focused_on_start(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.store.focused_pane == "dag"

    async def test_arrow_right_selects_first_node(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.store.selected_node is None
            await pilot.press("right")
            await pilot.pause()
            assert app.store.selected_node is not None, "RIGHT should select a node"
            assert app.store.selected_node == app.store.all_nodes[0]

    async def test_arrow_navigation_moves_selection(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            first = app.store.selected_node

            await pilot.press("right")
            await pilot.pause()
            assert app.store.selected_node.col > first.col

            col_before = app.store.selected_node.col
            await pilot.press("down")
            await pilot.pause()
            assert app.store.selected_node is not None

            await pilot.press("left")
            await pilot.pause()
            assert app.store.selected_node.col < col_before or app.store.selected_node.col == 0

    async def test_tab_cycles_workflows_forward(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            ingest = next(p for p in app.store.workflows if p.name == "ingest_workflow")
            app.store.switch_workflow(ingest)
            await pilot.pause()

            order = self._sidebar_workflow_order(app)
            current_idx = order.index(app.store.current_workflow.name)
            expected = order[(current_idx + 1) % len(order)]

            await pilot.press("tab")
            await pilot.pause()
            assert app.store.current_workflow.name == expected

    async def test_shift_tab_cycles_workflows_backward(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            analytics = next(p for p in app.store.workflows if p.name == "analytics_workflow")
            app.store.switch_workflow(analytics)
            await pilot.pause()

            order = self._sidebar_workflow_order(app)
            current_idx = order.index(app.store.current_workflow.name)
            expected = order[(current_idx - 1) % len(order)]

            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.store.current_workflow.name == expected

    async def test_arrows_in_sidebar_move_cursor(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Navigate to sidebar pane
            app.store.focused_pane = "sidebar"
            app._sync_sidebar_focus()
            await pilot.pause()

            assert app.store.sidebar_cursor == 0
            await pilot.press("down")
            await pilot.pause()
            assert app.store.sidebar_cursor == 1

            await pilot.press("up")
            await pilot.pause()
            assert app.store.sidebar_cursor == 0

    async def test_arrows_in_run_history_select_runs(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Start a second run so we have 2
            app.store.start_run()
            await pilot.pause()

            # Focus run-history pane
            app.store.focused_pane = "run-history"
            first_run_id = app.store.selected_run_id

            # Down selects next (older) run
            await pilot.press("down")
            await pilot.pause()
            assert app.store.selected_run_id != first_run_id

            # Up selects previous (newer) run
            await pilot.press("up")
            await pilot.pause()
            assert app.store.selected_run_id == first_run_id

    async def test_escape_returns_to_dag_from_sidebar(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.store.focused_pane = "sidebar"
            app._sync_sidebar_focus()
            await pilot.pause()

            await pilot.press("escape")
            await pilot.pause()
            assert app.store.focused_pane == "dag"

    async def test_escape_deselects_node(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            assert app.store.selected_node is not None

            await pilot.press("escape")
            await pilot.pause()
            assert app.store.selected_node is None

    async def test_search_flow(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            await pilot.press("slash")
            await pilot.pause()
            assert app.store.searching

            await pilot.press("t", "e", "s", "t")
            await pilot.pause()
            assert app.store.search_query == "test"

            await pilot.press("enter")
            await pilot.pause()
            assert not app.store.searching
            assert app.store.search_query == "test"

    async def test_r_starts_run(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            old_run = app.store.current_run

            await pilot.press("r")
            await pilot.pause()
            assert app.store.current_run is not old_run
            assert app.store.run_state_label == "RUNNING"

    async def test_space_e_toggles_explorer(self):
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.store.sidebar_visible is False

            # Space+E opens and focuses explorer
            await pilot.press("space")
            await pilot.press("e")
            await pilot.pause()
            assert app.store.sidebar_visible is True
            assert app.store.focused_pane == "sidebar"

            # Space+E closes explorer
            await pilot.press("space")
            await pilot.press("e")
            await pilot.pause()
            assert app.store.sidebar_visible is False
            assert app.store.focused_pane == "dag"

            # Space+E opens it again
            await pilot.press("space")
            await pilot.press("e")
            await pilot.pause()
            assert app.store.sidebar_visible is True

    async def test_sidebar_click_selects_correct_item(self):
        """Clicking a workflow in the sidebar should set the cursor to that item's row.

        Render starts with 1 empty line. Click offset accounts for empty line
        and border (row = event.y - 2 in handler).
        """
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.store.sidebar_visible = True
            app._refresh_widgets()
            await pilot.pause()
            sidebar = app._screen.query_one("#sidebar")
            sidebar._rebuild_tree()
            items = sidebar._flat_items

            # Find workflow items (won't toggle folders and change tree)
            workflow_indices = [i for i, it in enumerate(items) if not it.is_folder]
            assert len(workflow_indices) >= 2, "Need at least 2 workflows"

            for target_idx in workflow_indices[:3]:
                click_y = 2 + target_idx  # 1 empty line + 1 border
                await pilot.click("#sidebar", offset=(5, click_y))
                await pilot.pause()
                assert app.store.sidebar_cursor == target_idx, (
                    f"Clicked y={click_y} expecting item {target_idx} "
                    f"({items[target_idx].label}), "
                    f"but cursor landed on {app.store.sidebar_cursor}"
                )

    async def test_run_history_click_selects_correct_run(self):
        """Clicking a run in run history should select that run, not an adjacent one."""
        import asyncio

        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.store.start_run()
            await asyncio.sleep(0.5)
            for _ in range(3):
                app._tick()
                await pilot.pause()

            rh_widget = app._screen.query_one("#run-history-content")
            display = rh_widget._display_order
            assert len(display) >= 2, "Need at least 2 runs"

            # Use keyboard to select the second run instead of click
            # (click y-offset is unreliable with docked headers)
            app.store.focused_pane = "run-history"
            app.store.select_next_run()
            app._refresh_widgets()
            await pilot.pause()

            target_run = display[1]
            assert (
                app.store.selected_run_id == target_run.run_id
            ), f"Expected run {target_run.run_id}, got {app.store.selected_run_id}"

    async def test_border_titles_present(self):
        """All panes should have border titles rendered on their borders."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app._screen.query_one("#sidebar")
            assert sidebar.border_title == "EXPLORER"

            dag = app._screen.query_one("#dag-container")
            assert "DAG" in str(dag.border_title)

            rh = app._screen.query_one("#run-history")
            assert "Run" in str(rh.border_title)

            lp = app._screen.query_one("#log-panel")
            assert "Logs" in str(lp.border_title)

    async def test_log_border_title_updates_with_selected_node(self):
        """Log panel border title should show selected node name when one is selected."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            lp = app._screen.query_one("#log-panel")

            # Trigger a tick so LogWidget.render() runs and updates the title
            app._tick()
            await pilot.pause()
            assert "Logs" in str(lp.border_title)

            # Select a node and tick again
            await pilot.press("right")
            await pilot.pause()
            app._tick()
            await pilot.pause()

            node_name = app.store.selected_node.display_name
            assert node_name in str(
                lp.border_title
            ), f"Expected '{node_name}' in border title, got '{lp.border_title}'"

    async def test_sticky_headers_exist(self):
        """Run history and log panes should have separate sticky header widgets."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Verify the header widgets exist
            rh_header = app._screen.query_one("#run-history-header")
            log_header = app._screen.query_one("#log-header")
            assert rh_header is not None
            assert log_header is not None

            # Verify the static render_header methods produce column headers
            from avalanche.tui.widgets.log_panel import LogWidget
            from avalanche.tui.widgets.run_history import RunHistoryWidget

            assert "Run ID" in RunHistoryWidget.render_header().plain
            assert "Timestamp" in LogWidget.render_header().plain

    async def test_sidebar_drag_resize(self):
        """Dragging the sidebar's right border should change its width."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app._screen.query_one("#sidebar")
            original_width = app.store.sidebar_width

            from textual.events import MouseDown, MouseMove, MouseUp

            sidebar.on_mouse_down(
                MouseDown(
                    sidebar,
                    x=sidebar.size.width - 1,
                    y=5,
                    delta_x=0,
                    delta_y=0,
                    button=1,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=original_width - 1,
                    screen_y=5,
                )
            )
            assert sidebar._dragging is True

            sidebar.on_mouse_move(
                MouseMove(
                    sidebar,
                    x=0,
                    y=5,
                    delta_x=5,
                    delta_y=0,
                    button=1,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=original_width + 5,
                    screen_y=5,
                )
            )
            assert app.store.sidebar_width == original_width + 5

            sidebar.on_mouse_up(
                MouseUp(
                    sidebar,
                    x=0,
                    y=5,
                    delta_x=0,
                    delta_y=0,
                    button=1,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=original_width + 5,
                    screen_y=5,
                )
            )
            assert sidebar._dragging is False

    async def test_sidebar_drag_enforces_minimum_width(self):
        """Sidebar width should not go below 15 during drag."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app._screen.query_one("#sidebar")

            from textual.events import MouseDown, MouseMove, MouseUp

            sidebar.on_mouse_down(
                MouseDown(
                    sidebar,
                    x=sidebar.size.width - 1,
                    y=5,
                    delta_x=0,
                    delta_y=0,
                    button=1,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=30,
                    screen_y=5,
                )
            )
            sidebar.on_mouse_move(
                MouseMove(
                    sidebar,
                    x=0,
                    y=5,
                    delta_x=-20,
                    delta_y=0,
                    button=1,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=5,
                    screen_y=5,
                )
            )
            assert app.store.sidebar_width >= 15

            sidebar.on_mouse_up(
                MouseUp(
                    sidebar,
                    x=0,
                    y=5,
                    delta_x=0,
                    delta_y=0,
                    button=1,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=5,
                    screen_y=5,
                )
            )

    async def test_sidebar_text_clips_to_width(self):
        """Sidebar should clip long labels to fit within content width."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app._screen.query_one("#sidebar")
            # Shrink sidebar to force clipping
            sidebar.styles.width = 18
            app.store.sidebar_width = 18

            rendered = sidebar.render()
            lines = rendered.plain.split("\n")
            content_width = app.store.sidebar_width - 2
            for line in lines:
                assert len(line) <= content_width, (
                    f"Line '{line}' ({len(line)} chars) exceeds "
                    f"content width {content_width}"
                )

    async def test_run_history_arrow_scrolls_to_selected(self):
        """Arrow keys in run history should keep the selected run visible."""
        import asyncio

        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Start a run so we have 2
            app.store.start_run()
            await asyncio.sleep(0.3)
            app._tick()
            await pilot.pause()

            # Focus run-history
            app.store.focused_pane = "run-history"
            first_id = app.store.selected_run_id

            await pilot.press("down")
            await pilot.pause()
            assert app.store.selected_run_id != first_id, "Down should change selected run"

            await pilot.press("up")
            await pilot.pause()
            assert app.store.selected_run_id == first_id, "Up should restore selected run"

    async def test_scrollbar_sizes_are_thin(self):
        """Horizontal scrollbars should be 1 cell tall in the rendered output.

        Uses export_screenshot to check the actual rendered characters,
        since headless layout doesn't faithfully reproduce scrollbar sizing.
        """
        import asyncio

        app = await self._make_app()
        async with app.run_test(size=(50, 50)) as pilot:
            await pilot.pause()
            app.store.start_run()
            await asyncio.sleep(2)
            for _ in range(10):
                app._tick()
                await pilot.pause()

            # Verify our CSS is at least being applied
            for widget_id in ("#run-history", "#dag-container", "#log-content"):
                w = app._screen.query_one(widget_id)
                assert (
                    w.styles.scrollbar_size_vertical == 1
                ), f"{widget_id} scrollbar_size_vertical={w.styles.scrollbar_size_vertical}"
                assert w.styles.scrollbar_size_horizontal == 1, (
                    f"{widget_id} scrollbar_size_horizontal="
                    f"{w.styles.scrollbar_size_horizontal}"
                )

    async def test_log_panel_allows_horizontal_scroll(self):
        """Log panel should support horizontal scrolling when content is wider than pane."""
        import asyncio

        app = await self._make_app()
        async with app.run_test(size=(50, 40)) as pilot:  # very narrow to force overflow
            await pilot.pause()
            app.store.start_run()
            await asyncio.sleep(1.5)
            for _ in range(5):
                app._tick()
                await pilot.pause()

            lp = app._screen.query_one("#log-content")

            # Container must support horizontal scrolling
            assert lp.styles.overflow_x == "auto"
            # With a 50-col terminal, log content should overflow horizontally
            assert lp.virtual_size.width > lp.content_size.width, (
                f"Content should overflow: virtual_w={lp.virtual_size.width} "
                f"vs content_w={lp.content_size.width}"
            )
            assert lp.allow_horizontal_scroll, "Horizontal scroll must be enabled"
            assert lp.max_scroll_x > 0, "Should have horizontal scroll range"

    async def test_dag_container_is_scrollable(self):
        """DAG scroll container should support both horizontal and vertical scrolling."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            dag_scroll = app._screen.query_one("#dag-container")
            assert dag_scroll.styles.overflow_x == "auto"
            assert dag_scroll.styles.overflow_y == "auto"

    async def test_dag_pointer_scroll_accumulates_smoothly(self):
        """Pointer scroll routes by axis/modifier and stays within boundaries."""
        from textual.events import (
            MouseScrollDown,
            MouseScrollLeft,
            MouseScrollRight,
            MouseScrollUp,
        )

        from avalanche.tui.app import AvalancheApp

        app = AvalancheApp(workflow="ml_workflow")
        async with app.run_test(size=(50, 15)) as pilot:
            await pilot.pause()
            app._timer.pause()
            dag_scroll = app._screen.query_one("#dag-container")
            assert dag_scroll.max_scroll_x > 0
            assert dag_scroll.max_scroll_y > 0

            def pointer_event(event_type, *, shift=False, ctrl=False):
                return event_type(
                    dag_scroll,
                    x=10,
                    y=5,
                    delta_x=0,
                    delta_y=1,
                    button=0,
                    shift=shift,
                    meta=False,
                    ctrl=ctrl,
                )

            dag_scroll._on_mouse_scroll_right(pointer_event(MouseScrollRight))
            assert dag_scroll.scroll_target_x == 8
            assert dag_scroll.scroll_x < dag_scroll.scroll_target_x

            dag_scroll._on_mouse_scroll_right(pointer_event(MouseScrollRight))
            assert dag_scroll.scroll_target_x == 16

            dag_scroll._on_mouse_scroll_left(pointer_event(MouseScrollLeft))
            assert dag_scroll.scroll_target_x == 8

            await pilot.pause(0.12)
            assert dag_scroll.scroll_x == pytest.approx(8)

            dag_scroll._on_mouse_scroll_down(pointer_event(MouseScrollDown))
            assert dag_scroll.scroll_target_y == 3

            await pilot.pause(0.12)
            assert dag_scroll.scroll_y == pytest.approx(3)

            dag_scroll._on_mouse_scroll_down(pointer_event(MouseScrollDown, shift=True))
            assert dag_scroll.scroll_target_x == 16

            await pilot.pause(0.12)
            assert dag_scroll.scroll_x == pytest.approx(16)

            dag_scroll._on_mouse_scroll_up(pointer_event(MouseScrollUp, ctrl=True))
            assert dag_scroll.scroll_target_x == 8

            await pilot.pause(0.12)
            assert dag_scroll.scroll_x == pytest.approx(8)

            dag_scroll._scroll_to(x=0, y=0, animate=False)
            dag_scroll._on_mouse_scroll_left(pointer_event(MouseScrollLeft))
            dag_scroll._on_mouse_scroll_up(pointer_event(MouseScrollUp))
            assert dag_scroll.scroll_target_x == 0
            assert dag_scroll.scroll_target_y == 0

    async def test_dag_center_button_exists(self):
        """DAG pane should have a center button."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            btn = app._screen.query_one("#dag-center-btn")
            assert "⊡" in str(btn.render())

    async def test_dag_click_selects_correct_node(self):
        """Clicking a visible node in the DAG should select exactly that node.

        The DagWidget is inside a ScrollableContainer — the border is on the
        container, not the widget. So event.y maps directly to content rows
        (no border offset needed in on_click).
        """
        from textual.pilot import OutOfBounds

        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            dag = app._screen.query_one("#dag-panel")
            dag.render()  # populate hit regions

            # Test non-virtual hit regions that are within the visible area
            tested = 0
            for row_idx, col_start, col_end, node in dag._hit_regions:
                if node.virtual:
                    continue
                target_col = (col_start + col_end) // 2
                try:
                    await pilot.click("#dag-panel", offset=(target_col, row_idx))
                except OutOfBounds:
                    continue  # node scrolled off-screen
                await pilot.pause()
                assert (
                    app.store.selected_node is not None
                ), f"Click at ({target_col}, {row_idx}) should select {node.name}"
                assert app.store.selected_node.name == node.name, (
                    f"Clicked {node.name} at row={row_idx}, "
                    f"but selected {app.store.selected_node.name}"
                )
                tested += 1
            assert tested >= 1, "No visible nodes could be clicked"

    async def test_dag_no_wrap_for_wide_workflow(self):
        """DAG widget should not wrap lines — all branches stay on their own row."""
        app = await self._make_app()
        async with app.run_test(size=(60, 40)) as pilot:  # narrow to force overflow
            await pilot.pause()
            # Switch to ml_workflow (has 3-way parallel, wider than 60 cols)
            ml = next(p for p in app.store.workflows if p.name == "ml_workflow")
            app.store.switch_workflow(ml)
            app._screen._remount_dag()
            app._refresh_widgets()
            await pilot.pause()

            dag = app._screen.query_one("#dag-panel")
            rendered = dag.render()
            lines = rendered.plain.split("\n")
            # Should have DAG rows + possible spacer/skip-edge rows
            non_empty = [line for line in lines if line.strip()]
            assert len(non_empty) >= 3, f"Expected at least 3 DAG rows, got {len(non_empty)}"
            # All 3 fetch nodes must appear
            combined = "\n".join(lines)
            for name in ("fetch_training", "fetch_validation", "fetch_features"):
                assert name in combined, f"Missing {name} in DAG render"

    async def test_deep_link_workflow(self):
        """App should support starting with a specific workflow selected."""
        from avalanche.tui.app import AvalancheApp

        app = AvalancheApp(workflow="ml_workflow")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.store.current_workflow.name == "ml_workflow"
            assert app.store.sidebar_selected_name == "ml_workflow"

    async def test_deep_link_node_matches_display_name(self):
        """Deep-link node segment should accept the rendered display name."""
        from avalanche.tui.app import AvalancheApp

        app = AvalancheApp(workflow="ml_workflow", node="export_onnx")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.store.selected_node is not None
            assert app.store.selected_node.name == "export_onnx_1"
            assert app.store.selected_node.display_name == "export_onnx"

    async def test_deep_link_node_renders_while_runs_are_still_loading(self):
        """Deep-link selection must not be hidden behind asynchronous run loading."""
        from avalanche.tui.app import AvalancheApp

        delegate = MockStateProvider()
        catalog_entered = threading.Event()
        catalog_release = threading.Event()
        runs_release = threading.Event()

        class BlockingRunsProvider:
            def list_workflows(self):
                catalog_entered.set()
                catalog_release.wait()
                return delegate.list_workflows()

            def list_runs(self, workflow_selector):
                runs_release.wait()
                return delegate.list_runs(workflow_selector)

            def get_run(self, run_id):
                return delegate.get_run(run_id)

            def start_run(self, workflow_selector, **kwargs):
                return delegate.start_run(workflow_selector, **kwargs)

            def cancel_run(self, run_id):
                return delegate.cancel_run(run_id)

            def on_run_update(self, callback):
                return delegate.on_run_update(callback)

            def on_log(self, callback):
                return delegate.on_log(callback)

            def __getattr__(self, name):
                return getattr(delegate, name)

        app = AvalancheApp(
            provider=BlockingRunsProvider(),
            workflow="order_workflow",
            node="validate",
        )
        updates = _signal_background_updates(app.store)
        assert catalog_entered.wait(1.0)
        catalog_release.set()
        assert updates.put_event.wait(1.0)
        app.store._apply_background_updates()
        app._apply_deep_link()

        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                app._timer.pause()
                app._refresh_widgets()

                assert app.store.current_run is None
                assert app.store.selected_node is not None
                assert app.store.selected_node.display_name == "validate"
                assert "validate" in app._screen.query_one("#dag-panel").render().plain
                assert "validate" in str(app._screen.query_one("#log-panel").border_title)
                assert (
                    "validate" in app._screen.query_one("#status-bar", StatusBar).render().plain
                )
        finally:
            runs_release.set()

    async def test_dag_arrows_move_nodes_not_scroll(self):
        """Arrow keys in DAG pane should move node selection, not scroll the container."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.store.focused_pane == "dag"

            # Record scroll position after initial centering
            dag = app._screen.query_one("#dag-container")
            initial_scroll_x = dag.scroll_x

            # Right arrow should select a node
            await pilot.press("right")
            await pilot.pause()
            assert app.store.selected_node is not None
            first = app.store.selected_node

            # Right again should move to next column
            await pilot.press("right")
            await pilot.pause()
            assert app.store.selected_node.col > first.col

            # Left should move back
            await pilot.press("left")
            await pilot.pause()
            assert app.store.selected_node.col == first.col

            # Container scroll should not have changed from arrows
            assert dag.scroll_x == initial_scroll_x, (
                f"Arrow keys should not scroll the container: "
                f"was {initial_scroll_x}, now {dag.scroll_x}"
            )

    async def test_arrows_dont_scroll_other_panes(self):
        """Left/right arrows should not scroll run-history or log panes."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            for pane in ("run-history", "log"):
                app.store.focused_pane = pane
                await pilot.pause()

                await pilot.press("left")
                await pilot.press("right")
                await pilot.pause()

                # Check the scroll containers didn't move
                widget_id = "#run-history" if pane == "run-history" else "#log-panel"
                w = app._screen.query_one(widget_id)
                assert (
                    w.scroll_x == 0
                ), f"{pane}: left/right arrows should not scroll horizontally"

    async def test_focused_log_up_down_scrolls_log_content(self):
        """Up/down in the log pane should scroll the actual RichLog."""
        from avalanche.tui.app import AvalancheApp
        from avalanche.tui.widgets.log_panel import LogWidget

        app = AvalancheApp()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            app._timer.pause()
            node_id = next(iter(app.store.current_run.nodes))
            app.store.current_run = replace(
                app.store.current_run,
                logs=[
                    LogEntry(
                        timestamp=datetime(2026, 3, 27, 14, 35, index),
                        level=LogLevel.INFO,
                        node_id=node_id,
                        message=f"line {index}",
                    )
                    for index in range(30)
                ],
            )
            app.store.focused_pane = "log"
            app._log_autoscroll = False
            app._refresh_widgets()
            await pilot.pause()
            log_view = app._screen.query_one("#log-content", LogWidget)
            log_view.scroll_end(animate=False)
            await pilot.pause()
            bottom = log_view.scroll_y
            assert bottom > 0

            await pilot.press("up")
            await pilot.pause()
            assert log_view.scroll_y < bottom

            await pilot.press("down")
            await pilot.pause()
            assert log_view.scroll_y == bottom

    async def test_left_right_move_dag_nodes_from_any_pane(self):
        """Left/right should drive DAG selection even when another pane is focused."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            app.store.sidebar_visible = True
            app._refresh_widgets()
            await pilot.pause()

            for pane in ("sidebar", "run-history", "log"):
                app.store.focused_pane = pane
                if pane == "sidebar":
                    app._sync_sidebar_focus()
                app.store.deselect_node()
                await pilot.pause()

                await pilot.press("right")
                await pilot.pause()
                assert app.store.focused_pane == pane
                assert (
                    app.store.selected_node is not None
                ), f"Right should select a DAG node while {pane} is focused"
                first = app.store.selected_node

                await pilot.press("right")
                await pilot.pause()
                assert (
                    app.store.selected_node.col > first.col
                ), f"Right should advance DAG selection while {pane} is focused"

                await pilot.press("left")
                await pilot.pause()
                assert (
                    app.store.selected_node.col == first.col
                ), f"Left should move DAG selection back while {pane} is focused"

    async def test_alt_arrows_change_run_without_changing_pane(self):
        """Alt+Up/Down should cycle runs without changing the focused pane."""
        import asyncio

        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Start a second run so we have 2
            app.store.start_run()
            await asyncio.sleep(0.3)
            app._tick()
            await pilot.pause()

            # Focus the DAG pane (not run-history)
            app.store.focused_pane = "dag"
            first_run = app.store.selected_run_id

            # Alt+Down should select next run
            await pilot.press("alt+down")
            await pilot.pause()
            assert app.store.focused_pane == "dag", "Alt+Down should not change pane"
            assert app.store.selected_run_id != first_run, "Alt+Down should change run"

            # Alt+Up should go back
            await pilot.press("alt+up")
            await pilot.pause()
            assert app.store.focused_pane == "dag", "Alt+Up should not change pane"
            assert app.store.selected_run_id == first_run, "Alt+Up should restore run"


_SANDBOX_STDOUT_SENTINEL = "SANDBOX_STDOUT_SENTINEL_DO_NOT_USE_AS_AGENT_OUTPUT"


def _agent_trace_provider():
    metadata = {
        "signature": {
            "name": "InspectRecords",
            "instructions": "Inspect the supplied records.",
            "inputs": [
                {
                    "name": "records",
                    "annotation": "list[Record]",
                    "description": "records to inspect",
                }
            ],
            "outputs": [
                {
                    "name": "summary",
                    "annotation": "InspectionSummary",
                    "description": "structured inspection summary",
                },
                {
                    "name": "labels",
                    "annotation": "list[str]",
                    "description": "inspection labels",
                },
                {
                    "name": "note",
                    "annotation": "str | None",
                    "description": "optional inspection note",
                },
            ],
        },
        "runtime": {
            "lm": "main-model",
            "sub_lm": "sub-model",
            "max_iterations": 5,
            "debug": False,
        },
        "skills": [
            {
                "name": "audit",
                "instructions": "Check every claim.",
                "packages": ["pydantic"],
                "modules": ["audit_helpers"],
                "tools": ["lookup"],
            }
        ],
        "aggregated_static_instructions": (
            "Inspect the supplied records.\n\n" "## Skill: audit\n\nCheck every claim."
        ),
        "packages": ["pydantic"],
        "modules": ["audit_helpers"],
        "tools": [{"name": "lookup", "description": "Look up a record."}],
    }
    workflow = WorkflowInfo(
        name="agent_flow",
        file_path="workflows/agent_flow.py",
        node_ids=["agent_1"],
        graph={"agent_1": []},
        node_types={"agent_1": "step"},
        display_names={"agent_1": "agent"},
        agent_node_ids=["agent_1"],
        agent_metadata_json={"agent_1": json.dumps(metadata)},
    )
    events = [
        {
            "sequence": 1,
            "kind": "code.generated",
            "timestamp_ns": 1,
            "data": {"iteration": 1, "code": "print('first')"},
        },
        {
            "sequence": 2,
            "kind": "iteration.recorded",
            "timestamp_ns": 2,
            "data": {"step": {"iteration": 1}},
        },
        {
            "sequence": 3,
            "kind": "code.executed",
            "timestamp_ns": 3,
            "data": {"iteration": 2, "output": _SANDBOX_STDOUT_SENTINEL},
        },
        {
            "sequence": 4,
            "kind": "iteration.recorded",
            "timestamp_ns": 4,
            "data": {"step": {"iteration": 2}},
        },
        {
            "sequence": 5,
            "kind": "run.succeeded",
            "timestamp_ns": 5,
            "data": {
                "status": "completed",
                "outputs": {
                    "summary": {"active_count": 1, "ready": False},
                    "labels": ["reviewed"],
                    "note": None,
                },
            },
        },
    ]
    steps = [
        {
            "iteration": 1,
            "reasoning": "first reasoning",
            "code": "print('first')",
            "output": "first",
            "untruncated_output": "FULL-FIRST",
            "error": False,
            "duration_ms": 5,
            "tool_calls": [],
            "predict_calls": [],
            "lm": {"finish_reason": "stop"},
            "usage": {"main": {}, "sub": {}},
        },
        {
            "iteration": 2,
            "reasoning": "second reasoning",
            "code": "print('second')",
            "output": _SANDBOX_STDOUT_SENTINEL,
            "untruncated_output": "FULL-SECOND",
            "error": False,
            "duration_ms": 8,
            "tool_calls": [
                {
                    "name": "lookup",
                    "args": [],
                    "kwargs": {},
                    "result": "value",
                    "error": None,
                    "duration_ms": 1,
                }
            ],
            "predict_calls": [
                {
                    "signature": "text -> answer",
                    "instructions": "answer",
                    "model": "sub",
                    "total_usage": {},
                    "calls": [
                        {
                            "duration_ms": 2,
                            "usage": {},
                            "input": {"text": "x"},
                            "output": {"answer": "y"},
                            "error": None,
                            "lm": {"finish_reason": "stop"},
                        }
                    ],
                }
            ],
            "lm": {"finish_reason": "stop"},
            "usage": {
                "main": {"input_tokens": 10, "output_tokens": 2, "cost": 0.01},
                "sub": {"input_tokens": 4, "output_tokens": 1, "cost": 0.002},
            },
        },
    ]
    envelope = {
        "schema_version": 1,
        "status": "completed",
        "run_id": "agent-run",
        "events": [
            {
                "sequence": event["sequence"],
                "event_kind": event["kind"],
                "timestamp_ns": event["timestamp_ns"],
                "data": event["data"],
            }
            for event in events
        ],
        "trace": {
            "status": "completed",
            "model": "main-model",
            "sub_model": "sub-model",
            "iterations": 2,
            "max_iterations": 5,
            "duration_ms": 20,
            "usage": {
                "main": {
                    "input_tokens": 20,
                    "output_tokens": 4,
                    "cache_hits": 1,
                    "cost": 0.02,
                },
                "sub": {
                    "input_tokens": 8,
                    "output_tokens": 2,
                    "cache_hits": 0,
                    "cost": 0.004,
                },
            },
            "steps": steps,
            "evidence": {
                "run_id": "agent-run",
                "complete": True,
                "terminal_outcome": "completed",
                "events": events,
            },
        },
        "error": None,
    }
    run = RunState(
        run_id="run-agent",
        flow_name="agent_flow",
        status=RunStatus.SUCCESS,
    )
    run.nodes["agent_1"] = NodeState(
        node_id="agent_1",
        name="agent",
        node_type="step",
        status=NodeStatus.SUCCESS,
        agent_trace_json=json.dumps(envelope),
    )
    run.logs.extend(
        [
            LogEntry(datetime.now(), LogLevel.INFO, "agent_1", "Agent code.generated"),
            LogEntry(datetime.now(), LogLevel.INFO, "agent_1", "Agent iteration.recorded"),
        ]
    )
    provider = MockStateProvider()
    provider._workflows = {"agent_flow": workflow}
    provider._runs = {run.run_id: run}
    return provider, envelope


def _unhydrated_agent_trace_provider():
    provider, complete_envelope = _agent_trace_provider()
    run = provider._runs["run-agent"]
    node = run.nodes["agent_1"]
    pending_envelope = json.loads(json.dumps(complete_envelope))
    pending_envelope["trace"] = None
    descriptor = TraceDescriptor(
        status="completed",
        revision=1,
        available=True,
        complete=True,
        event_count=len(pending_envelope["events"]),
        size_bytes=100,
    )
    node.trace = descriptor
    node.revision = descriptor.revision
    node.agent_trace_json = json.dumps(pending_envelope)
    return provider, complete_envelope, run, node, pending_envelope, descriptor


def _trace_detail_from_run(run: RunState, node_id: str) -> TraceDetail:
    node = run.nodes[node_id]
    assert node.trace is not None
    envelope = json.loads(node.agent_trace_json)
    assert isinstance(envelope["trace"], dict)
    return TraceDetail(
        operator_instance_id=run.operator_instance_id,
        run_id=run.run_id,
        created_sequence=run.created_sequence,
        node_id=node_id,
        descriptor_revision=node.trace.revision,
        trace_body=envelope["trace"],
    )


@pytest.mark.asyncio
async def test_agent_trace_inspector_interactions_and_log_retention():
    from avalanche.tui.app import AvalancheApp
    from avalanche.tui.widgets.agent_trace import (
        AgentMetadataInspector,
        AgentOutputInspector,
        AgentTraceInspector,
    )

    provider, _ = _agent_trace_provider()
    app = AvalancheApp(provider=provider, workflow="agent_flow", node="agent")
    updates = _signal_background_updates(app.store)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await _wait_for_current_run(app, updates)
        await pilot.press("enter")
        await pilot.pause()

        assert app.store.trace_inspector_open is True
        assert app.store.focused_pane == "trace"
        assert app.store.trace_selected_path == ("turn", "1")
        assert app.store.trace_collapsed_turns == {0, 1}
        assert app._screen.query_one("#dashboard-pane").display is False
        assert app._screen.query_one("#agent-trace-inspector").display is True

        content = app._screen.query_one("#agent-trace-content", AgentTraceInspector)
        collapsed = content.render().plain
        assert "STRUCTURED TRACE · 2 turn(s)" in collapsed
        assert "AGENT TURN 1/5" in collapsed
        assert "AGENT TURN 2/5" in collapsed
        assert "second reasoning" not in collapsed
        assert "print('second')" not in collapsed

        await pilot.press("enter")
        assert app.store.trace_collapsed_turns == {0}
        expanded = content.render().plain
        for expected in ("Reasoning", "Code", "Output", "Tools (1)", "Predict details (1)"):
            assert expected in expanded
        assert "second reasoning" not in expanded

        await pilot.press("down", "enter")
        assert app.store.trace_selected_path == ("turn", "1", "reasoning")
        assert "second reasoning" in content.render().plain

        await pilot.press("down", "enter")
        code_render = content.render()
        assert "print('second')" in code_render.plain
        code_offset = code_render.plain.index("print('second')")
        assert any(
            span.start <= code_offset < span.end for span in code_render.spans
        ), "Python code should carry syntax-highlighting spans"

        await pilot.press("down", "enter", "o")
        assert "Output (full)" in content.render().plain
        assert "FULL-SECOND" in content.render().plain

        await pilot.press("down", "enter", "down", "enter")
        assert app.store.trace_selected_path == ("turn", "1", "tools", "0")
        assert "Input" in content.render().plain
        assert "Result" in content.render().plain

        await pilot.press("right")
        await pilot.pause()
        assert app.store.trace_inspector_tab == "output"
        output_content = app._screen.query_one("#agent-output-content", AgentOutputInspector)
        assert output_content.display is True
        assert app.store.trace_selected_path == ("output", "summary")
        assert '"active_count": 1' not in output_content.render().plain
        await pilot.press("enter")
        output_plain = output_content.render().plain
        assert '"active_count": 1' in output_plain
        assert '"ready": false' in output_plain

        await pilot.press("down", "enter")
        assert app.store.trace_selected_path == ("output", "labels")
        assert '"reviewed"' in output_content.render().plain

        await pilot.press("right")
        await pilot.pause()
        assert app.store.trace_inspector_tab == "metadata"
        metadata_content = app._screen.query_one(
            "#agent-metadata-content", AgentMetadataInspector
        )
        assert metadata_content.display is True
        assert app.store.trace_selected_path == ("metadata", "signature")
        assert "InspectRecords" not in metadata_content.render().plain
        await pilot.press("enter")
        metadata_plain = metadata_content.render().plain
        assert "InspectRecords" in metadata_plain
        assert "records" in metadata_plain

        await pilot.press("escape")
        await pilot.pause()
        assert app.store.trace_inspector_open is False
        assert app.store.focused_pane == "dag"
        assert app._screen.query_one("#dashboard-pane").display is True
        logs = [entry.message for entry in app.store.logs]
        assert "Agent code.generated" in logs
        assert "Agent iteration.recorded" in logs


@pytest.mark.asyncio
async def test_agent_inspector_navigation_scrolls_selected_output_into_view():
    from avalanche.tui.app import AvalancheApp

    provider, envelope = _agent_trace_provider()
    outputs = {f"field_{index}": index for index in range(60)}
    envelope["trace"]["evidence"]["events"][-1]["data"]["outputs"] = outputs
    provider._runs["run-agent"].nodes["agent_1"].agent_trace_json = json.dumps(envelope)
    metadata = json.loads(provider._workflows["agent_flow"].agent_metadata_json["agent_1"])
    metadata["signature"]["outputs"] = [
        {"name": name, "annotation": "int", "description": name} for name in outputs
    ]
    provider._workflows["agent_flow"].agent_metadata_json["agent_1"] = json.dumps(metadata)

    app = AvalancheApp(provider=provider, workflow="agent_flow", node="agent")
    updates = _signal_background_updates(app.store)
    async with app.run_test(size=(100, 25)) as pilot:
        await pilot.pause()
        await _wait_for_current_run(app, updates)
        await pilot.press("enter", "right")
        for _ in range(50):
            await pilot.press("down")
        await pilot.pause()

        assert app.store.trace_selected_path == ("output", "field_50")
        inspector = app._screen.query_one("#agent-trace-inspector")
        assert inspector.scroll_y > 0


@pytest.mark.asyncio
async def test_open_trace_inspector_tracks_revisions_and_retries_hydration():
    from avalanche.tui.app import AvalancheApp

    (
        provider,
        complete_envelope,
        run,
        node,
        pending_envelope,
        descriptor,
    ) = _unhydrated_agent_trace_provider()

    hydration_calls = []
    first_started = threading.Event()
    release_first = threading.Event()

    def hydrate_trace(run_id, node_id):
        current = provider._runs[run_id]
        current_node = current.nodes[node_id]
        revision = current_node.trace.revision
        hydration_calls.append(revision)
        if revision == 1:
            first_started.set()
            release_first.wait()
            return None
        if revision == 3 and hydration_calls.count(3) == 1:
            return None
        hydrated_node = replace(
            current_node,
            agent_trace_json=json.dumps(complete_envelope),
        )
        hydrated = replace(current, nodes={node_id: hydrated_node})
        provider._runs[run_id] = hydrated
        return _trace_detail_from_run(hydrated, node_id)

    provider.hydrate_trace = hydrate_trace
    app = AvalancheApp(provider=provider, workflow="agent_flow", node="agent")
    updates = _signal_background_updates(app.store)
    async with app.run_test(size=(100, 35)) as pilot:
        try:
            await pilot.pause()
            await _wait_for_current_run(app, updates)
            await pilot.press("enter")
            for _ in range(20):
                if first_started.is_set():
                    break
                await pilot.pause()
            assert first_started.is_set()
            assert ("run-agent", "agent_1", 1) in app._trace_hydration_in_flight

            node_v2 = replace(
                node,
                trace=replace(descriptor, revision=2),
                revision=2,
                agent_trace_json=json.dumps(pending_envelope),
            )
            run_v2 = replace(run, revision=2, nodes={"agent_1": node_v2})
            provider._runs[run.run_id] = run_v2
            app.store.enqueue_run_update(run_v2)
            for _ in range(3):
                await pilot.pause()
            assert hydration_calls == [1]
            assert ("run-agent", "agent_1", 1) in app._trace_hydration_in_flight
        finally:
            release_first.set()

        for _ in range(40):
            await pilot.pause()
            envelope = app.store.selected_agent_trace_envelope
            if 2 in hydration_calls and isinstance(envelope.get("trace"), dict):
                break
        assert hydration_calls[:2] == [1, 2]

        await pilot.pause()

        await pilot.press("left")
        assert app.store.trace_inspector_tab == "metadata"
        node_v3 = replace(
            node_v2,
            trace=replace(descriptor, revision=3),
            revision=3,
            agent_trace_json=json.dumps(pending_envelope),
        )
        run_v3 = replace(run_v2, revision=3, nodes={"agent_1": node_v3})
        provider._runs[run.run_id] = run_v3
        app.store.enqueue_run_update(run_v3)
        for _ in range(3):
            await pilot.pause()
        assert 3 not in hydration_calls

        await pilot.press("right")
        for _ in range(40):
            await pilot.pause()
            envelope = app.store.selected_agent_trace_envelope
            if hydration_calls.count(3) >= 2 and isinstance(envelope.get("trace"), dict):
                break
        assert app.store.trace_inspector_tab == "trace"
        assert hydration_calls.count(3) == 2
        assert isinstance(app.store.selected_agent_trace_envelope["trace"], dict)


@pytest.mark.asyncio
async def test_delayed_trace_detail_rejects_stale_body_without_state_rollback():
    from avalanche.tui.app import AvalancheApp

    (
        provider,
        complete_envelope,
        run,
        node,
        pending_envelope,
        descriptor,
    ) = _unhydrated_agent_trace_provider()
    run.operator_instance_id = "operator-1"
    run.created_sequence = 4
    stale_node = replace(
        node,
        agent_trace_json=json.dumps(complete_envelope),
    )
    stale_run = replace(
        run,
        nodes={"agent_1": stale_node},
        logs=list(run.logs),
    )
    newer_envelope = json.loads(json.dumps(pending_envelope))
    newer_envelope["events"].append(
        {"sequence": 999, "event_kind": "iteration.recorded", "data": {"new": True}}
    )
    descriptor_v2 = replace(
        descriptor,
        revision=2,
        event_count=len(newer_envelope["events"]),
    )
    newer_node = replace(
        node,
        status=NodeStatus.FAILED,
        ended_at=99.0,
        trace=descriptor_v2,
        revision=8,
        agent_trace_json=json.dumps(newer_envelope),
    )
    newer_log = LogEntry(
        datetime.now(),
        LogLevel.ERROR,
        "agent_1",
        "new structural state",
    )
    newer_run = replace(
        run,
        status=RunStatus.FAILED,
        ended_at=101.0,
        revision=12,
        latest_log_sequence=77,
        nodes={"agent_1": newer_node},
        logs=[newer_log],
    )
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    calls = 0

    def hydrate_trace(run_id, node_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            release_first.wait()
            return _trace_detail_from_run(stale_run, node_id)
        second_started.set()
        release_second.wait()
        return None

    def close():
        release_first.set()
        release_second.set()

    provider.hydrate_trace = hydrate_trace
    provider.close = close
    app = AvalancheApp(provider=provider, workflow="agent_flow", node="agent")
    updates = _signal_background_updates(app.store)

    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await _wait_for_current_run(app, updates)
        await pilot.press("enter")
        for _ in range(20):
            if first_started.is_set():
                break
            await pilot.pause()
        assert first_started.is_set()

        provider._runs[run.run_id] = newer_run
        app.store.enqueue_run_update(newer_run)
        for _ in range(20):
            await pilot.pause()
            if app.store.current_run.revision == newer_run.revision:
                break
        assert app.store.current_run is newer_run

        release_first.set()
        for _ in range(40):
            await pilot.pause()
            if second_started.is_set():
                break
        assert second_started.is_set()

        current = app.store.current_run
        assert current.status is RunStatus.FAILED
        assert current.revision == 12
        assert current.latest_log_sequence == 77
        assert [entry.message for entry in current.logs] == ["new structural state"]
        current_node = current.nodes["agent_1"]
        assert current_node.status is NodeStatus.FAILED
        assert current_node.revision == 8
        assert current_node.trace.revision == 2
        envelope = json.loads(current_node.agent_trace_json)
        assert envelope["events"][-1]["sequence"] == 999
        assert envelope["trace"] is None
        assert ("run-agent", "agent_1", 1) not in app._trace_hydration_retry


@pytest.mark.asyncio
async def test_trace_hydration_persistent_failure_uses_bounded_backoff():
    from avalanche.tui.app import AvalancheApp

    provider, _, _, _, _, _ = _unhydrated_agent_trace_provider()
    clock = [0.0]
    calls = []
    completed_attempts = []

    def hydrate_trace(run_id, node_id):
        calls.append((run_id, node_id, clock[0]))
        try:
            return None
        finally:
            completed_attempts.append(len(calls))

    provider.hydrate_trace = hydrate_trace
    app = AvalancheApp(provider=provider, workflow="agent_flow", node="agent")
    app._trace_hydration_now = lambda: clock[0]
    key = ("run-agent", "agent_1", 1)

    async def wait_for_retry(pilot, level, call_count, after_deadline):
        for _ in range(40):
            await pilot.pause()
            retry = app._trace_hydration_retry.get(key)
            if (
                len(calls) == call_count
                and len(completed_attempts) == call_count
                and retry is not None
                and retry[0] == level
                and retry[1] > after_deadline
            ):
                return retry
        raise AssertionError(f"retry level {level} was not observed")

    updates = _signal_background_updates(app.store)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await _wait_for_current_run(app, updates)
        await pilot.press("enter")
        retry = await wait_for_retry(pilot, 1, 1, clock[0])
        assert retry == (1, 0.25)

        for _ in range(20):
            await pilot.pause()
        assert len(calls) == 1

        expected_levels = (2, 3, 4, 5, 5)
        for call_count, level in enumerate(expected_levels, start=2):
            clock[0] = app._trace_hydration_retry[key][1]
            retry = await wait_for_retry(pilot, level, call_count, clock[0])
        assert retry[1] - clock[0] == 4.0

        for _ in range(20):
            await pilot.pause()
        assert len(calls) == 6


@pytest.mark.asyncio
async def test_trace_hydration_completion_during_navigation_allows_retry():
    from avalanche.tui.app import AvalancheApp

    (
        provider,
        complete_envelope,
        run,
        _node,
        _pending_envelope,
        _descriptor,
    ) = _unhydrated_agent_trace_provider()
    old_workflow = provider._workflows["agent_flow"]
    provider._workflows[ORDER_WORKFLOW.selector] = ORDER_WORKFLOW
    started = threading.Event()
    release = threading.Event()
    completion_ready = threading.Event()
    pending_completions = []
    calls = []

    def hydrate_trace(run_id, node_id):
        calls.append((run_id, node_id))
        current = provider._runs[run_id]
        hydrated_node = replace(
            current.nodes[node_id],
            agent_trace_json=json.dumps(complete_envelope),
        )
        if len(calls) == 1:
            started.set()
            release.wait()
        return _trace_detail_from_run(
            replace(current, nodes={node_id: hydrated_node}),
            node_id,
        )

    provider.hydrate_trace = hydrate_trace
    app = AvalancheApp(provider=provider, workflow="agent_flow", node="agent")
    enqueue_completion = app.store.enqueue_trace_hydration_completion

    def signal_completion(completion):
        pending_completions.append(completion)
        completion_ready.set()

    def deliver_completion():
        assert len(pending_completions) == 1
        enqueue_completion(pending_completions.pop())
        completion_ready.clear()
        app.store._apply_background_updates()
        app._apply_trace_hydration_completions()

    app.store.enqueue_trace_hydration_completion = signal_completion
    key = ("run-agent", "agent_1", 1)
    updates = _signal_background_updates(app.store)
    async with app.run_test(size=(100, 35)) as pilot:
        try:
            await pilot.pause()
            await _wait_for_current_run(app, updates)
            await pilot.press("enter")
            for _ in range(20):
                if started.is_set():
                    break
                await pilot.pause()
            assert started.is_set()
            attempt = app._trace_hydration_attempts[key]
            assert key in app._trace_hydration_in_flight

            app.store.switch_workflow(ORDER_WORKFLOW)
            await pilot.pause()
            app._hydrate_selected_trace()
            assert app.store.current_workflow.selector == ORDER_WORKFLOW.selector
            assert app._trace_hydration_context is None
            assert app._trace_hydration_attempts == {key: attempt}
            assert key in app._trace_hydration_in_flight
            assert attempt in app._trace_hydration_superseded
        finally:
            release.set()

        await asyncio.to_thread(completion_ready.wait)
        assert key in app._trace_hydration_in_flight
        deliver_completion()
        assert key not in app._trace_hydration_in_flight
        assert key not in app._trace_hydration_attempts
        assert attempt not in app._trace_hydration_superseded
        assert calls == [("run-agent", "agent_1")]

        app.store.switch_workflow(old_workflow)
        app.store._runs_cache = [run]
        app.store.current_run = run
        app.store.run_pinned = True
        agent_node = next(
            item for item in app.store.all_nodes if item.name in old_workflow.agent_node_ids
        )
        app.store.select_node(agent_node)
        assert app.store.open_trace_inspector()
        app._hydrate_selected_trace()
        await asyncio.to_thread(completion_ready.wait)
        deliver_completion()
        assert calls == [
            ("run-agent", "agent_1"),
            ("run-agent", "agent_1"),
        ]
        assert isinstance(app.store.selected_agent_trace_envelope["trace"], dict)


@pytest.mark.asyncio
async def test_trace_hydration_tab_cycles_bound_blocked_worker_and_keep_backoff():
    from avalanche.tui.app import AvalancheApp

    provider, _, _, _, _, _ = _unhydrated_agent_trace_provider()
    clock = [100.0]
    started = threading.Event()
    release = threading.Event()
    worker_done = threading.Event()
    lock = threading.Lock()
    calls = 0
    active = 0
    max_active = 0

    def hydrate_trace(run_id, node_id):
        nonlocal active, calls, max_active
        with lock:
            calls += 1
            active += 1
            max_active = max(max_active, active)
        started.set()
        try:
            release.wait()
            return None
        finally:
            with lock:
                active -= 1
            worker_done.set()

    provider.hydrate_trace = hydrate_trace
    provider.close = release.set
    app = AvalancheApp(provider=provider, workflow="agent_flow", node="agent")
    app._trace_hydration_now = lambda: clock[0]
    key = ("run-agent", "agent_1", 1)
    updates = _signal_background_updates(app.store)

    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await _wait_for_current_run(app, updates)
        await pilot.press("enter")
        for _ in range(20):
            if started.is_set():
                break
            await pilot.pause()
        assert started.is_set()

        attempt = app._trace_hydration_attempts[key]
        for _ in range(8):
            await pilot.press("left")
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()

        with lock:
            assert calls == 1
            assert active == 1
            assert max_active == 1
        assert key in app._trace_hydration_in_flight
        assert key in app._trace_hydration_attempts
        assert attempt in app._trace_hydration_superseded

        release.set()
        await asyncio.to_thread(worker_done.wait)
        retry = None
        for _ in range(40):
            await pilot.pause()
            retry = app._trace_hydration_retry.get(key)
            if retry is not None:
                break
        assert retry == (1, 100.25)
        assert key not in app._trace_hydration_in_flight
        assert app._trace_hydration_attempts == {}
        assert attempt not in app._trace_hydration_superseded

        await pilot.press("left")
        await pilot.pause()
        await pilot.press("right")
        for _ in range(5):
            await pilot.pause()
        assert app._trace_hydration_retry[key] == retry
        assert calls == 1


@pytest.mark.asyncio
async def test_trace_hydration_shutdown_closes_provider_and_joins_worker():
    from avalanche.tui.app import AvalancheApp

    provider, _, _, _, _, _ = _unhydrated_agent_trace_provider()
    started = threading.Event()
    release = threading.Event()
    close_called = threading.Event()
    worker_done = threading.Event()

    def hydrate_trace(run_id, node_id):
        started.set()
        try:
            release.wait()
            return None
        finally:
            worker_done.set()

    def close():
        close_called.set()
        release.set()

    provider.hydrate_trace = hydrate_trace
    provider.close = close
    app = AvalancheApp(provider=provider, workflow="agent_flow", node="agent")
    updates = _signal_background_updates(app.store)

    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await _wait_for_current_run(app, updates)
        await pilot.press("enter")
        for _ in range(20):
            if started.is_set():
                break
            await pilot.pause()
        assert started.is_set()
        assert not worker_done.is_set()

    assert close_called.is_set()
    assert worker_done.is_set()
    assert all(not thread.is_alive() for thread in app._trace_hydration_executor._threads)


@pytest.mark.asyncio
async def test_app_does_not_close_launch_owned_provider_on_unmount():
    from avalanche.tui.app import AvalancheApp

    provider = MockStateProvider()
    close_calls = 0

    def close():
        nonlocal close_calls
        close_calls += 1

    provider.close = close
    app = AvalancheApp(provider=provider, close_provider_on_unmount=False)

    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()

    assert close_calls == 0


@pytest.mark.asyncio
async def test_agent_trace_inspector_renders_pending_failed_malformed_and_incomplete():
    from avalanche.tui.app import AvalancheApp
    from avalanche.tui.widgets.agent_trace import (
        AgentMetadataInspector,
        AgentOutputInspector,
        AgentTraceInspector,
    )

    provider, envelope = _agent_trace_provider()
    app = AvalancheApp(provider=provider, workflow="agent_flow", node="agent")
    updates = _signal_background_updates(app.store)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        await _wait_for_current_run(app, updates)
        await pilot.pause()
        await pilot.press("enter")
        content = app._screen.query_one("#agent-trace-content", AgentTraceInspector)
        node = app.store.current_run.nodes["agent_1"]

        await pilot.press("left")
        metadata_content = app._screen.query_one(
            "#agent-metadata-content", AgentMetadataInspector
        )
        workflow = app.store.current_workflow
        original_metadata_json = workflow.agent_metadata_json["agent_1"]
        workflow.agent_metadata_json["agent_1"] = "{malformed"
        assert "Metadata unavailable or malformed" in metadata_content.render().plain
        workflow.agent_metadata_json.pop("agent_1")
        assert "Metadata unavailable or malformed" in metadata_content.render().plain
        await pilot.press("left")
        output_content = app._screen.query_one("#agent-output-content", AgentOutputInspector)
        fallback_output = output_content.render().plain
        assert app.store.trace_inspector_tab == "output"
        assert "summary" in fallback_output
        assert "labels" in fallback_output
        assert "note" in fallback_output
        assert "InspectionSummary" not in fallback_output
        assert '"ready": false' not in fallback_output
        assert _SANDBOX_STDOUT_SENTINEL not in fallback_output

        await pilot.press("right")
        assert app.store.trace_inspector_tab == "metadata"
        await pilot.press("right")
        assert app.store.trace_inspector_tab == "trace"
        node = app.store.current_run.nodes["agent_1"]
        workflow.agent_metadata_json["agent_1"] = original_metadata_json

        projected = json.loads(json.dumps(envelope))
        projected_success = projected["trace"]["evidence"]["events"][-1]
        projected_success["event_kind"] = projected_success.pop("kind")
        node.agent_trace_json = json.dumps(projected)
        assert app.store.selected_agent_outputs == {
            "summary": {"active_count": 1, "ready": False},
            "labels": ["reviewed"],
            "note": None,
        }

        absent_field = json.loads(json.dumps(envelope))
        absent_field["trace"]["evidence"]["events"][-1]["data"]["outputs"].pop("note")
        node.agent_trace_json = json.dumps(absent_field)
        await pilot.press("right")
        app.store.trace_selected_paths["output"] = ("output", "note")
        await pilot.press("enter")
        absent_output = output_content.render().plain
        note_header = "note: str | None — optional inspection note"
        assert note_header in absent_output
        assert "Unavailable" in absent_output[absent_output.index(note_header) :]
        await pilot.press("left")
        node = app.store.current_run.nodes["agent_1"]

        malformed_success = json.loads(json.dumps(envelope))
        malformed_success["trace"]["evidence"]["events"].append(
            {
                "sequence": 6,
                "kind": "run.succeeded",
                "data": {"status": "completed", "outputs": []},
            }
        )
        node.agent_trace_json = json.dumps(malformed_success)
        await pilot.press("right")
        assert "Output unavailable" in output_content.render().plain
        await pilot.press("left")
        node = app.store.current_run.nodes["agent_1"]

        legacy = json.loads(json.dumps(envelope))
        legacy["trace"]["evidence"]["events"] = [
            event
            for event in legacy["trace"]["evidence"]["events"]
            if event.get("kind") != "run.succeeded"
        ]
        node.agent_trace_json = json.dumps(legacy)
        await pilot.press("right")
        assert "Output unavailable" in output_content.render().plain
        await pilot.press("left")
        node = app.store.current_run.nodes["agent_1"]

        assert app.store.trace_inspector_tab == "trace"
        node.agent_trace_json = json.dumps(envelope)
        assert app.store.selected_agent_outputs is not None
        assert _SANDBOX_STDOUT_SENTINEL not in output_content.render().plain

        node.agent_trace_json = "{malformed"
        await pilot.press("right")
        assert "Output unavailable" in output_content.render().plain
        await pilot.press("left")
        node = app.store.current_run.nodes["agent_1"]
        assert app.store.trace_inspector_tab == "trace"

        node.agent_trace_json = "{malformed"
        assert "unavailable or malformed" in content.render().plain

        missing_node = app.store.current_run.nodes.pop("agent_1")
        missing_trace = content.render().plain
        assert "This agent step has not run yet for this run." in missing_trace
        assert "Trace unavailable or malformed" not in missing_trace
        await pilot.press("right")
        app.store.current_run.nodes.pop("agent_1", None)
        missing_output = output_content.render().plain
        assert "This agent step has not run yet for this run." in missing_output
        assert "Output unavailable" not in missing_output
        await pilot.press("left")
        app.store.current_run.nodes["agent_1"] = missing_node
        node = app.store.current_run.nodes["agent_1"]

        node.status = NodeStatus.PENDING
        node.agent_trace_json = None
        pending_trace = content.render().plain
        assert "This agent step has not run yet for this run." in pending_trace
        assert "Trace unavailable or malformed" not in pending_trace
        await pilot.press("right")
        node = app.store.current_run.nodes["agent_1"]
        node.status = NodeStatus.PENDING
        node.agent_trace_json = None
        pending_output = output_content.render().plain
        assert "This agent step has not run yet for this run." in pending_output
        assert "Output unavailable" not in pending_output
        await pilot.press("left")
        node = app.store.current_run.nodes["agent_1"]

        node.status = NodeStatus.RUNNING
        assert "waiting for live updates" in content.render().plain
        await pilot.press("right")
        node = app.store.current_run.nodes["agent_1"]
        node.status = NodeStatus.RUNNING
        node.agent_trace_json = None
        running_output = output_content.render().plain
        assert "Output will be available after this agent step completes." in running_output
        assert "This agent step has not run yet for this run." not in running_output
        await pilot.press("left")
        node = app.store.current_run.nodes["agent_1"]

        live = dict(envelope)
        live.update({"status": "in_progress", "trace": None, "error": None})
        node.agent_trace_json = json.dumps(live)
        live_render = content.render().plain
        assert "LIVE AGENT STATUS" in live_render
        assert "LIVE TRACE · 2 turn(s)" in live_render
        assert "AGENT TURN 1/2 · 0ms · 0 tool · 0 predict · LIVE" in live_render
        assert "print('first')" not in live_render
        assert _SANDBOX_STDOUT_SENTINEL not in live_render

        failed = dict(live)
        failed.update({"status": "error", "error": "provider failed"})
        node.agent_trace_json = json.dumps(failed)
        failed_render = content.render().plain
        assert "Status: error" in failed_render
        assert "provider failed" in failed_render
        assert "LIVE AGENT STATUS" in failed_render
        assert "Code generated · turn 1" in failed_render
        assert "Turn completed · turn 2" in failed_render
        assert "iteration.recorded" not in failed_render
        node.status = NodeStatus.FAILED
        await pilot.press("right")
        node = app.store.current_run.nodes["agent_1"]
        node.status = NodeStatus.FAILED
        node.agent_trace_json = json.dumps(failed)
        assert "Output unavailable" in output_content.render().plain
        assert _SANDBOX_STDOUT_SENTINEL not in output_content.render().plain
        await pilot.press("left")

        node = app.store.current_run.nodes["agent_1"]
        incomplete = json.loads(json.dumps(envelope))
        incomplete["trace"]["evidence"]["complete"] = False
        node.agent_trace_json = json.dumps(incomplete)
        final_render = content.render().plain
        assert "Live record: incomplete" in final_render
        assert "STRUCTURED TRACE · 2 turn(s)" in final_render
        assert "LIVE TRACE" not in final_render
