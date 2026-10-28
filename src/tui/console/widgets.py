"""Small shared widgets: text tabs, selectable tables, pickers, confirm dialogs."""

from __future__ import annotations

from collections.abc import Sequence

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, OptionList, Static
from textual.widgets.option_list import Option


def click(action: str) -> Style:
    return Style.from_meta({"@click": action})


def text_tabs(
    options: Sequence[tuple[str, str]], active: str, action: str, pal: dict, hint: str = ""
) -> Text:
    """console TextTabs: active label underlined in blue (click, [ ] or 1-6)."""
    out = Text(no_wrap=True, overflow="ellipsis")
    for index, (value, label) in enumerate(options):
        if index:
            out.append("   ")
        style = (
            Style(color=pal["accent"], bold=True, underline=True)
            if value == active
            else Style(color=pal["muted"])
        )
        out.append(label, style + click(f"{action}('{value}')"))
    if hint:
        out.append("   " + hint, Style(color=pal["hairline"]))
    return out


class Table(DataTable):
    """Row-cursor table that keeps its cursor on the same row key across refreshes."""

    DEFAULT_CSS = """
    Table { height: auto; max-height: 100%; background: $background; }
    Table > .datatable--header {
        background: $background; color: $text-muted; text-style: none;
    }
    Table > .datatable--cursor { background: $block-cursor-background; color: $foreground; }
    Table:focus > .datatable--cursor { background: $block-cursor-background; color: $primary; }
    Table > .datatable--hover { background: $panel; }
    """

    def __init__(self, *, id: str | None = None):
        # Cell colours (status badges) win over the cursor colour, as in the console's rows.
        super().__init__(
            cursor_type="row",
            zebra_stripes=False,
            show_cursor=True,
            id=id,
            cursor_foreground_priority="renderable",
        )
        self.keys: list[str] = []
        self.meta: dict[str, object] = {}
        self._shape: tuple = ()
        self._signature: list = []

    def set_rows(
        self,
        columns: Sequence[str],
        rows: Sequence[tuple[str, Sequence[object], object]],
        *,
        select: str | None = None,
    ) -> None:
        # `select` positions the cursor only when the row set changes; otherwise the user's
        # cursor wins (a redraw must never snap it back).
        current = self.current[0]
        if select and (
            tuple(columns) != self._shape or [key for key, _, _ in rows] != self.keys
        ):
            current = select
        if tuple(columns) != self._shape:
            self.clear(columns=True)
            self._columns = self.add_columns(*columns)
            self._shape, self._signature = tuple(columns), []
        signature = [(key, tuple(str(cell) for cell in cells)) for key, cells, _ in rows]
        self.meta = {key: meta for key, _, meta in rows}
        if signature != self._signature:
            same_rows = [key for key, _ in signature] == self.keys and len(
                self._signature
            ) == len(signature)
            if same_rows:
                # In-place updates keep cursor and scroll still (animated glyphs, counts).
                for (key, cells, _), (_, old) in zip(rows, self._signature):
                    for index, cell in enumerate(cells):
                        if str(cell) != old[index]:
                            # Widen the column: badges load after their row (… → [3 FAILED]).
                            self.update_cell(key, self._columns[index], cell, update_width=True)
            else:
                self.clear(columns=False)
                self.keys = []
                for key, cells, _ in rows:
                    height = max(str(cell).count("\n") + 1 for cell in cells) if cells else 1
                    self.add_row(*cells, key=key, height=height)
                    self.keys.append(key)
            self._signature = signature
        if current in self.keys and self.keys.index(current) != self.cursor_row:
            self.move_cursor(row=self.keys.index(current), animate=False)

    @property
    def current(self) -> tuple[str | None, object]:
        if not self.keys or not 0 <= self.cursor_row < len(self.keys):
            return None, None
        key = self.keys[self.cursor_row]
        return key, self.meta.get(key)


class Picker(ModalScreen[str | None]):
    """Breadcrumb sibling switcher (CrumbSwitcher) and organization control."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]
    DEFAULT_CSS = """
    Picker { align: left top; background: $background 40%; }
    Picker > Vertical { width: 64; max-height: 24; margin: 1 0 0 8; border: solid $border;
                        background: $surface; height: auto; }
    Picker #picker-title { color: $text-muted; padding: 0 1; }
    Picker OptionList { border: none; background: $surface; height: auto; max-height: 20; }
    """

    def __init__(self, title: str, items: Sequence[tuple[str, str, str]], current: str | None):
        super().__init__()
        self.title_text, self.items, self.current = title, list(items), current

    def compose(self) -> ComposeResult:
        options = []
        for key, label, detail in self.items:
            row = Text.assemble(("■ " if key == self.current else "  ", "bold"), label)
            if detail:
                row.append("  " + detail, "dim")
            options.append(Option(row, id=key))
        with Vertical():
            yield Static(self.title_text.upper(), id="picker-title")
            if options:
                yield OptionList(*options)
            else:
                yield Static("  Nothing to switch to")

    def on_mount(self) -> None:
        for options in self.query(OptionList):
            index = next(
                (i for i, (key, _, _) in enumerate(self.items) if key == self.current), 0
            )
            options.highlighted = index
            options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class Confirm(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "dismiss(True)", "Yes"),
        Binding("n,escape", "dismiss(False)", "No"),
    ]
    DEFAULT_CSS = """
    Confirm { align: center middle; background: $background 40%; }
    Confirm > Static {
        width: 72; height: auto; border: solid $error; background: $surface; padding: 1 2;
    }
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Static(
            Text.assemble(
                self.message,
                "\n\n",
                ("y", "bold"),
                " confirm   ",
                ("n", "bold"),
                " keep running",
            )
        )


HELP = """\
Navigation      h Projects home · a Activity · enter open · backspace back · ctrl+b sidebar
Sidebar tree    space expand/collapse a project (never selects) · enter open
Breadcrumbs     o organization · p project · w flow (sibling switchers)
Tabs            [ ] previous/next · 1-6 jump
Lists           s status filter · d deployment filter · m load more · R refresh now
Flow / run      tab focus graph/panel · arrows on the graph · enter step drawer · esc close
                c see current deployment · z collapse panel · - + resize panel
Actions         x cancel run · D download artifact (permission-gated; off in --read-only)
Other           y copy the focused ID (OSC 52) · t light/dark · ? this help · q quit
"""


class Help(ModalScreen[None]):
    BINDINGS = [Binding("escape,question_mark,q", "dismiss(None)", "Close")]
    DEFAULT_CSS = """
    Help { align: center middle; background: $background 40%; }
    Help > Static {
        width: 96; height: auto; border: solid $border; background: $surface; padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(Text.assemble(("KEYS\n\n", "bold"), HELP))
