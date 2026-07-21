"""Tests for the Avalanche TUI module."""

import threading
import time
from dataclasses import replace
from datetime import datetime

import pytest

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
    RunState,
    RunStatus,
    WorkflowInfo,
)
from avalanche.tui.ui_store import UIStore
from avalanche.tui.widgets.run_history import RunHistoryWidget
from avalanche.tui.widgets.sidebar import Sidebar
from avalanche.tui.widgets.status_bar import StatusBar


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
            node_id="x", name="x", node_type="step",
            status=NodeStatus.RUNNING, started_at=time.monotonic() - 2.0,
        )
        assert ns.elapsed is not None
        assert ns.elapsed >= 1.9

    def test_node_state_elapsed_completed(self):
        t = time.monotonic()
        ns = NodeState(
            node_id="x", name="x", node_type="step",
            status=NodeStatus.SUCCESS, started_at=t - 5.0, ended_at=t - 2.0,
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
        assert "&" in combined, (
            f"Expected '&' between parallel branches, got:\n{combined}"
        )

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
        assert ("push_to_cdn_1", "page_highlights_1") in dag.skip_edges, (
            f"Expected skip edge push_to_cdn→page_highlights, got: {dag.skip_edges}"
        )

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
        graph = {
            ids[i]: [ids[j] for j in range(i + 1, 7)]
            for i in range(6)
        }
        info = WorkflowInfo(
            name="dense", file_path="f", node_ids=ids, graph=graph,
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
            assert "─┐" not in plain or "┌──" in plain or "├" in plain, (
                f"Line {i} has closing bracket without opening — possible wrap: {plain}"
            )


# ── Mock Provider ──────────────────────────────────────────────────────────


class TestMockStateProvider:
    def test_list_workflows(self):
        provider = MockStateProvider()
        workflows = provider.list_workflows()
        assert len(workflows) == 6
        names = {p.name for p in workflows}
        assert "order_workflow" in names
        assert "data_platform" in names

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

        class BlockingCatalogProvider:
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

        app = AvalancheApp(
            BlockingCatalogProvider(), workflow="desired", node="shared_node"
        )
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

        class BlockingStartProvider:
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

        class BlockingStartProvider:
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
            workflow
            for workflow in store.workflows
            if workflow.selector != started_selector
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

        class FailedFollowupProvider:
            def __init__(self):
                self.connected = True
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
                    self.connected = False
                    self.last_error = "UNAVAILABLE: get failed"
                    return None
                return RunState(run_id=run_id, flow_name="order_workflow")

            def list_runs(self, selector):
                if self.started and failed_call == "list":
                    self.connected = False
                    self.last_error = "DEADLINE_EXCEEDED: list failed"
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

        class BlockingStartProvider:
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

    def test_duplicate_display_names_use_ids_and_refresh_preserves_selection(self):
        class MutableProvider:
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

        class BlockingProvider:
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

        class BlockingProvider:
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
        store._runs_cache = [older, stale]
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

        class BlockingProvider:
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

        class BlockingFirstProvider:
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

        class BlockingAProvider:
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

        class BlockingStartProvider:
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
        store._runs_cache = [older, latest]
        store.workflow_statuses = {workflow.selector: latest.status}

        store.enqueue_run_update(updated_older)
        store._apply_background_updates()

        assert store._runs_cache == [updated_older, latest]
        assert store.current_run is latest
        assert store.workflow_statuses[workflow.selector] is RunStatus.SUCCESS

    def test_run_error_is_rendered_in_status_bar(self):
        store = UIStore(MockStateProvider())
        store.run_error = "UNAVAILABLE: get failed"
        status = StatusBar()
        status._test_store = store

        rendered = status.render().plain

        assert "✗ UNAVAILABLE: get failed" in rendered

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
            name="nonexistent", file_path="x/y.py",
            node_ids=[], graph={}, node_types={},
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
            workflow_indices = [
                i for i, it in enumerate(items) if not it.is_folder
            ]
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
            assert app.store.selected_run_id == target_run.run_id, (
                f"Expected run {target_run.run_id}, got {app.store.selected_run_id}"
            )

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
            assert node_name in str(lp.border_title), (
                f"Expected '{node_name}' in border title, got '{lp.border_title}'"
            )

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
            sidebar.on_mouse_down(MouseDown(
                sidebar, x=sidebar.size.width - 1, y=5,
                delta_x=0, delta_y=0, button=1,
                shift=False, meta=False, ctrl=False,
                screen_x=original_width - 1, screen_y=5,
            ))
            assert sidebar._dragging is True

            sidebar.on_mouse_move(MouseMove(
                sidebar, x=0, y=5,
                delta_x=5, delta_y=0, button=1,
                shift=False, meta=False, ctrl=False,
                screen_x=original_width + 5, screen_y=5,
            ))
            assert app.store.sidebar_width == original_width + 5

            sidebar.on_mouse_up(MouseUp(
                sidebar, x=0, y=5,
                delta_x=0, delta_y=0, button=1,
                shift=False, meta=False, ctrl=False,
                screen_x=original_width + 5, screen_y=5,
            ))
            assert sidebar._dragging is False

    async def test_sidebar_drag_enforces_minimum_width(self):
        """Sidebar width should not go below 15 during drag."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app._screen.query_one("#sidebar")

            from textual.events import MouseDown, MouseMove, MouseUp
            sidebar.on_mouse_down(MouseDown(
                sidebar, x=sidebar.size.width - 1, y=5,
                delta_x=0, delta_y=0, button=1,
                shift=False, meta=False, ctrl=False,
                screen_x=30, screen_y=5,
            ))
            sidebar.on_mouse_move(MouseMove(
                sidebar, x=0, y=5,
                delta_x=-20, delta_y=0, button=1,
                shift=False, meta=False, ctrl=False,
                screen_x=5, screen_y=5,
            ))
            assert app.store.sidebar_width >= 15

            sidebar.on_mouse_up(MouseUp(
                sidebar, x=0, y=5,
                delta_x=0, delta_y=0, button=1,
                shift=False, meta=False, ctrl=False,
                screen_x=5, screen_y=5,
            ))

    async def test_sidebar_text_clips_to_width(self):
        """Sidebar should clip long labels to fit within content width."""
        app = await self._make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            sidebar = app._screen.query_one("#sidebar")
            # Shrink sidebar to force clipping
            sidebar.styles.width = 18
            app.store.sidebar_width = 18
            await pilot.pause()

            rendered = sidebar.render()
            lines = rendered.plain.split("\n")
            content_width = sidebar.content_size.width or 16
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
                assert w.styles.scrollbar_size_vertical == 1, (
                    f"{widget_id} scrollbar_size_vertical={w.styles.scrollbar_size_vertical}"
                )
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
                assert app.store.selected_node is not None, (
                    f"Click at ({target_col}, {row_idx}) should select {node.name}"
                )
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
            assert len(non_empty) >= 3, (
                f"Expected at least 3 DAG rows, got {len(non_empty)}"
            )
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
                assert w.scroll_x == 0, (
                    f"{pane}: left/right arrows should not scroll horizontally"
                )

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
                assert app.store.selected_node is not None, (
                    f"Right should select a DAG node while {pane} is focused"
                )
                first = app.store.selected_node

                await pilot.press("right")
                await pilot.pause()
                assert app.store.selected_node.col > first.col, (
                    f"Right should advance DAG selection while {pane} is focused"
                )

                await pilot.press("left")
                await pilot.pause()
                assert app.store.selected_node.col == first.col, (
                    f"Left should move DAG selection back while {pane} is focused"
                )

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
