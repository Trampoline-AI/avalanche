"""Console shell chrome: the 1-row top bar (breadcrumbs + meta) and the sidebar tree.

Ports console-shell.tsx: crumbs `Delta / org ▾ / project ▾ / flow ▾ / run_…` whose carets open
sibling pickers, and a sidebar with Projects, Activity, then a project tree whose caret expands
without selecting (space toggles, enter opens).
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.widgets import Static, Tree

from .fmt import abbrev_id
from .routes import Route
from .widgets import click


def project_name(store, project: str | None) -> str | None:
    if project is None:
        return None
    for item in store.get(("projects",)).items:
        if item["proj_id"] == project:
            return item.get("name") or project
    return abbrev_id(project)


class TopBar(Static):
    DEFAULT_CSS = """
    TopBar { height: 1; padding: 0 1; background: $background; border-bottom: none; }
    """

    def draw(self, app, meta: Text) -> None:
        pal, route, store = app.pal, app.route, app.store
        crumb = Style(color=pal["ink"])
        caret = Style(color=pal["muted"])
        sep = Text(" / ", style=pal["hairline"])
        out = Text(no_wrap=True, overflow="ellipsis")
        out.append("Delta", Style(color=pal["accent"], bold=True) + click("app.home"))
        out.append_text(sep)
        org = app.source.org
        named = next(
            (
                o.get("name")
                for o in store.ensure(("orgs",)).items
                if o.get("org_id") == org and o.get("name")
            ),
            None,
        )
        out.append(named or app.source.org_label(), crumb + click("app.pick('org')"))
        out.append(" ▾", caret + click("app.pick('org')"))
        if route.view == "activity":
            out.append_text(sep)
            out.append("Activity", crumb)
        if route.project:
            out.append_text(sep)
            out.append(
                project_name(store, route.project) or "", crumb + click("app.open_project")
            )
            out.append(" ▾", caret + click("app.pick('project')"))
        if route.flow and route.view in {"flow", "run"}:
            out.append_text(sep)
            out.append(route.flow, crumb + click("app.open_flow"))
            out.append(" ▾", caret + click("app.pick('flow')"))
        if route.view == "run" and route.run:
            out.append_text(sep)
            out.append(abbrev_id(route.run), Style(color=pal["ink"], bold=True))
        if meta:
            room = max(1, self.size.width - 2 - out.cell_len - meta.cell_len)
            out.append(" " * room)
            out.append_text(meta)
        self.update(out)


class Sidebar(Tree):
    """Projects, Activity, then the project tree; flows load when a project expands."""

    DEFAULT_CSS = """
    Sidebar { width: 30; height: 1fr; background: $background; padding: 0 1;
              border-right: solid $border; scrollbar-size: 1 1; }
    Sidebar.-hidden { display: none; }
    Sidebar > .tree--cursor { background: $block-cursor-background; color: $foreground; }
    Sidebar:focus > .tree--cursor { color: $primary; }
    Sidebar > .tree--guides { color: $border; }
    """

    def __init__(self) -> None:
        super().__init__("Delta", id="sidebar")
        self.show_root = False
        self.auto_expand = False
        self.guide_depth = 2
        self.projects: dict[str, object] = {}
        self.home_node = None
        self.activity_node = None

    def on_mount(self) -> None:
        self.home_node = self.root.add_leaf("Projects", data=Route())
        self.activity_node = self.root.add_leaf("Activity", data=Route(view="activity"))
        self.root.expand()

    def sync(self, app) -> None:
        store, route, pal = app.store, app.route, app.pal
        projects = store.ensure(("projects",))
        active = Style(color=pal["accent"], bold=True)
        plain = Style(color=pal["ink"])
        self.home_node.set_label(Text("Projects", active if route.view == "home" else plain))
        self.activity_node.set_label(
            Text("Activity", active if route.view == "activity" else plain)
        )
        for item in projects.items:
            pid = item["proj_id"]
            node = self.projects.get(pid)
            if node is None:
                node = self.root.add(
                    item.get("name") or pid,
                    data=Route(view="project", project=pid),
                    allow_expand=True,
                )
                self.projects[pid] = node
            selected = route.project == pid and route.view == "project"
            node.set_label(Text(item.get("name") or pid, active if selected else plain))
            if node.is_expanded:
                self._fill(node, pid, app)

    def _fill(self, node, pid: str, app) -> None:
        flows, deployment, pending = app.store.project_flows(pid)
        pal, route = app.pal, app.route
        if pending:
            wanted = [("Loading…", None)]
        elif flows is None:
            wanted = [("No deployment yet", None)]
        elif flows.error:
            wanted = [("Flows unavailable", None)]
        elif not flows.items:
            wanted = [("No flows deployed", None)]
        else:
            wanted = [
                (flow["flow_slug"], Route(view="flow", project=pid, flow=flow["flow_slug"]))
                for flow in flows.items
            ]
        current = [(str(child.label), child.data) for child in node.children]
        labels = [(label, data) for label, data in wanted]
        if [label for label, _ in current] != [label for label, _ in labels]:
            node.remove_children()
            for label, data in labels:
                node.add_leaf(label, data=data)
        for child in node.children:
            data = child.data
            on = (
                data is not None
                and route.view in {"flow", "run"}
                and route.project == pid
                and route.flow == data.flow
            )
            colour = pal["accent"] if on else (pal["ink"] if data is not None else pal["muted"])
            child.set_label(Text(str(child.label), Style(color=colour, bold=on)))

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        self.app.dirty = True

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        event.stop()
        if isinstance(event.node.data, Route):
            self.app.navigate(event.node.data)
