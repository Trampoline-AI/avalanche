"""Headless behavior tests for workflow-detail pane controls."""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Button

from tui.app import AvalancheApp
from tui.mock import MockStateProvider


class RecordingProvider(MockStateProvider):
    """Mock provider that records cancellation requests."""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled_run_ids: list[str] = []

    def cancel_run(self, run_id: str) -> None:
        self.cancelled_run_ids.append(run_id)
        super().cancel_run(run_id)


@pytest.mark.asyncio
async def test_dag_and_log_hints_toggle_independently_without_remounting() -> None:
    app = AvalancheApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._timer.pause()
        dag_widget = app._screen.query_one("#dag-panel")
        log_widget = app._screen.query_one("#log-content")
        selected_node = app.store.all_nodes[0]
        dag_container = app._screen.query_one("#dag-container")
        dag_scroll_position = (dag_container.scroll_x, dag_container.scroll_y)
        log_scroll_position = (log_widget.scroll_x, log_widget.scroll_y)
        app.store.select_node(selected_node)
        app._refresh_widgets()

        assert list(app._screen.query("#dag-section Button")) == []
        assert list(app._screen.query("#log-section Button")) == []
        await pilot.click("#dag-key-hint")
        await pilot.pause()
        assert app._screen.query_one("#dag-container").display is True

        await pilot.press("d")
        await pilot.pause()

        assert app._screen.query_one("#dag-container").display is False
        assert app._screen.query_one("#log-panel").display is True
        assert app._screen.query_one("#dag-panel") is dag_widget
        assert app.store.selected_node == selected_node
        assert "restore" in str(app._screen.query_one("#dag-key-hint").render())

        app.action_focus_next_pane()
        assert app.store.focused_pane == "log"
        app.action_focus_next_pane()
        assert app.store.focused_pane == "run-history"

        await pilot.press("d")
        await pilot.pause()
        assert app._screen.query_one("#dag-container").display is True
        assert (dag_container.scroll_x, dag_container.scroll_y) == dag_scroll_position
        assert app._screen.query_one("#dag-panel") is dag_widget

        await pilot.press("l")
        await pilot.pause()
        assert app._screen.query_one("#log-panel").display is False
        assert app._screen.query_one("#dag-container").display is True
        assert app._screen.query_one("#log-content") is log_widget
        assert (log_widget.scroll_x, log_widget.scroll_y) == log_scroll_position
        assert "restore" in str(app._screen.query_one("#log-key-hint").render())

        await pilot.press("l")
        await pilot.pause()
        assert app._screen.query_one("#log-panel").display is True
        assert app._screen.query_one("#log-content") is log_widget
        assert (log_widget.scroll_x, log_widget.scroll_y) == log_scroll_position

        await pilot.press("d")
        await pilot.pause()
        assert app._screen.query_one("#dag-container").display is False
        assert app.store.focused_pane == "run-history"

        app.store.focused_pane = "dag"
        app._refresh_widgets()
        assert app.store.focused_pane == "run-history"
        app.action_focus_next_pane()
        assert app.store.focused_pane == "log"


@pytest.mark.asyncio
async def test_dag_toggle_retains_scroll_position() -> None:
    app = AvalancheApp(workflow="ml_workflow")

    async with app.run_test(size=(50, 15)) as pilot:
        await pilot.pause()
        app._timer.pause()
        dag_container = app._screen.query_one("#dag-container")
        assert dag_container.max_scroll_x > 0
        assert dag_container.max_scroll_y > 0
        dag_container._scroll_to(x=8, y=3, animate=False)
        scroll_position = (dag_container.scroll_target_x, dag_container.scroll_target_y)
        assert scroll_position == (8, 3)

        await pilot.press("d")
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()

        assert (dag_container.scroll_target_x, dag_container.scroll_target_y) == scroll_position



@pytest.mark.asyncio
@pytest.mark.parametrize(("toggle_key", "pane"), [("d", "dag"), ("l", "log")])
async def test_restoring_hidden_active_pane_restores_fallback_focus(
    toggle_key: str, pane: str
) -> None:
    app = AvalancheApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._timer.pause()
        app.store.focused_pane = pane

        await pilot.press(toggle_key)
        await pilot.pause()
        assert app.store.focused_pane == "run-history"

        await pilot.press(toggle_key)
        await pilot.pause()
        assert app.store.focused_pane == pane


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("toggle_key", "pane", "other_pane"),
    [("d", "dag", "log"), ("l", "log", "dag")],
)
async def test_restoring_hidden_pane_does_not_steal_changed_focus(
    toggle_key: str, pane: str, other_pane: str
) -> None:
    app = AvalancheApp()

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._timer.pause()
        app.store.focused_pane = pane

        await pilot.press(toggle_key)
        await pilot.pause()
        assert app.store.focused_pane == "run-history"

        app.store.focused_pane = other_pane
        app._refresh_widgets()
        await pilot.press(toggle_key)
        await pilot.pause()
        assert app.store.focused_pane == other_pane


@pytest.mark.asyncio
async def test_run_key_bindings_start_cancel_and_open_actions_menu() -> None:
    provider = RecordingProvider()
    app = AvalancheApp(provider=provider)

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._timer.pause()

        start = app._screen.query_one("#run-start-button", Button)
        stop = app._screen.query_one("#run-stop-button", Button)
        assert start.disabled is False

        await pilot.press("r")
        for _ in range(20):
            if not app.store._start_run_in_flight:
                break
            await asyncio.sleep(0.01)
        app.store._apply_background_updates()
        app._refresh_widgets()

        run = app.store.current_run
        assert run is not None
        assert stop.disabled is False

        await pilot.press("a")
        await pilot.pause()
        menu = app._screen.query_one("#run-action-menu")
        assert menu.has_class("-open")

        await pilot.press("x")
        for _ in range(20):
            if provider.cancelled_run_ids:
                break
            await asyncio.sleep(0.01)

        assert provider.cancelled_run_ids == [run.run_id]
        assert not menu.has_class("-open")


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
        legend = app._screen.query_one("#run-key-legend")
        header = app._screen.query_one("#run-history-header")
        content = app._screen.query_one("#run-history-content")
        dag_hint = app._screen.query_one("#dag-key-hint")
        contents = [toolbar, legend, header, content]
        if menu_open:
            menu = app._screen.query_one("#run-action-menu")
            assert menu.has_class("-open")
            contents.append(menu)

        inner_bottom = run_history.region.bottom - 1
        assert run_history.region.bottom <= dag_hint.region.y
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


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 40), (50, 15)])
async def test_run_key_legend_stays_visible_and_marks_disabled_actions(
    size: tuple[int, int],
) -> None:
    app = AvalancheApp()

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app._timer.pause()

        legend = app._screen.query_one("#run-key-legend")
        key_ids = (
            "#run-key-run",
            "#run-key-stop",
            "#run-key-actions",
            "#run-key-select",
        )
        rendered = " ".join(
            str(app._screen.query_one(widget_id).render()) for widget_id in key_ids
        )
        assert "[r] Run" in rendered
        assert "[x] Stop" in rendered
        assert "[a] Actions" in rendered
        assert "[↑↓] Select" in rendered
        assert legend.region.y == app._screen.query_one("#run-history").region.y + 1
        assert legend.region.right <= app.size.width
        for widget_id in key_ids:
            key = app._screen.query_one(widget_id)
            assert key.region.x >= legend.region.x
            assert key.region.right <= legend.region.right
        assert app._screen.query_one("#run-key-actions").has_class("-disabled") is (
            size == (50, 15)
        )

        app.store.current_workflow = None
        app._refresh_widgets()

        assert app._screen.query_one("#run-key-run").has_class("-disabled")
        assert app._screen.query_one("#run-key-actions").has_class("-disabled")
        await pilot.press("a")
        await pilot.pause()
        assert not app._screen.query_one("#run-action-menu").has_class("-open")
