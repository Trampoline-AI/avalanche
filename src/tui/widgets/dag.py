"""DagWidget — renders the DAG visualization with click support."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from ..dag_layout import DagNode, render_dag_rich
from ..theme import AGENT_MARKER, AGENT_STYLE, DIM_STYLE


def _dag_hint(nodes: list[DagNode]) -> Text:
    """Describe DAG selection and agent inspection controls."""
    hint = Text("Click or ↑↓←→ select node", DIM_STYLE)
    if any(node.is_agent for node in nodes):
        hint.append("  •  Enter inspect selected agent step  •  ", DIM_STYLE)
        hint.append(f"{AGENT_MARKER} agent step", AGENT_STYLE)
    return hint



class DagWidget(Static, can_focus=False):
    """Renders the DAG visualization. Emits click events for node selection."""

    ALLOW_SELECT = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._hit_regions: list[tuple[int, int, int, DagNode]] = []
        self._test_store = None
        self._last_scrolled_node: DagNode | None = None

    def render(self) -> Text:
        store = self._test_store or self.app.store
        if store.dag is None:
            return Text()
        lines = render_dag_rich(
            store.dag, store.node_statuses, store.frame,
            store.selected_node, store.node_elapsed,
        )
        lines.append(_dag_hint(store.all_nodes))


        # Build hit regions from the plain text of each line.
        # Use _render_row to match each node only on its visual row,
        # so duplicate-named nodes (e.g. notify_slack in two branches)
        # get separate, correct hit regions.
        self._hit_regions.clear()
        for node in store.all_nodes:
            if node.virtual:
                continue
            render_row = getattr(node, "_render_row", None)
            if render_row is None or render_row >= len(lines):
                continue
            plain = lines[render_row].plain
            needle = f" {node.display_name} "
            col = plain.find(needle)
            if col >= 0:
                # Extend hit region to include "(dur) " suffix if present
                end = col + len(needle)
                if end < len(plain) and plain[end] == "(":
                    paren_close = plain.find(") ", end)
                    if paren_close >= 0:
                        end = paren_close + 2
                self._hit_regions.append((render_row, col, end, node))

        # Set widget min-width to DAG content width to prevent line wrapping
        dag_width = max((len(line.plain) for line in lines), default=0)
        self.styles.min_width = dag_width + 2

        # Only scroll when the selection actually changes
        if store.selected_node is not self._last_scrolled_node:
            self._last_scrolled_node = store.selected_node
            if store.selected_node is not None:
                self._scroll_to_node(store.selected_node)

        result = Text(no_wrap=True)
        for i, line in enumerate(lines):
            if i > 0:
                result.append("\n")
            result.append_text(line)
        return result

    def _scroll_to_node(self, node: DagNode) -> None:
        """Scroll the parent container to center the given node."""
        try:
            container = self.parent
            if container is None:
                return
            for row, col_start, col_end, n in self._hit_regions:
                if n is node:
                    half_w = container.content_size.width // 2
                    half_h = container.content_size.height // 2
                    container.scroll_to(
                        x=max(0, col_start - half_w),
                        y=max(0, row - half_h),
                        animate=False,
                    )
                    return
        except Exception:
            pass

    def on_click(self, event) -> None:
        click_row = event.y
        click_col = event.x
        for row_idx, col_start, col_end, node in self._hit_regions:
            if click_row == row_idx and col_start <= click_col < col_end:
                self.app.select_node(node)
                return
