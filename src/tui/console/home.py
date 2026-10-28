"""Home (home-view.tsx) and Activity (activity-view.tsx) pages."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult

from .fmt import (
    STATUS_GROUPS,
    abbrev_id,
    format_duration,
    format_when,
    glyph_status,
    load_counts,
    matches_status,
    newest_deployment,
    plural,
    run_times,
    status_badge,
    status_counts,
)
from .page import Page, text_widget
from .routes import ACTIVITY_TABS, Route
from .widgets import Table

STATUS_FILTERS = (None, "active", "waiting", "failed", "succeeded", "cancelled")


def filter_label(value: str | None) -> str:
    if value is None:
        return "All statuses"
    group = STATUS_GROUPS.get(value)
    return group[0] if group else value.capitalize()


def run_rows(
    runs: list[dict],
    frame: int,
    pal: dict,
    *,
    show_project: dict | None = None,
    glyph: bool = False,
):
    """Runs table rows shared by Activity, Project → Runs and Flow → Runs.

    As in the console: Activity/Project tables use RunStatusBadge (succeeded = foreground);
    only the flow page's Runs tab uses RunGlyphStatus (animated glyph, succeeded = green).
    """
    rows = []
    for run in runs:
        start, duration = run_times(run)
        status = (
            glyph_status(run["status"], frame, pal)
            if glyph
            else status_badge(run["status"], pal)
        )
        cells = [status, abbrev_id(run["run_id"])]
        if show_project is not None:
            cells.append(
                show_project.get(run.get("project_id"), abbrev_id(run.get("project_id") or ""))
            )
        cells += [
            run.get("flow_slug") or "",
            abbrev_id(run.get("deployment_id") or ""),
            format_when(start) or "—",
            format_duration(duration) or "—",
        ]
        rows.append((run["run_id"], cells, run))
    return rows


RUN_COLUMNS = ["Status", "Run", "Flow", "Deployment", "Started", "Duration"]


def sort_runs(runs: list[dict]) -> list[dict]:
    return sorted(runs, key=lambda run: (-(run_times(run)[0] or 0), run["run_id"]))


class HomePage(Page):
    """Projects table: name + ID, flows, run badges, active/queued; capacity placeholder."""

    def compose(self) -> ComposeResult:
        yield text_widget("title", id="home-title")
        yield text_widget("note", id="home-capacity")
        yield Table(id="home-projects")
        yield text_widget("note", id="home-more")

    def draw(self) -> None:
        pal, store = self.pal, self.store
        projects = store.ensure(("projects",))
        self.query_one("#home-title").update(
            self.title(
                "Projects",
                f"{len(projects.items)} {plural(len(projects.items), 'project')}"
                "   a Activity",
            )
        )
        self.query_one("#home-capacity").update(
            Text(
                "Capacity · admission limits are not shown yet "
                "(placeholder, as in the console)",
                style=pal["muted"],
            )
        )
        rows = []
        for item in projects.items:
            pid = item["proj_id"]
            flows, _, pending = store.project_flows(pid)
            summaries = store.ensure(("summaries", pid, None, None))
            flow_count = "…" if pending else "—" if flows is None else str(len(flows.items))
            runs = summaries.items
            badges = (
                Text("…", style=pal["muted"])
                if summaries.pending
                else Text("unavailable", style=pal["danger"])
                if summaries.error and not runs
                else status_counts(runs, pal)
            )
            name = Text(item.get("name") or pid, style=f"bold {pal['ink']}")
            name.append("\n" + abbrev_id(pid), style=pal["muted"])
            rows.append((pid, [name, flow_count, badges, load_counts(runs, pal)], item))
        table = self.query_one("#home-projects", Table)
        empty = self.loading_text(
            projects, "No projects yet. Create one with `delta project create`."
        )
        table.display = empty is None
        if empty is None:
            table.set_rows(["Project", "Flows", "Runs", "Now"], rows)
        more = self.more_line(projects)
        self.query_one("#home-more").update(
            Text(empty or "", style=pal["muted"]) + more if empty else more
        )

    def open_row(self, key: str, meta: object) -> None:
        self.app.navigate(Route(view="project", project=key))

    def meta(self) -> Text:
        projects = self.store.get(("projects",))
        if not projects.items:
            return Text()
        deployed = sum(
            1
            for item in projects.items
            if newest_deployment(self.store.get(("deployments", item["proj_id"])).items)
        )
        return Text(
            f"{deployed} deployed · {self.app.updated_ago(projects)}", style=self.pal["muted"]
        )


class ActivityPage(Page):
    """Runs across projects (status filter, newest first, load more); Changes = placeholder."""

    TABS = ACTIVITY_TABS

    def compose(self) -> ComposeResult:
        yield text_widget("title", id="act-title")
        yield text_widget("tabs", id="act-tabs")
        yield text_widget("note", id="act-filter")
        yield Table(id="act-runs")
        yield text_widget("note", id="act-more")

    def draw(self) -> None:
        pal, store, route = self.pal, self.store, self.route
        self.query_one("#act-title").update(
            self.title("Activity", "Runs and changes across projects")
        )
        self.query_one("#act-tabs").update(self.tab_strip("[ ] switch"))
        table = self.query_one("#act-runs", Table)
        if route.active_tab == "changes":
            table.display = False
            self.query_one("#act-filter").update(
                Text(
                    "Change history is not available yet (placeholder, as in the console).",
                    style=pal["muted"],
                )
            )
            self.query_one("#act-more").update("")
            return
        summaries = store.ensure(("summaries", None, None, None))
        names = {
            item["proj_id"]: item.get("name") or abbrev_id(item["proj_id"])
            for item in store.ensure(("projects",)).items
        }
        runs = sort_runs(
            [run for run in summaries.items if matches_status(run["status"], route.status)]
        )
        self.query_one("#act-filter").update(
            Text.assemble(
                ("Status: ", pal["muted"]),
                (filter_label(route.status), pal["ink"]),
                ("   s change", pal["hairline"]),
            )
        )
        empty = self.loading_text(summaries, "No runs yet.")
        if empty is None and not runs:
            empty = f"No {filter_label(route.status).lower()} runs."
        table.display = empty is None
        if empty is None:
            columns = RUN_COLUMNS[:2] + ["Project"] + RUN_COLUMNS[2:]
            table.set_rows(columns, run_rows(runs, self.app.frame, pal, show_project=names))
        more = self.more_line(summaries)
        if summaries.extra.get("complete") is False:
            more.append("Some runs could not be listed.  ", pal["waiting"])
        self.query_one("#act-more").update(
            Text(empty, style=pal["muted"]) + more if empty else more
        )

    def animating(self) -> bool:
        runs = self.store.get(("summaries", None, None, None)).items
        return any(run["status"] in STATUS_GROUPS["active"][1] for run in runs)

    def open_row(self, key: str, meta: object) -> None:
        run = meta if isinstance(meta, dict) else {}
        self.app.navigate(
            Route(view="run", project=run.get("project_id"), flow=run.get("flow_slug"), run=key)
        )

    def meta(self) -> Text:
        res = self.store.get(("summaries", None, None, None))
        return (
            Text(
                f"{len(res.items)} {plural(len(res.items), 'run')} · "
                f"{self.app.updated_ago(res)}",
                style=self.pal["muted"],
            )
            if res.loaded
            else Text()
        )


def next_status(current: str | None) -> str | None:
    index = STATUS_FILTERS.index(current) if current in STATUS_FILTERS else 0
    return STATUS_FILTERS[(index + 1) % len(STATUS_FILTERS)]
