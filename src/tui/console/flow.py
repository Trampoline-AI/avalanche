"""Flow page (flow-detail.tsx): the DAG fills the area; the bottom panel's selection drives it.

Runs | Inputs & Outputs | Timeline | Artifacts | Deployments | Schedules. Selecting a run
(enter) shows its statuses on its own deployment's graph; enter again opens the run page.
Selecting a deployment shows that definition. `c` = "See current deployment".
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding

from .fmt import abbrev_id, newest_deployment, schedule_rows
from .graphpage import GraphPage
from .home import RUN_COLUMNS, run_rows, sort_runs
from .project import deployment_table_rows, schedule_table_rows
from .routes import FLOW_TABS, Route
from .widgets import Table


class FlowPage(GraphPage):
    TABS = FLOW_TABS
    BINDINGS = [Binding("c", "see_current", "Current deployment", show=False)]

    def _summaries(self):
        return self.store.ensure(("summaries", self.route.project, self.route.flow, None))

    def _selected_summary(self) -> dict | None:
        return next((r for r in self._summaries().items if r["run_id"] == self.route.run), None)

    def _newest(self) -> dict | None:
        return newest_deployment(self.store.ensure(("deployments", self.route.project)).items)

    def flow_key(self) -> tuple | None:
        route = self.route
        summary = self._selected_summary() if route.run else None
        newest = self._newest()
        deployment = (
            (summary or {}).get("deployment_id")
            or route.pick_deployment
            or route.deployment
            or (newest or {}).get("deployment_id")
        )
        return route.project, route.flow, deployment

    def caption(self) -> Text:
        pal, route = self.pal, self.route
        _, _, deployment = self.flow_key() or (None, None, None)
        newest = self._newest()
        current = newest is not None and deployment == newest["deployment_id"]
        out = Text("Showing: ", style=pal["muted"])
        if route.run:
            out.append(f"run {abbrev_id(route.run)}", f"bold {pal['ink']}")
            out.append(f" on {abbrev_id(deployment or '')}", pal["muted"])
        elif deployment:
            out.append(f"Deployment {abbrev_id(deployment)}", f"bold {pal['ink']}")
            out.append(" · Definition", pal["muted"])
            if current:
                out.append(" · Current", pal["accent"])
        else:
            out.append("no deployment yet", pal["muted"])
        if route.run or not current:
            out.append("    c see current deployment", pal["hairline"])
        return out

    def draw_tab(self, tab: str, table: Table):
        pal, route, store = self.pal, self.route, self.store
        if tab == "runs":
            res = self._summaries()
            empty = self.loading_text(res, "No runs of this flow yet.")
            if empty:
                return None, Text(empty, style=pal["muted"])
            table.set_rows(
                RUN_COLUMNS,
                run_rows(sort_runs(res.items), self.app.frame, pal, glyph=True),
                select=route.run,
            )
            note = Text(
                "enter show on the graph · enter again open the run   ", style=pal["hairline"]
            )
            return note + self.more_line(res), None
        if tab == "deployments":
            res = store.ensure(("deployments", route.project))
            empty = self.loading_text(res, "No deployments yet.")
            if empty:
                return None, Text(empty, style=pal["muted"])
            table.set_rows(
                ["Deployment", "", "Created", "Bundles", "Environment"],
                deployment_table_rows(res.items, self._newest(), pal),
                select=self.flow_key()[2],
            )
            return Text(
                "enter show this definition · Set current: not available yet (placeholder)",
                style=pal["hairline"],
            ), None
        if tab == "schedules":
            records = store.ensure(("schedules", route.project))
            flows, _, _ = store.project_flows(route.project)
            rows = schedule_rows(records.items, flows.items if flows else [], route.flow)
            if not rows:
                return None, Text(
                    self.loading_text(records, "No schedules for this flow.")
                    or "No schedules for this flow.",
                    style=pal["muted"],
                )
            table.set_rows(
                ["Flow", "Cron", "Timezone", "State", "Deployment", "Source"],
                schedule_table_rows(rows, pal),
            )
            return None, None
        return self.run_tab(tab, table)

    def open_row(self, key: str, meta: object) -> None:
        route, tab = self.route, self.route.active_tab
        if tab == "runs":
            if route.run == key:
                self.app.navigate(
                    Route(view="run", project=route.project, flow=route.flow, run=key)
                )
            else:
                self.app.navigate(
                    route.with_(run=key, pick_deployment=None, node=None), push=False
                )
        elif tab == "deployments":
            self.app.navigate(route.with_(pick_deployment=key, run=None, node=None), push=False)
        elif tab == "artifacts":
            self.app.download(route.project, route.run, meta)

    def action_see_current(self) -> None:
        self.app.navigate(
            self.route.with_(run=None, pick_deployment=None, deployment=None, node=None),
            push=False,
        )

    def animating(self) -> bool:
        running = {"admitted", "running", "resumable"}
        return super().animating() or (
            self.route.active_tab == "runs"
            and any(r["status"] in running for r in self._summaries().items)
        )

    def meta(self) -> Text:
        res = self._summaries()
        out = Text(f"{self.route.flow} · {len(res.items)} runs", style=self.pal["muted"])
        state = self.stream_state.get("run")
        if self.route.run and state and state != "live":
            out.append(f" · {state}", self.pal["waiting"])
        return out
