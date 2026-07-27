"""Headless behavior tests for workflow-detail pane controls."""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Button

from tui.app import AvalancheApp
from tui.mock import MockStateProvider
from tui.models import RunStatus


class RecordingProvider(MockStateProvider):
    """Mock provider that records cancellation requests."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled_run_ids: list[str] = []

    def cancel_run(self, run_id: str) -> None:
        self.cancelled_run_ids.append(run_id)
        super().cancel_run(run_id)


@pytest.mark.asyncio
async def test_dag_and_log_controls_toggle_independently_without_remounting() -> None:
    app = AvalancheApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._timer.pause()
        dag_widget = app._screen.query_one("#dag-panel")
        log_widget = app._screen.query_one("#log-content")
        selected_node = app.store.all_nodes[0]
        app.store.select_node(selected_node)
        app._refresh_widgets()

        await pilot.click("#dag-toggle-button")
        await pilot.pause()

        assert app._screen.query_one("#dag-container").display is False
        assert app._screen.query_one("#log-panel").display is True
        assert app._screen.query_one("#dag-panel") is dag_widget
        assert app.store.selected_node == selected_node

        app.action_focus_next_pane()
        assert app.store.focused_pane == "log"
        app.action_focus_next_pane()
        assert app.store.focused_pane == "run-history"
        assert str(app._screen.query_one("#dag-toggle-button", Button).label) == "Show DAG (d)"

        await pilot.press("d")
        await pilot.pause()
        assert app._screen.query_one("#dag-container").display is True
        assert app._screen.query_one("#dag-panel") is dag_widget

        await pilot.press("l")
        await pilot.pause()
        assert app._screen.query_one("#log-panel").display is False
        assert app._screen.query_one("#dag-container").display is True
        assert app._screen.query_one("#log-content") is log_widget
        assert str(app._screen.query_one("#log-toggle-button", Button).label) == "Show Logs (l)"

        await pilot.press("d")
        await pilot.pause()
        assert app._screen.query_one("#dag-container").display is False
        assert app.store.focused_pane == "run-history"

        app.store.focused_pane = "dag"
        app._refresh_widgets()
        assert app.store.focused_pane == "run-history"
        app.action_focus_next_pane()
        assert app.store.focused_pane == "run-history"


@pytest.mark.asyncio
async def test_run_controls_disable_unavailable_actions_and_cancel_selected_run() -> None:
    provider = RecordingProvider()
    app = AvalancheApp(provider=provider)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._timer.pause()

        start = app._screen.query_one("#run-start-button", Button)
        stop = app._screen.query_one("#run-stop-button", Button)
        actions = app._screen.query_one("#run-actions-button", Button)
        assert start.disabled is False
        assert stop.disabled is True

        app.store.current_workflow = None
        app._refresh_widgets()
        assert start.disabled is True
        assert actions.disabled is True

        app.store.current_workflow = app.store.workflows[0]
        run_id = app.store.start_run()
        assert run_id is not None
        app._refresh_widgets()
        assert stop.disabled is False
        assert actions.disabled is False

        app.store.current_run.status = RunStatus.PENDING
        app._refresh_widgets()
        assert stop.disabled is False
        assert app._screen.query_one("#run-menu-stop-button", Button).disabled is False

        app.store.current_run.status = RunStatus.RUNNING
        app._refresh_widgets()

        await pilot.click("#run-actions-button")
        await pilot.pause()
        menu = app._screen.query_one("#run-action-menu")
        assert menu.has_class("-open")
        assert app._screen.query_one("#run-menu-stop-button", Button).disabled is False

        await pilot.click("#run-menu-stop-button")
        for _ in range(20):
            if provider.cancelled_run_ids:
                break
            await asyncio.sleep(0.01)

        assert provider.cancelled_run_ids == [run_id]
        assert not app._screen.query_one("#run-action-menu").has_class("-open")


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
@pytest.mark.parametrize("menu_open", [False, True])
async def test_run_pane_contents_remain_above_dag(
    size: tuple[int, int], menu_open: bool
) -> None:
    app = AvalancheApp()

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app._timer.pause()
        if menu_open:
            await pilot.click("#run-actions-button")
            await pilot.pause()

        run_history = app._screen.query_one("#run-history")
        toolbar = app._screen.query_one("#run-toolbar")
        header = app._screen.query_one("#run-history-header")
        content = app._screen.query_one("#run-history-content")
        dag_controls = app._screen.query_one("#dag-controls")
        contents = [toolbar, header, content]
        if menu_open:
            menu = app._screen.query_one("#run-action-menu")
            assert menu.has_class("-open")
            contents.append(menu)

        inner_bottom = run_history.region.bottom - 1
        assert run_history.region.bottom <= dag_controls.region.y
        for widget in contents:
            assert widget.region.y >= run_history.region.y + 1
            assert widget.region.bottom <= inner_bottom


@pytest.mark.asyncio
async def test_compact_layout_closes_actions_menu_and_keeps_panes_onscreen() -> None:
    app = AvalancheApp(workflow="ml_workflow")

    async with app.run_test(size=(50, 15)) as pilot:
        await pilot.pause()
        app._timer.pause()
        app._run_actions_menu_open = True
        app._refresh_widgets()
        await pilot.pause()

        menu = app._screen.query_one("#run-action-menu")
        actions = app._screen.query_one("#run-actions-button", Button)
        assert app._run_actions_menu_open is False
        assert menu.has_class("-open") is False
        assert actions.disabled is True
        for widget_id in ("#run-history", "#dag-section", "#log-section"):
            assert app._screen.query_one(widget_id).region.bottom <= app.size.height - 1
