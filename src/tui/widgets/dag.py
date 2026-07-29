"""DagWidget — renders the DAG visualization with click support."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from ..dag_layout import DagNode, render_dag_rich


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
            store.dag,
            store.node_statuses,
            store.frame,
            store.selected_node,
            store.node_elapsed,
        )

        # Build regions for each node's name and, for agent nodes, caption.
        # Primary rows disambiguate duplicate display names in parallel branches.
        self._hit_regions.clear()
        for node in store.all_nodes:
            if node.virtual or node.render_row is None or node.render_col is None:
                continue
            if node.render_row >= len(lines):
                continue
            plain = lines[node.render_row].plain
            col = node.render_col
            needle = f" {node.display_name} "
            end = col + len(needle)
            # Extend the name region to include a duration suffix.
            if end < len(plain) and plain[end] == "(":
                paren_close = plain.find(") ", end)
                if paren_close >= 0:
                    end = paren_close + 2
            self._hit_regions.append((node.render_row, col, end, node))
            if (
                node.is_agent
                and node.caption_render_row is not None
                and node.caption_col is not None
                and node.caption_render_row < len(lines)
            ):
                caption = "(agent)"
                caption_plain = lines[node.caption_render_row].plain
                caption_col = node.caption_col
                if caption_plain[caption_col : caption_col + len(caption)] == caption:
                    self._hit_regions.append(
                        (node.caption_render_row, caption_col, caption_col + len(caption), node)
                    )

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
