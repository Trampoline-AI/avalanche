"""Sidebar widget — folder tree workflow navigator with keyboard support."""

from __future__ import annotations

from dataclasses import dataclass

from rich.style import Style
from rich.text import Text
from textual.events import MouseDown, MouseMove, MouseUp
from textual.message import Message
from textual.widgets import Static

from ..models import RunStatus, WorkflowInfo
from ..theme import (
    ICE_BRIGHT,
    ICE_FAIL,
    ICE_FROST,
    ICE_PURPLE,
    ICE_STEEL,
    ICE_TEAL,
    ICE_WARN,
    SPINNER_FRAMES,
)

_SELECTED_BG = Style(bgcolor="#2a4a6a")
_CURSOR_BG = Style(bgcolor="#1e3555")

_STATUS_ICONS: dict[RunStatus, str] = {
    RunStatus.REQUESTING: "◌",
    RunStatus.PENDING: "○",
    RunStatus.RUNNING: "◐",
    RunStatus.SUCCESS: "✓",
    RunStatus.FAILED: "✗",
    RunStatus.CANCELLED: "⊘",
}

_STATUS_STYLES: dict[RunStatus, Style] = {
    RunStatus.REQUESTING: Style(color=ICE_WARN, bold=True),
    RunStatus.PENDING: Style(color=ICE_STEEL),
    RunStatus.RUNNING: Style(color=ICE_BRIGHT, bold=True),
    RunStatus.SUCCESS: Style(color=ICE_TEAL, bold=True),
    RunStatus.FAILED: Style(color=ICE_FAIL, bold=True),
    RunStatus.CANCELLED: Style(color=ICE_PURPLE),
}


@dataclass
class _TreeItem:
    """A row in the flattened tree: either a folder or a workflow."""
    label: str
    depth: int
    is_folder: bool
    folder_path: str = ""
    workflow: WorkflowInfo | None = None


class Sidebar(Static, can_focus=True):
    """Folder-tree sidebar listing workflows grouped by relative source path.

    When focused, up/down moves cursor, enter selects/toggles.
    """

    ALLOW_SELECT = False

    @dataclass
    class WorkflowSelected(Message):
        workflow: WorkflowInfo

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._flat_items: list[_TreeItem] = []
        self._test_store = None
        self._dragging: bool = False
        self.border_title = "EXPLORER"

    def _rebuild_tree(self, workflows=None, expanded=None) -> None:
        """Build the flat item list from workflow data.

        When called without arguments, reads from the store.
        Explicit arguments allow standalone usage in tests.
        """
        if workflows is None:
            store = self._test_store or self.app.store
            workflows = store.workflows
            expanded = store.sidebar_expanded
        if expanded is None:
            expanded = set()

        tree: dict = {}
        for p in workflows:
            source = p.source_file.replace("\\", "/")
            if p.root_alias:
                source = f"{p.root_alias}/{source}"
            parts = source.split("/")
            folders = parts[:-1]
            node = tree
            for f in folders:
                if f not in node:
                    node[f] = {}
                node = node[f]
            node[f"__workflow__{p.selector}"] = p

        self._flat_items.clear()
        self._walk_tree(tree, depth=0, path_prefix="", expanded=expanded)

    def _walk_tree(self, node: dict, depth: int, path_prefix: str, expanded: set[str]) -> None:
        folders = sorted(k for k in node if not k.startswith("__workflow__"))
        workflows = sorted(
            ((k, node[k]) for k in node if k.startswith("__workflow__")),
            key=lambda x: (x[1].rendered_name, x[1].selector),
        )

        for folder_name in folders:
            folder_path = f"{path_prefix}/{folder_name}" if path_prefix else folder_name
            self._flat_items.append(_TreeItem(
                label=folder_name, depth=depth, is_folder=True, folder_path=folder_path,
            ))
            if folder_path in expanded:
                self._walk_tree(node[folder_name], depth + 1, folder_path, expanded)

        for _, workflow in workflows:
            self._flat_items.append(_TreeItem(
                label=workflow.rendered_name,
                depth=depth,
                is_folder=False,
                workflow=workflow,
            ))

    def render(self) -> Text:
        self._rebuild_tree()
        store = self._test_store or self.app.store
        text = Text()
        text.append("\n")  # visual gap below border title

        content_w = self.content_size.width if self.content_size.width > 0 else 0
        configured_w = max(0, store.sidebar_width - 2)
        # Textual may expose the previous content size for one layout tick after a resize.
        row_width = min(content_w, configured_w) if content_w else configured_w
        has_focus = store.focused_pane == "sidebar"
        cursor = store.sidebar_cursor
        selected_id = store.sidebar_selected_id
        workflow_statuses = store.workflow_statuses
        frame = store.frame

        def _clip(label: str, used: int) -> str:
            """Truncate label so total line width stays within row_width."""
            avail = row_width - used
            if avail <= 0:
                return ""
            return label[:avail]

        for idx, item in enumerate(self._flat_items):
            indent = "  " + "  " * item.depth
            is_cursor = has_focus and idx == cursor

            if item.is_folder:
                is_expanded = item.folder_path in store.sidebar_expanded
                arrow = "▾" if is_expanded else "▸"
                prefix_str = f"{indent}{arrow} "
                label = _clip(f"{item.label}/", len(prefix_str))
                line = f"{prefix_str}{label}"
                pad = max(0, row_width - len(line))
                if is_cursor:
                    text.append(line, Style(color=ICE_FROST, bold=True) + _CURSOR_BG)
                    text.append(" " * pad, _CURSOR_BG)
                    text.append("\n")
                else:
                    text.append(prefix_str, Style(color=ICE_STEEL))
                    text.append(f"{label}\n", Style(color=ICE_FROST))
            else:
                p = item.workflow
                is_selected = p.selector == selected_id
                status = workflow_statuses.get(p.selector)

                if status is not None:
                    if status == RunStatus.RUNNING:
                        icon = SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
                    else:
                        icon = _STATUS_ICONS[status]
                    icon_style = _STATUS_STYLES[status]
                else:
                    icon = "·"
                    icon_style = Style(color=ICE_STEEL)

                prefix = f"{indent}  "
                # prefix + icon + space + name
                name = _clip(p.rendered_name, len(prefix) + 2)
                content_len = len(prefix) + 2 + len(name)
                pad = max(0, row_width - content_len)

                if is_selected:
                    bg = _SELECTED_BG
                    text.append("▌", Style(color=ICE_TEAL))
                    rest_prefix = prefix[1:]
                    text.append(rest_prefix, bg)
                    text.append(icon, icon_style + bg)
                    text.append(" ", bg)
                    text.append(name, Style(color=ICE_FROST, bold=True) + bg)
                    text.append(" " * pad, bg)
                    text.append("\n")
                elif is_cursor:
                    text.append(prefix, _CURSOR_BG)
                    text.append(icon, icon_style + _CURSOR_BG)
                    text.append(" ", _CURSOR_BG)
                    text.append(name, Style(color=ICE_FROST) + _CURSOR_BG)
                    text.append(" " * pad, _CURSOR_BG)
                    text.append("\n")
                else:
                    text.append(prefix)
                    text.append(icon, icon_style)
                    text.append(" ")
                    text.append(f"{name}\n", Style(color=ICE_FROST))

        return text

    # ── Keyboard navigation ────────────────────────────────────────────

    def cursor_up(self) -> None:
        self.app.store.sidebar_cursor_up()
        self.refresh()

    def cursor_down(self) -> None:
        self._rebuild_tree()
        self.app.store.sidebar_cursor_down(len(self._flat_items))
        self.refresh()

    def activate_cursor(self) -> None:
        """Enter/space on current cursor item."""
        self._rebuild_tree()
        store = self.app.store
        if not self._flat_items or store.sidebar_cursor >= len(self._flat_items):
            return
        item = self._flat_items[store.sidebar_cursor]
        if item.is_folder:
            store.sidebar_toggle_expand(item.folder_path)
            self.refresh()
        elif item.workflow:
            self.post_message(self.WorkflowSelected(workflow=item.workflow))
            self.refresh()

    # ── Border-drag resize ────────────────────────────────────────────

    def on_mouse_down(self, event: MouseDown) -> None:
        if event.x >= self.size.width - 1:
            self._dragging = True
            self.capture_mouse()
            event.stop()

    def on_mouse_move(self, event: MouseMove) -> None:
        if self._dragging:
            new_width = max(15, event.screen_x)
            self.styles.width = new_width
            store = self._test_store or self.app.store
            store.sidebar_width = new_width
            event.stop()

    def on_mouse_up(self, event: MouseUp) -> None:
        if self._dragging:
            self._dragging = False
            self.release_mouse()
            event.stop()

    def on_click(self, event) -> None:
        if self._dragging:
            return
        self._rebuild_tree()
        store = self.app.store
        store.focused_pane = "sidebar"
        row = event.y - 2  # skip border + leading empty line
        if 0 <= row < len(self._flat_items):
            store.sidebar_cursor = row
            item = self._flat_items[row]
            if item.is_folder:
                store.sidebar_toggle_expand(item.folder_path)
                self.refresh()
            elif item.workflow:
                self.post_message(self.WorkflowSelected(workflow=item.workflow))
                self.refresh()
