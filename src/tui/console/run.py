"""Run page (durable-run-detail.tsx): the run's graph + Inputs & Outputs | Timeline | Artifacts.

Status badge and Cancel sit in the top-bar meta; the step drawer docks right (enter on a step).
Cancel is permission-gated and off in --read-only (the server re-authorizes).
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding

from .fmt import abbrev_id, status_badge
from .graphpage import GraphPage
from .routes import RUN_TABS
from .runview import TERMINAL
from .widgets import click


class RunPage(GraphPage):
    TABS = RUN_TABS
    BINDINGS = [Binding("x", "cancel", "Cancel run", show=False)]

    def _run_res(self):
        return self.store.ensure(("run", self.route.project, self.route.run))

    def flow_key(self) -> tuple | None:
        run = self.run_data() or {}
        slug = run.get("flow_slug") or self.route.flow
        return self.route.project, slug, run.get("deployment_id")

    def caption(self) -> Text:
        pal = self.pal
        res = self._run_res()
        if res.error:
            return Text(f"Run unavailable · {res.error}   R retry", style=pal["danger"])
        run = self.run_data()
        if run is None:
            return Text("Loading run…", style=pal["muted"])
        out = Text("Showing: ", style=pal["muted"])
        out.append(f"run {abbrev_id(run['run_id'])}", f"bold {pal['ink']}")
        out.append(f" on {abbrev_id(run.get('deployment_id') or '')}", pal["muted"])
        projector = (res.data or {}).get("projector") or {}
        if run.get("stale") or projector.get("caught_up") is False:
            out.append("   catching up…", pal["waiting"])
        return out

    def draw(self) -> None:
        run = self.run_data()
        if run and run.get("flow_slug") and self.route.flow != run["flow_slug"]:
            # Activity rows may lack the slug; fill the crumb without a history entry.
            self.app.route = self.route = self.route.with_(flow=run["flow_slug"])
        super().draw()

    def _cancel_state(self) -> tuple[bool, str]:
        run = self.run_data()
        if run is None:
            return False, "loading"
        if run.get("status") in TERMINAL:
            return False, "run already finished"
        return self.app.allowed("run.cancel", self.route.project, self.route.run)

    def meta(self) -> Text:
        pal = self.pal
        run = self.run_data()
        if run is None:
            return Text()
        out = status_badge(run.get("status"), pal)
        ok, why = self._cancel_state()
        if run.get("status") not in TERMINAL:
            out.append("   ")
            if ok:
                label = "[x Cancel]"
                out.append(label, f"bold {pal['danger']}")
                out.stylize(click("app.cancel_current"), len(out) - len(label), len(out))
            else:
                out.append(f"[Cancel · {why}]", pal["hairline"])
        state = self.stream_state.get("run")
        if state and state != "live":
            out.append(f"   {state}", pal["waiting"])
        return out

    def action_cancel(self) -> None:
        ok, why = self._cancel_state()
        if not ok:
            self.app.notify(f"Cancel unavailable: {why}", severity="warning")
            return
        self.app.cancel_run(self.route.project, self.route.run)

    def describe(self) -> str:
        base = super().describe()
        return (
            base or f"{self.route.run} · y copy · x cancel · enter on a step opens the drawer"
        )

    def focused_id(self) -> str | None:
        return super().focused_id() or self.route.run

    def open_row(self, key: str, meta: object) -> None:
        if self.route.active_tab == "artifacts":
            self.app.download(self.route.project, self.route.run, meta)
