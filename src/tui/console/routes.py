"""Console routes (console-routes.ts): view + URL-state vocabulary, as an immutable value."""

from __future__ import annotations

from dataclasses import dataclass, replace

PROJECT_TABS = (
    ("flows", "Overview"),
    ("runs", "Runs"),
    ("schedules", "Schedules"),
    ("deployments", "Deployments"),
)
FLOW_TABS = (
    ("runs", "Runs"),
    ("io", "Inputs & Outputs"),
    ("timeline", "Timeline"),
    ("artifacts", "Artifacts"),
    ("deployments", "Deployments"),
    ("schedules", "Schedules"),
)
RUN_TABS = (("io", "Inputs & Outputs"), ("timeline", "Timeline"), ("artifacts", "Artifacts"))
ACTIVITY_TABS = (("runs", "Runs"), ("changes", "Changes"))


@dataclass(frozen=True)
class Route:
    view: str = "home"  # home | activity | project | flow | run
    project: str | None = None
    flow: str | None = None
    deployment: str | None = None  # Flow page: definition deployment (defaults to newest).
    run: str | None = None  # Run page: the run; flow page: the selected run (?run=).
    pick_deployment: str | None = None  # Flow page: selected deployment (?dpl=).
    tab: str | None = None
    status: str | None = None
    deployment_filter: str | None = None
    node: str | None = None

    def with_(self, **changes) -> Route:
        return replace(self, **changes)

    @property
    def key(self) -> tuple:
        """Route identity: tabs/filters/selection never remount streams or data owners."""
        return (
            self.view,
            self.project,
            self.flow if self.view == "flow" else None,
            self.run if self.view == "run" else None,
        )

    def default_tab(self) -> str:
        return {"project": "flows", "flow": "runs", "run": "io", "activity": "runs"}.get(
            self.view, ""
        )

    @property
    def active_tab(self) -> str:
        return self.tab or self.default_tab()
