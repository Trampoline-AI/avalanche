"""Keyed resource cache (the console's react-query layer) on worker threads.

Loads run on a small bounded pool; completions are generation-fenced and delivered to the UI
thread. Paged resources keep every loaded page and the next opaque cursor (PagedResource).
No timers: data refreshes on route admission, project change notices, run events, or `R`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .fmt import newest_deployment
from .source import ConsoleSource, SourceError

Key = tuple


@dataclass
class Res:
    data: object = None
    items: list = field(default_factory=list)
    cursor: str | None = None
    error: str | None = None
    status: int | None = None
    loading: bool = False
    loaded: bool = False
    updated: float = 0.0
    gen: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        return not self.loaded and self.error is None


PAGED = {"orgs", "projects", "deployments", "flows", "schedules", "summaries"}


class Store:
    def __init__(self, api: ConsoleSource, notify: Callable[[Key], None], workers: int = 4):
        self.api = api
        self.notify = notify
        self.res: dict[Key, Res] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="delta-console")
        self.closed = False

    # ── public ───────────────────────────────────────────────────────────────
    def get(self, key: Key) -> Res:
        with self.lock:
            return self.res.setdefault(key, Res())

    def ensure(self, key: Key) -> Res:
        res = self.get(key)
        if not res.loaded and not res.loading and res.error is None:
            self._start(key, more=False)
        return res

    def reload(self, key: Key) -> None:
        """Refresh starts a new traversal; never re-reads old snapshot pages serially."""
        self._start(key, more=False, fresh=True)

    def more(self, key: Key) -> None:
        res = self.get(key)
        if res.cursor and not res.loading:
            self._start(key, more=True)

    def invalidate(self, predicate: Callable[[Key], bool]) -> None:
        with self.lock:
            keys = [
                key
                for key, res in self.res.items()
                if predicate(key) and (res.loaded or res.error)
            ]
        for key in keys:
            self.reload(key)

    def drop(self, predicate: Callable[[Key], bool]) -> None:
        with self.lock:
            for key in [key for key in self.res if predicate(key)]:
                self.res[key].gen += 1
                del self.res[key]

    def close(self) -> None:
        self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ── derived reads shared by several screens ─────────────────────────────
    def newest(self, project: str) -> tuple[Res, dict | None]:
        deployments = self.ensure(("deployments", project))
        return deployments, newest_deployment(deployments.items)

    def project_flows(
        self, project: str, deployment: str | None = None
    ) -> tuple[Res | None, str | None, bool]:
        """Flows of the given or newest deployment → (resource, deployment_id, pending)."""
        deployments, newest = self.newest(project)
        chosen = deployment or (newest["deployment_id"] if newest else None)
        if chosen is None:
            return None, None, not deployments.loaded and deployments.error is None
        flows = self.ensure(("flows", project, chosen))
        return flows, chosen, flows.pending

    # ── loading ──────────────────────────────────────────────────────────────
    def _start(self, key: Key, *, more: bool, fresh: bool = False) -> None:
        if self.closed:
            return
        with self.lock:
            res = self.res.setdefault(key, Res())
            if fresh:
                res.gen += 1
            elif res.loading:
                return
            res.loading = True
            gen, cursor = res.gen, res.cursor if more else None
        self.notify(key)
        try:
            self.pool.submit(self._run, key, gen, cursor, more)
        except RuntimeError:
            pass  # Pool shut down during exit.

    def _run(self, key: Key, gen: int, cursor: str | None, more: bool) -> None:
        try:
            result, error, status = self._load(key, cursor), None, None
        except SourceError as exc:
            result, error, status = None, str(exc), exc.status
        except Exception as exc:  # noqa: BLE001 - surfaced inline, never crashes the UI.
            result, error, status = None, f"{type(exc).__name__}: {exc}", None
        if self.closed:
            return
        with self.lock:
            res = self.res.get(key)
            if res is None or res.gen != gen:
                return  # Superseded selection/refresh: late results never render.
            res.loading = False
            if error is not None:
                if more and status in {409, 410}:
                    res.error, res.status = (
                        "List changed; refreshed from the first page.",
                        status,
                    )
                    res.cursor = None
                else:
                    res.error, res.status = error, status
            else:
                res.error, res.status = None, None
                if key[0] in PAGED:
                    items, next_cursor, extra = result
                    if cursor and next_cursor and next_cursor == cursor:
                        res.error = "API returned a repeated pagination cursor"
                        next_cursor = None
                    res.items = (res.items + items) if more else items
                    res.cursor = next_cursor
                    res.extra = extra if not more else {**res.extra, **extra}
                else:
                    res.data = result
                res.loaded = True
                res.updated = time.time()
        self.notify(key)
        if error is not None and more and status in {409, 410}:
            self.reload(key)

    def _load(self, key: Key, cursor: str | None):
        api, kind = self.api, key[0]
        if kind == "orgs":
            items, nxt = api.me_orgs(cursor)
            return items, nxt, {}
        if kind == "projects":
            items, nxt = api.projects(cursor)
            return items, nxt, {}
        if kind == "deployments":
            items, nxt = api.deployments(key[1], cursor)
            return items, nxt, {}
        if kind == "flows":
            items, nxt = api.flows(key[1], key[2], cursor)
            return items, nxt, {}
        if kind == "schedules":
            items, nxt = api.schedules(key[1], cursor)
            return items, nxt, {}
        if kind == "summaries":
            _, project, flow, deployment = key
            page = api.run_summaries(project, flow=flow, deployment=deployment, cursor=cursor)
            return (
                list(page["runs"]),
                page.get("cursor"),
                {"complete": page.get("complete", True), "projector": page.get("projector")},
            )
        if kind == "flow":
            return api.flow(key[1], key[2], key[3])
        if kind == "run":
            return api.run(key[1], key[2])
        if kind == "inputs":
            return api.run_inputs(key[1], key[2])
        if kind == "outputs":
            return api.run_outputs(key[1], key[2])
        if kind == "artifacts":
            return api.run_artifacts(key[1], key[2])
        if kind == "observation":
            return api.observation(key[1], key[2], key[3], key[4])
        if kind == "caps":
            return api.capabilities(key[1], key[2])
        raise ValueError(f"unknown resource {kind}")
