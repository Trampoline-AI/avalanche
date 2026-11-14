"""A map's index-aligned item list; missing items exist only in this view."""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

from runtime.operator.models import RunStatus

from ..theme import node_status_display, node_wait_kind


class MapItemsScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "close", "Close")]
    DEFAULT_CSS = """
    MapItemsScreen { align: center middle; }
    MapItemsScreen > Vertical {
        width: 90; height: auto; max-height: 80%; padding: 1 2;
        border: round $accent; background: $surface;
    }
    MapItemsScreen OptionList { height: auto; max-height: 28; }
    """

    def __init__(self, parent: str):
        super().__init__()
        self.parent_id = parent
        self._token: tuple = ()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Map items — select an item for its trace / detail")
            yield OptionList(id="map-items")

    def on_mount(self) -> None:
        self._refresh_items()
        self.set_interval(0.2, self._refresh_items)

    def _refresh_items(self) -> None:
        run = self.app.store.current_run
        if run is None or self.parent_id not in run.expansions:
            return
        n = run.expansions[self.parent_id]
        items = {
            item.map_index: item
            for item in run.nodes.values()
            if item.map_parent == self.parent_id
        }
        token = (n, run.status, tuple((i, item.status) for i, item in items.items()))
        if token == self._token:
            return
        self._token = token
        options = self.query_one(OptionList)
        highlighted = options.highlighted
        options.clear_options()
        terminal = run.status in (RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED)
        placeholder = "not run" if terminal else "not started"
        for i in range(n):
            item = items.get(i)
            if item is not None:
                icon, style = node_status_display(
                    item.status, node_wait_kind(self.app.store.current_workflow, item.node_id)
                )
                label = Text.assemble((icon, style), f" {item.name}")
            options.add_option(
                Option(label, id=item.node_id)
                if item is not None
                else Option(
                    f"· [{i}] {placeholder}",
                    disabled=True,
                )
            )
        options.highlighted = highlighted if highlighted is not None else 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_close(self) -> None:
        self.dismiss(None)
