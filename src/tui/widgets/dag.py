"""DagWidget — renders the DAG visualization with click support."""

from __future__ import annotations

from rich.text import Text
from textual.strip import Strip
from textual.widgets import Static

from ..dag_layout import DagNode, marker_for, render_dag_rich
from ..models import NodeStatus
from ..state import status_label
from ..theme import OTHER_STATUS_STYLE, STATUS_STYLES, status_display


class DagWidget(Static, can_focus=False):
    """Renders the DAG visualization. Emits click events for node selection."""

    ALLOW_SELECT = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._hit_regions: list[tuple[int, int, int, DagNode]] = []
        self._test_store = None
        self._last_scrolled_node: DagNode | None = None
        self._expanded_groups: set[int] = set()
        self._large_rows: list[DagNode | int] = []
        self._large_token: tuple | None = None
        self._node_lines: dict[str, tuple[tuple, Text]] = {}

    def _large_index(self, store) -> None:
        selected = store.selected_node
        selected_index = store.all_nodes.index(selected) if selected is not None else -1
        selected_group = selected_index // 50 if selected_index >= 0 else -1
        token = (id(store.dag), selected_group, frozenset(self._expanded_groups))
        if token == self._large_token:
            return
        self._large_token = token
        rows: list[DagNode | int] = []
        for start in range(0, len(store.all_nodes), 50):
            group = start // 50
            rows.append(group)
            if group in self._expanded_groups or group == selected_group:
                rows.extend(store.all_nodes[start : start + 50])
        self._large_rows = rows
        self.refresh(layout=True)
        if selected is not None and self.parent is not None:
            row = next((i for i, item in enumerate(rows) if item is selected), None)
            if row is not None:
                self.parent.scroll_to(
                    y=max(0, row - self.parent.content_size.height // 2), animate=False
                )
        self._node_lines.clear()
        self.styles.min_width = 90

    def expand_all(self, expanded: bool) -> None:
        store = self._test_store or self.app.store
        self._expanded_groups = (
            set(range((len(store.all_nodes) + 49) // 50)) if expanded else set()
        )
        self._large_token = None
        self.refresh(layout=True)

    def _large_row(self, store, index: int) -> Text:
        self._large_index(store)
        if index < 0 or index >= len(self._large_rows):
            return Text()
        item = self._large_rows[index]
        if isinstance(item, int):
            start = item * 50
            return Text(
                f"  Nodes {start + 1}–{min(start + 50, len(store.all_nodes))} "
                "[click to expand/collapse; e/z all]",
                style="dim",
            )
        state = store.current_run.nodes.get(item.name) if store.current_run else None
        status = status_label(state.status) if state else "PENDING"
        selected = item is store.selected_node
        revision = state.revision if state else -1
        node_status = state.status if state else NodeStatus.PENDING
        animation = store.frame if node_status is NodeStatus.RUNNING else 0
        token = (status, type(node_status), revision, selected, animation)
        cached = self._node_lines.get(item.name)
        if cached is not None and cached[0] == token:
            return cached[1]
        workflow = store.current_workflow
        children = workflow.graph.get(item.name, []) if workflow else []
        edges = ", ".join(children[:4]) + (" …" if len(children) > 4 else "")
        line = Text(f"  {'▶' if selected else ' '} {item.display_name}  ")
        line.append(
            f"{marker_for(item, node_status, store.frame)} {status}",
            status_display(node_status, STATUS_STYLES, OTHER_STATUS_STYLE),
        )
        line.append(f"  → {edges}")
        if selected:
            line.stylize("reverse")
        self._node_lines[item.name] = (token, line)
        if len(self._node_lines) > 256:
            del self._node_lines[next(iter(self._node_lines))]
        return line

    def get_content_height(self, container, viewport, width: int) -> int:
        store = self._test_store or self.app.store
        if len(store.all_nodes) > 200:
            self._large_index(store)
            return len(self._large_rows)
        return super().get_content_height(container, viewport, width)

    def render_line(self, y: int) -> Strip:
        store = self._test_store or self.app.store
        if len(store.all_nodes) <= 200:
            return super().render_line(y)
        options = self.app.console.options.update(width=self.size.width, height=1)
        return Strip.from_lines(
            self.app.console.render_lines(self._large_row(store, y), options)
        )[0]

    def render(self) -> Text:
        store = self._test_store or self.app.store
        if store.dag is None:
            return Text()
        if len(store.all_nodes) > 200:
            self._large_index(store)
            start = int(self.parent.scroll_y) if self.parent else 0
            height = self.parent.size.height if self.parent else 24
            return Text("\n").join(
                self._large_row(store, i)
                for i in range(start, min(len(self._large_rows), start + height))
            )
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
        store = self.app.store
        if len(store.all_nodes) > 200:
            self._large_index(store)
            if 0 <= event.y < len(self._large_rows):
                item = self._large_rows[event.y]
                if isinstance(item, int):
                    if item in self._expanded_groups:
                        self._expanded_groups.remove(item)
                    else:
                        self._expanded_groups.add(item)
                    self._large_token = None
                    self.refresh(layout=True)
                else:
                    self.app.select_node(item)
            return
        click_row = event.y
        click_col = event.x
        for row_idx, col_start, col_end, node in self._hit_regions:
            if click_row == row_idx and col_start <= click_col < col_end:
                self.app.select_node(node)
                return
