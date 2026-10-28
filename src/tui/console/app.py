"""Console app shell (console-shell.tsx): top bar, sidebar tree, routed page, status line.

Pages live in their own modules and register in `PAGES`. One shared clock animates run glyphs
and flushes redraws; data never polls (store loads on admission, events, or `R`).
"""

from __future__ import annotations

import base64
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from .chrome import Sidebar, TopBar, project_name
from .flow import FlowPage
from .fmt import relative_age
from .home import ActivityPage, HomePage, next_status
from .page import Page
from .project import ProjectPage
from .routes import Route
from .run import RunPage
from .source import ConsoleSource
from .store import Res, Store
from .theme import THEMES, palette
from .widgets import Confirm, Help, Picker

PAGES: dict[str, type[Page]] = {
    "home": HomePage,
    "activity": ActivityPage,
    "project": ProjectPage,
    "flow": FlowPage,
    "run": RunPage,
}


class StatusLine(Static):
    DEFAULT_CSS = (
        "StatusLine { height: 1; padding: 0 1; background: $background; color: $text-muted; }"
    )


class ConsoleApp(App):
    TITLE = "Delta"
    CSS = """
    Screen { background: $background; }
    #body { height: 1fr; }
    #main { width: 1fr; height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Keys"),
        Binding("h", "home", "Projects", show=False),
        Binding("a", "activity", "Activity", show=False),
        Binding("backspace", "back", "Back", show=False),
        Binding("ctrl+b", "sidebar", show=False),
        Binding("t", "theme", show=False),
        Binding("y", "copy", show=False),
        Binding("R", "refresh", show=False),
        Binding("m", "more", show=False),
        Binding("s", "status_filter", show=False),
        Binding("d", "deployment_filter", show=False),
        Binding("o", "pick('org')", show=False),
        Binding("p", "pick('project')", show=False),
        Binding("w", "pick('flow')", show=False),
        Binding("left_square_bracket", "tab_step(-1)", show=False),
        Binding("right_square_bracket", "tab_step(1)", show=False),
        *[Binding(str(n), f"tab_index({n - 1})", show=False) for n in range(1, 7)],
    ]

    def __init__(
        self, source: ConsoleSource, *, route: Route | None = None, mode: str = "dark"
    ):
        super().__init__()
        self.source = source
        self.mode = mode
        self.pal = palette(mode)
        self.route = route or Route()
        self.history: list[Route] = []
        self.frame = 0
        self.dirty = True
        self.store = Store(source, self._notify)
        self.page: Page | None = None
        for theme in THEMES.values():
            self.register_theme(theme)
        self.theme = THEMES[mode].name

    # ── layout ───────────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="body"):
            yield Sidebar()
            yield Vertical(id="main")
        yield StatusLine(id="status")

    NARROW = 110  # Below this many columns the sidebar starts hidden (ctrl+b shows it).

    def on_mount(self) -> None:
        self._sidebar_auto(self.size.width)
        self._mount_page()
        self.set_interval(0.15, self._tick)

    def _notify(self, key) -> None:
        self.dirty = True  # Called from worker threads; the clock flushes on the UI thread.

    def _tick(self) -> None:
        animating = self.page is not None and self.page.animating()
        if animating:
            self.frame += 1
        if self.dirty or animating:
            self.dirty = False
            self.redraw()

    def redraw(self) -> None:
        page = self.page
        if page is None or not page.is_mounted:
            return
        page.draw()
        self.query_one(TopBar).draw(self, page.meta())
        self.query_one(Sidebar).sync(self)
        self.query_one(StatusLine).update(Text(page.describe(), style=self.pal["muted"]))

    def _mount_page(self) -> None:
        main = self.query_one("#main")
        if self.page is not None:
            self.page.remove()
        self.page = PAGES.get(self.route.view, HomePage)(self.route)
        main.mount(self.page)
        self.call_after_refresh(self._after_mount)

    def _after_mount(self) -> None:
        self.redraw()
        tables = [table for table in self.page.query("Table")] if self.page else []
        if tables:
            tables[0].focus()

    # ── routing ──────────────────────────────────────────────────────────────
    def navigate(self, route: Route, *, push: bool = True) -> None:
        if route == self.route:
            return
        if push:
            self.history.append(self.route)
        same_page = route.key == self.route.key and self.page is not None
        self.route = route
        if same_page:
            self.page.set_route(route)
        else:
            self._mount_page()
        self.dirty = True

    def now(self) -> float:
        """Wall clock, or the source's own (fixture sources pin it for stable screenshots)."""
        clock = getattr(self.source, "now", None)
        return clock() if callable(clock) else time.time()

    def updated_ago(self, res: Res) -> str:
        if not res.updated:
            return ""
        return "updated " + relative_age(res.updated * 1000, time.time() * 1000)

    # ── actions ──────────────────────────────────────────────────────────────
    def action_home(self) -> None:
        self.navigate(Route())

    def action_activity(self) -> None:
        self.navigate(Route(view="activity"))

    def action_back(self) -> None:
        if self.history:
            self.navigate(self.history.pop(), push=False)

    def action_open_project(self) -> None:
        if self.route.project:
            self.navigate(Route(view="project", project=self.route.project))

    def action_open_flow(self) -> None:
        if self.route.project and self.route.flow:
            self.navigate(Route(view="flow", project=self.route.project, flow=self.route.flow))

    def action_tab(self, value: str) -> None:
        self.navigate(self.route.with_(tab=value), push=False)

    def _tabs(self) -> tuple:
        return self.page.TABS if self.page else ()

    def action_tab_step(self, step: int) -> None:
        tabs = [value for value, _ in self._tabs()]
        if tabs:
            index = tabs.index(self.route.active_tab) if self.route.active_tab in tabs else 0
            self.action_tab(tabs[(index + step) % len(tabs)])

    def action_tab_index(self, index: int) -> None:
        tabs = self._tabs()
        if 0 <= index < len(tabs):
            self.action_tab(tabs[index][0])

    def action_status_filter(self) -> None:
        self.navigate(self.route.with_(status=next_status(self.route.status)), push=False)

    def action_deployment_filter(self) -> None:
        """Cycle the Runs deployment filter: All → each deployment, newest first."""
        if not self.route.project:
            return
        deployments = self.store.ensure(("deployments", self.route.project)).items
        ids = [None] + [
            d["deployment_id"]
            for d in sorted(deployments, key=lambda d: d.get("created_at") or "", reverse=True)
        ]
        current = self.route.deployment_filter
        index = ids.index(current) if current in ids else 0
        self.navigate(
            self.route.with_(deployment_filter=ids[(index + 1) % len(ids)]), push=False
        )

    def action_sidebar(self) -> None:
        self._sidebar_pinned = True  # An explicit toggle wins over the width rule.
        self.query_one(Sidebar).toggle_class("-hidden")

    def _sidebar_auto(self, width: int) -> None:
        if not getattr(self, "_sidebar_pinned", False):
            self.query_one(Sidebar).set_class(0 < width < self.NARROW, "-hidden")

    def on_resize(self, event) -> None:
        self._sidebar_auto(event.size.width)
        self.dirty = True

    def action_theme(self) -> None:
        self.mode = "light" if self.mode == "dark" else "dark"
        self.pal = palette(self.mode)
        self.theme = THEMES[self.mode].name
        self.dirty = True

    def action_help(self) -> None:
        self.push_screen(Help())

    def action_refresh(self) -> None:
        self.store.invalidate(lambda key: True)

    def action_more(self) -> None:
        for key in list(self.store.res):
            if self.store.get(key).cursor:
                self.store.more(key)

    def action_copy(self) -> None:
        """C4: copy via OSC 52 (iTerm2/kitty/WezTerm; tmux needs `set-clipboard on`)."""
        ident = self.page.focused_id() if self.page else None
        if not ident:
            return
        payload = base64.b64encode(ident.encode()).decode()
        self._driver and self._driver.write(f"\x1b]52;c;{payload}\x07")
        self.notify(f"Copied {ident}", timeout=2)

    def action_pick(self, kind: str) -> None:
        store, route = self.store, self.route
        if kind == "org":
            items = [
                (o["org_id"], o.get("name") or o["org_id"], o["org_id"])
                for o in store.ensure(("orgs",)).items
            ]
            self.push_screen(Picker("Organization", items, self.source.org), self._switch_org)
        elif kind == "project":
            items = [
                (p["proj_id"], p.get("name") or p["proj_id"], "")
                for p in store.ensure(("projects",)).items
            ]
            self.push_screen(
                Picker("Project", items, route.project),
                lambda pid: pid and self.navigate(Route(view="project", project=pid)),
            )
        elif kind == "flow" and route.project:
            flows, _, _ = store.project_flows(route.project)
            items = [
                (f["flow_slug"], f.get("workflow_name") or f["flow_slug"], "")
                for f in (flows.items if flows else [])
            ]
            self.push_screen(
                Picker("Flow", items, route.flow),
                lambda slug: slug
                and self.navigate(Route(view="flow", project=route.project, flow=slug)),
            )

    def _switch_org(self, org: str | None) -> None:
        if not org or org == self.source.org:
            return
        label = next(
            (o.get("name") for o in self.store.get(("orgs",)).items if o["org_id"] == org), None
        )
        self.source.set_org(org, label)
        self.store.drop(lambda key: key[0] != "orgs")
        sidebar = self.query_one(Sidebar)
        for node in list(sidebar.projects.values()):
            node.remove()
        sidebar.projects.clear()
        self.history.clear()
        self.route = Route(view="home", project=None)
        self._mount_page()

    # ── actions on runs (C3: the console's; permission-gated; off in --read-only) ──
    def allowed(self, action: str, project: str, run: str | None) -> tuple[bool, str]:
        if self.source.read_only:
            return False, "read-only"
        caps = self.store.ensure(("caps", project, run))
        if not caps.loaded:
            return False, "checking permissions…"
        entry = ((caps.data or {}).get("actions") or {}).get(action) or {}
        if not entry.get("supported", False):
            return False, "not supported by this API"
        if not entry.get("allowed", False):
            return False, f"not permitted ({entry.get('reason') or 'forbidden'})"
        return True, ""

    def download(self, project: str | None, run: str | None, meta: object) -> None:
        if not (project and run and isinstance(meta, dict)):
            return
        ok, why = self.allowed("artifact.download", project, run)
        if not ok:
            self.notify(f"Download unavailable: {why}", severity="warning")
            return
        import os

        destination = os.path.join(os.getcwd(), os.path.basename(meta["path"]))

        def work() -> None:
            try:
                result = self.source.download(
                    project, run, meta["binding"], meta["path"], destination
                )
                self.call_from_thread(
                    self.notify,
                    f"Saved {destination} · {result.get('size_bytes')} "
                    f"bytes · sha256 {str(result.get('sha256'))[:12]}… verified",
                )
            except Exception as error:  # noqa: BLE001 - shown to the operator.
                self.call_from_thread(
                    self.notify, f"Download failed: {error}", severity="error"
                )

        self.store.pool.submit(work)

    def action_cancel_current(self) -> None:
        if isinstance(self.page, RunPage):
            self.page.action_cancel()

    def cancel_run(self, project: str, run: str) -> None:
        ok, why = self.allowed("run.cancel", project, run)
        if not ok:
            self.notify(f"Cancel unavailable: {why}", severity="warning")
            return

        def confirmed(yes: bool | None) -> None:
            if not yes:
                return

            def work() -> None:
                try:
                    result, cmd = self.source.cancel(project, run)
                    self.call_from_thread(
                        self.notify, f"Cancel {result.get('outcome')} · {cmd}"
                    )
                    self.store.invalidate(
                        lambda key: len(key) > 1 and key[1] in {project, None}
                    )
                except Exception as error:  # noqa: BLE001 - shown to the operator.
                    self.call_from_thread(
                        self.notify, f"Cancel failed: {error}", severity="error"
                    )

            self.store.pool.submit(work)

        self.push_screen(
            Confirm(f"Cancel run {run}? Running steps stop; this cannot be undone."), confirmed
        )

    async def action_quit(self) -> None:
        self.store.close()
        self.source.close()
        self.exit()

    def crumb_name(self) -> str | None:
        return project_name(self.store, self.route.project)
