"""Persistent, explicitly loaded provider navigation; no polling or backend actions."""

from __future__ import annotations

from dataclasses import dataclass, replace

from rich.text import Text
from textual.widgets import Tree
from textual.widgets.tree import TreeNode

from ..state import (
    NavigationContribution,
    NavigationNode,
    NavigationPage,
    TuiContext,
    TuiExtensionError,
)


@dataclass(frozen=True)
class NavigationEntry:
    node: NavigationNode | None = None
    cursor: str | None = None
    retry: bool = False
    revision: str | None = None


class NavigationTree(Tree[NavigationEntry]):
    """Page replacement keeps each branch bounded; the whole tree caps at 2,000 nodes."""

    DEFAULT_CSS = """
    NavigationTree { width: 36; max-width: 45%; height: 100%; border: solid $accent; }
    """

    BINDINGS = [
        ("left", "navigation_parent", "Collapse / parent"),
        ("right", "navigation_children", "Expand children"),
    ]

    def action_navigation_parent(self) -> None:
        node = self.cursor_node
        if node is not None:
            if node.is_expanded:
                node.collapse()
            elif node.parent is not None:
                self.move_cursor(node.parent)

    def action_navigation_children(self) -> None:
        node = self.cursor_node
        if node is not None:
            node.expand()

    def __init__(self) -> None:
        super().__init__("Navigation · h Home", id="extension-navigation")
        self.border_title = "Navigation · h Home"
        self.show_root = False
        self.auto_expand = False
        self._contribution: NavigationContribution | None = None
        self._selection_context: TuiContext | None = None
        self._loading: set[int] = set()
        self.display = False

    def sync(self, contribution: NavigationContribution, context: TuiContext) -> None:
        if context != self._selection_context:
            self._loading.clear()
            self._selection_context = context
        if self._contribution is None or self._contribution.nodes != contribution.nodes:
            self.clear()
            self._loading.clear()
            self._add(self.root, contribution.nodes)
            self.root.expand()
        self._contribution = contribution
        self.display = True

    def _add(
        self, parent: TreeNode[NavigationEntry], nodes: tuple[NavigationNode, ...]
    ) -> None:
        for node in nodes:
            label = Text(node.label)
            if node.badge is not None:
                label.append(f" · {node.badge}")
            child = parent.add(
                label, NavigationEntry(node), allow_expand=bool(node.children) or node.lazy
            )
            self._add(child, node.children)
            if node.expanded:
                child.expand()

    def on_tree_node_selected(self, event: Tree.NodeSelected[NavigationEntry]) -> None:
        event.stop()
        entry = event.node.data
        if entry is None:
            return
        if entry.node is None:
            if entry.cursor is not None or entry.retry:
                self.load_children(event.node.parent, entry.cursor)
            return
        if entry.node.target is not None:
            self.app.navigate(entry.node.target)
        if entry.node.lazy and not event.node.children:
            self.load_children(event.node)
        event.node.expand()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[NavigationEntry]) -> None:
        entry = event.node.data
        if (
            entry is not None
            and entry.node is not None
            and entry.node.lazy
            and not event.node.children
        ):
            self.load_children(event.node)

    def load_children(
        self, parent: TreeNode[NavigationEntry], cursor: str | None = None
    ) -> None:
        entry = parent.data
        if entry is None or entry.node is None or parent.id in self._loading:
            return
        context = self.app.extension_context
        self._loading.add(parent.id)
        node_id = entry.node.id

        def clear() -> None:
            for child in tuple(parent.children):
                child.remove()

        def fail(error: TuiExtensionError) -> None:
            self._loading.discard(parent.id)
            if (
                not self.is_mounted
                or context != self.app.extension_context
                or self._tree_nodes.get(parent.id) is not parent
            ):
                return
            clear()
            parent.add_leaf(Text(f"{error.code}: {error} · Retry"), NavigationEntry(retry=True))
            parent.expand()

        def accept(page: NavigationPage) -> None:
            self._loading.discard(parent.id)
            if (
                not self.is_mounted
                or context != self.app.extension_context
                or self._tree_nodes.get(parent.id) is not parent
            ):
                return
            if cursor is not None and entry.revision != page.revision:
                fail(TuiExtensionError("cursor_expired", "Children changed; reload first page"))
                return
            # Bound accumulated lazy depth as well as individual provider pages.
            depth = 0
            ancestor = parent
            while ancestor.parent is not None:
                depth += 1
                ancestor = ancestor.parent
            pending = [(node, depth) for node in page.nodes]
            while pending:
                node, node_depth = pending.pop()
                if node_depth > 32:
                    fail(TuiExtensionError("integrity", "Navigation tree exceeds depth 32"))
                    return
                pending.extend((child, node_depth + 1) for child in node.children)
            # Bound accumulated lazy branches as well as individual provider pages.
            if len(self._tree_nodes) + 100 > 2000:
                fail(
                    TuiExtensionError(
                        "integrity", "Tree window full; reselect a narrower scope"
                    )
                )
                return
            parent.data = replace(entry, revision=page.revision)
            clear()
            self._add(parent, page.nodes)
            if page.truncated:
                parent.add_leaf(Text("Unknown / incomplete children"))
            if page.next_cursor is not None:
                parent.add_leaf(Text("More…"), NavigationEntry(cursor=page.next_cursor))
            parent.expand()

        if not self.app.extension_session.read_navigation(node_id, cursor, accept, fail):
            fail(TuiExtensionError("unavailable", "Provider busy; retry when ready"))
