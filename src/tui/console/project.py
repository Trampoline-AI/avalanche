"""Project page (console-views.tsx ProjectView): Overview | Runs | Schedules | Deployments.

Overview = fact row (newest deployment, flows, enabled schedules, runs now) + flows table with
per-flow run badges. Project change notices invalidate this project's resources.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult

from .fmt import (
    abbrev_id,
    format_iso,
    load_counts,
    matches_status,
    plural,
    schedule_rows,
    status_counts,
)
from .home import RUN_COLUMNS, filter_label, run_rows, sort_runs
from .page import Page, text_widget
from .routes import PROJECT_TABS, Route
from .widgets import Table


def schedule_table_rows(rows: list[dict], pal: dict) -> list:
    out = []
    for row in rows:
        state = Text(
            "enabled" if row["enabled"] else "disabled",
            style=pal["ink"] if row["enabled"] else pal["muted"],
        )
        source = "schedule" if row["source"] == "record" else "declared in flow"
        out.append(
            (
                row["key"],
                [
                    row["flow_slug"],
                    row["cron"],
                    row["timezone"] or "UTC",
                    state,
                    abbrev_id(row["deployment_id"]),
                    source,
                ],
                row,
            )
        )
    return out


def deployment_table_rows(deployments: list[dict], newest: dict | None, pal: dict) -> list:
    out = []
    for item in sorted(deployments, key=lambda d: d.get("created_at") or "", reverse=True):
        current = newest is not None and item["deployment_id"] == newest["deployment_id"]
        tag = Text("newest", style=pal["accent"]) if current else Text("")
        bundles = len(item.get("bundle_manifest_uris") or [])
        env = "pinned image" if item.get("environment") else "default"
        out.append(
            (
                item["deployment_id"],
                [
                    abbrev_id(item["deployment_id"]),
                    tag,
                    format_iso(item.get("created_at")) or "—",
                    f"{bundles} {plural(bundles, 'bundle')}",
                    env,
                ],
                item,
            )
        )
    return out


class ProjectPage(Page):
    TABS = PROJECT_TABS

    def compose(self) -> ComposeResult:
        yield text_widget("title", id="proj-title")
        yield text_widget("note", id="proj-facts")
        yield text_widget("tabs", id="proj-tabs")
        yield text_widget("note", id="proj-filter")
        yield Table(id="proj-table")
        yield text_widget("note", id="proj-more")

    def on_mount(self) -> None:
        project = self.route.project
        self.stream(
            "project",
            lambda cursor, stop: self.app.source.project_events(
                project, stop=stop, last_event_id=cursor
            ),
            self._on_change,
        )

    def _on_change(self, frame) -> None:
        if frame.event in {"ready", "heartbeat"}:
            return
        project = self.route.project
        self.store.invalidate(lambda key: len(key) > 1 and key[1] == project)

    def draw(self) -> None:
        pal, store, route = self.pal, self.store, self.route
        project = route.project
        deployments, newest = store.newest(project)
        flows, deployment, flows_pending = store.project_flows(project)
        schedules = store.ensure(("schedules", project))
        summaries = store.ensure(("summaries", project, None, None))
        name = next(
            (
                p.get("name")
                for p in store.ensure(("projects",)).items
                if p["proj_id"] == project
            ),
            None,
        ) or abbrev_id(project)
        self.query_one("#proj-title").update(self.title(name, project))
        enabled = sum(
            1
            for row in schedule_rows(schedules.items, flows.items if flows else [])
            if row["enabled"]
        )
        facts = Text()
        for label, value in (
            ("Newest deployment", abbrev_id(newest["deployment_id"]) if newest else "none yet"),
            ("Flows", "…" if flows_pending else str(len(flows.items)) if flows else "0"),
            ("Enabled schedules", str(enabled)),
        ):
            facts.append(label + " ", pal["muted"])
            facts.append(value + "    ", pal["ink"])
        facts.append("Runs now ", pal["muted"])
        facts.append_text(load_counts(summaries.items, pal))
        self.query_one("#proj-facts").update(facts)
        self.query_one("#proj-tabs").update(self.tab_strip("[ ] switch"))
        tab = route.active_tab
        table = self.query_one("#proj-table", Table)
        note = Text()
        if tab == "flows":
            resource, empty = (
                flows,
                self.loading_text(flows, "No deployment yet. Run `delta deploy`."),
            )
            if empty is None:
                rows = []
                for flow in flows.items:
                    runs = [
                        r for r in summaries.items if r.get("flow_slug") == flow["flow_slug"]
                    ]
                    trigger = (
                        "cron " + flow["cron"]
                        if flow.get("cron")
                        else "webhook"
                        if flow.get("webhook_enabled")
                        else "manual"
                    )
                    label = Text(flow["flow_slug"], style=f"bold {pal['ink']}")
                    if flow.get("workflow_name"):
                        label.append("\n" + flow["workflow_name"], style=pal["muted"])
                    rows.append(
                        (
                            flow["flow_slug"],
                            [
                                label,
                                str(len(flow.get("node_ids") or [])),
                                trigger,
                                status_counts(runs, pal),
                            ],
                            flow,
                        )
                    )
                table.set_rows(["Flow", "Steps", "Trigger", "Runs"], rows)
            note.append(
                f"Definitions from {abbrev_id(deployment)}" if deployment else "", pal["muted"]
            )
        elif tab == "runs":
            resource = summaries
            wanted_dpl = route.deployment_filter
            runs = sort_runs(
                [
                    r
                    for r in summaries.items
                    if matches_status(r["status"], route.status)
                    and (wanted_dpl is None or r.get("deployment_id") == wanted_dpl)
                ]
            )
            empty = self.loading_text(summaries, "No runs yet.")
            if empty is None and not runs:
                empty = f"No {filter_label(route.status).lower()} runs."
            if empty is None:
                table.set_rows(RUN_COLUMNS, run_rows(runs, self.app.frame, pal))
            note = Text.assemble(
                ("Status: ", pal["muted"]),
                (filter_label(route.status), pal["ink"]),
                ("   Deployment: ", pal["muted"]),
                (abbrev_id(wanted_dpl) if wanted_dpl else "All", pal["ink"]),
                ("   s status · d deployment", pal["hairline"]),
            )
        elif tab == "schedules":
            resource = schedules
            rows = schedule_rows(schedules.items, flows.items if flows else [])
            empty = self.loading_text(schedules, "No schedules.") if not rows else None
            if empty is None:
                table.set_rows(
                    ["Flow", "Cron", "Timezone", "State", "Deployment", "Source"],
                    schedule_table_rows(rows, pal),
                )
        else:
            resource = deployments
            empty = self.loading_text(deployments, "No deployments yet. Run `delta deploy`.")
            if empty is None:
                table.set_rows(
                    ["Deployment", "", "Created", "Bundles", "Environment"],
                    deployment_table_rows(deployments.items, newest, pal),
                )
            note.append(
                "Set current deployment: not available yet (placeholder, as in the console)",
                pal["muted"],
            )
        table.display = empty is None
        self.query_one("#proj-filter").update(note)
        more = self.more_line(resource)
        if tab == "runs" and summaries.extra.get("complete") is False:
            more.append("Some runs could not be listed.  ", pal["waiting"])
        self.query_one("#proj-more").update(
            Text(empty, style=pal["muted"]) + more if empty else more
        )

    def animating(self) -> bool:
        if self.route.active_tab != "runs":
            return False
        runs = self.store.get(("summaries", self.route.project, None, None)).items
        return any(r["status"] in {"admitted", "running", "resumable"} for r in runs)

    def open_row(self, key: str, meta: object) -> None:
        project, tab = self.route.project, self.route.active_tab
        if tab == "flows":
            self.app.navigate(Route(view="flow", project=project, flow=key))
        elif tab == "runs" and isinstance(meta, dict):
            self.app.navigate(
                Route(view="run", project=project, flow=meta.get("flow_slug"), run=key)
            )
        elif tab == "schedules" and isinstance(meta, dict):
            self.app.navigate(
                Route(view="flow", project=project, flow=meta["flow_slug"], tab="schedules")
            )
        elif tab == "deployments":
            self.app.navigate(
                Route(view="project", project=project, tab="runs", deployment_filter=key)
            )

    def meta(self) -> Text:
        res = self.store.get(("summaries", self.route.project, None, None))
        state = self.stream_state.get("project", "")
        out = Text(f"{abbrev_id(self.route.project)} · ", style=self.pal["muted"])
        out.append(f"{len(res.items)} {plural(len(res.items), 'run')}", self.pal["muted"])
        if state and state != "live":
            out.append(f" · {state}", self.pal["waiting"])
        return out
