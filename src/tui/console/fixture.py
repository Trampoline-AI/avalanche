"""In-memory `ConsoleSource` seeded like the console's design-review mock (dev, screenshots).

Deterministic data: three projects (two deployed, one empty), two deployments, mixed run
statuses including one running run whose events arrive on its stream. GET-only except cancel.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

ORG = "org_2kq9DemoOrg0001"
NOW = 1_793_116_800.0  # Fixed clock so screenshots are stable.


@dataclass
class _Frame:
    event: str
    id: str | None
    data: dict


def _iso(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - seconds_ago))


def _flow(slug, name, deployment, nodes, graph, *, cron=None, webhook=False, agents=()):
    return {
        "flow_slug": slug,
        "workflow_name": name,
        "deployment_id": deployment,
        "flow_id": f"flow_{slug[:4]}{deployment[-8:]}",
        "node_ids": list(nodes),
        "graph": graph,
        "node_types": {node: ("agent" if node in agents else "step") for node in nodes},
        "display_names": {},
        "agent_nodes": {node: {"model": "openai/gpt-5-mini"} for node in agents},
        "cron": cron,
        "webhook_enabled": webhook,
        "definition_file": f"flows/{slug}.py",
        "node_docs": {nodes[0]: "Reads the inbound batch and normalizes it."},
        "node_sources": {
            nodes[0]: f"@step\nasync def {nodes[0]}(batch):\n    return normalize(batch)\n"
        },
    }


P1, P2, P3 = "proj_7d2c9f0a1b3e4c55", "proj_c41e88aa02d94f10", "proj_0b9a77e1c2d34e8f"
D1_OLD, D1 = "dpl_51f0c2a9e8b74d01", "dpl_9a3e7b21c04f4e62"
D2 = "dpl_e2b8d4f6a1c94a73"

PROJECTS = [
    {"proj_id": P1, "name": "Invoice pipeline", "created_at": _iso(9 * 86400)},
    {"proj_id": P2, "name": "Support triage", "created_at": _iso(4 * 86400)},
    {"proj_id": P3, "name": "Sandbox", "created_at": _iso(3600)},
]
DEPLOYMENTS = {
    P1: [
        {
            "deployment_id": D1_OLD,
            "proj_id": P1,
            "created_at": _iso(5 * 86400),
            "bundle_manifest_uris": ["gs://b/1"],
            "environment": None,
        },
        {
            "deployment_id": D1,
            "proj_id": P1,
            "created_at": _iso(7200),
            "bundle_manifest_uris": ["gs://b/2", "gs://b/3"],
            "environment": {"image": "pinned"},
        },
    ],
    P2: [
        {
            "deployment_id": D2,
            "proj_id": P2,
            "created_at": _iso(86400),
            "bundle_manifest_uris": ["gs://b/4"],
            "environment": None,
        }
    ],
    P3: [],
}
INGEST = ("fetch_batch", "parse_lines", "match_vendor", "review_agent", "post_ledger")
INGEST_GRAPH = {
    "fetch_batch": ["parse_lines"],
    "parse_lines": ["match_vendor", "review_agent"],
    "match_vendor": ["post_ledger"],
    "review_agent": ["post_ledger"],
    "post_ledger": [],
}
FLOWS = {
    (P1, D1): [
        _flow(
            "ingest_invoices",
            "Ingest invoices",
            D1,
            INGEST,
            INGEST_GRAPH,
            cron="0 * * * *",
            agents=("review_agent",),
        ),
        _flow(
            "reconcile",
            "Reconcile accounts",
            D1,
            ("load", "diff", "report"),
            {"load": ["diff"], "diff": ["report"], "report": []},
            webhook=True,
        ),
    ],
    (P1, D1_OLD): [
        _flow(
            "ingest_invoices",
            "Ingest invoices",
            D1_OLD,
            INGEST[:3] + ("post_ledger",),
            {
                "fetch_batch": ["parse_lines"],
                "parse_lines": ["match_vendor"],
                "match_vendor": ["post_ledger"],
                "post_ledger": [],
            },
            cron="0 * * * *",
        ),
    ],
    (P2, D2): [
        _flow(
            "triage",
            "Triage tickets",
            D2,
            ("classify", "route"),
            {"classify": ["route"], "route": []},
            agents=("classify",),
        ),
    ],
}
SCHEDULES = {
    P1: [
        {
            "schedule_id": "sch_3c1d9e0f7a2b4c88",
            "flow_slug": "reconcile",
            "cron": "30 2 * * *",
            "timezone": "America/Toronto",
            "enabled": False,
            "deployment_id": D1,
            "updated_at": _iso(3 * 86400),
        }
    ],
    P2: [],
    P3: [],
}


def _run(rid, project, flow, deployment, status, started_ago, duration, counts):
    base = (NOW - started_ago) * 1000
    finished = None if duration is None else duration * 1000
    return {
        "run_id": rid,
        "project_id": project,
        "flow_slug": flow,
        "deployment_id": deployment,
        "status": status,
        "node_counts": counts,
        "times": [base, 0, 0, 0, finished],
    }


RUNS = [
    _run(
        "run_f3a9c21b7d0e4a6f9b12",
        P1,
        "ingest_invoices",
        D1,
        "running",
        95,
        None,
        [0, 1, 1, 1, 2, 0, 0, 0, 0],
    ),
    _run(
        "run_a17c0e93b24d4f81a5c3",
        P1,
        "ingest_invoices",
        D1,
        "failed",
        1900,
        41,
        [0, 0, 0, 0, 3, 0, 1, 1, 0],
    ),
    _run(
        "run_09be44d1c6a24e7f8d20",
        P1,
        "ingest_invoices",
        D1_OLD,
        "succeeded",
        86000,
        63,
        [0, 0, 0, 0, 4, 0, 0, 0, 0],
    ),
    _run(
        "run_5d2e8f01a9c34b6e7f44",
        P1,
        "reconcile",
        D1,
        "queued",
        20,
        None,
        [0, 3, 0, 0, 0, 0, 0, 0, 0],
    ),
    _run(
        "run_77c1d0e2f3a44b5c6d81",
        P1,
        "reconcile",
        D1,
        "succeeded",
        5400,
        12,
        [0, 0, 0, 0, 3, 0, 0, 0, 0],
    ),
    _run(
        "run_b8e2f4a6c1d34e9fa011",
        P2,
        "triage",
        D2,
        "succeeded",
        7300,
        8,
        [0, 0, 0, 0, 2, 0, 0, 0, 0],
    ),
    _run(
        "run_c90d1e2f3a4b4c5d6e7f",
        P2,
        "triage",
        D2,
        "cancelled",
        9000,
        30,
        [0, 0, 0, 0, 1, 0, 0, 1, 0],
    ),
]
NODE_STATE = {
    "run_f3a9c21b7d0e4a6f9b12": {
        "fetch_batch": "accepted",
        "parse_lines": "accepted",
        "match_vendor": "running",
        "review_agent": "ready",
        "post_ledger": "intended",
    },
    "run_a17c0e93b24d4f81a5c3": {
        "fetch_batch": "accepted",
        "parse_lines": "accepted",
        "match_vendor": "failed",
        "review_agent": "accepted",
        "post_ledger": "cancelled",
    },
}


class FixtureSource:
    def __init__(self, *, read_only: bool = False, journal: list | None = None):
        self.org = ORG
        self.read_only = read_only
        self.closed = threading.Event()
        self.journal = journal
        self.cancelled: set[str] = set()

    def _log(self, path: str) -> None:
        if self.journal is not None:
            self.journal.append(("GET", path))

    def now(self) -> float:
        """The fixture's fixed clock: running durations stay stable in screenshots."""
        return NOW

    def org_label(self) -> str:
        return "Demo Org"

    def set_org(self, org, label=None):
        self.org = org

    def me_orgs(self, cursor=None):
        return [
            {"org_id": ORG, "name": "Demo Org"},
            {"org_id": "org_2kq9OtherOrg002", "name": "Other Org"},
        ], None

    def projects(self, cursor=None):
        self._log("projects")
        return list(PROJECTS), None

    def deployments(self, project, cursor=None):
        return list(DEPLOYMENTS.get(project, [])), None

    def flows(self, project, deployment, cursor=None):
        return list(FLOWS.get((project, deployment), [])), None

    def flow(self, project, slug, deployment):
        for flow in FLOWS.get((project, deployment), []):
            if flow["flow_slug"] == slug:
                return flow
        from .source import SourceError

        raise SourceError("HTTP 404 · flow_not_found", 404, "flow_not_found")

    def schedules(self, project, cursor=None):
        return list(SCHEDULES.get(project, [])), None

    def run_summaries(self, project=None, *, flow=None, deployment=None, cursor=None):
        rows = [
            dict(run, status="cancelled" if run["run_id"] in self.cancelled else run["status"])
            for run in RUNS
            if (project is None or run["project_id"] == project)
            and (flow is None or run["flow_slug"] == flow)
            and (deployment is None or run["deployment_id"] == deployment)
        ]
        return {"runs": rows, "cursor": None, "complete": True, "projector": {"state": "live"}}

    def _summary(self, run):
        return next(item for item in RUNS if item["run_id"] == run)

    def run(self, project, run):
        summary = self._summary(run)
        flow = self.flow(project, summary["flow_slug"], summary["deployment_id"])
        states = NODE_STATE.get(run) or {node: "accepted" for node in flow["node_ids"]}
        start = summary["times"][0] / 1000
        nodes = {}
        for index, inv in enumerate(flow["node_ids"]):
            state = states.get(inv, "intended")
            attempts = []
            if state in {"accepted", "failed", "running"}:
                began = start + index * 6
                attempt = {
                    "g": 1,
                    "started_at": began,
                    "auth": f"auth_{inv}_1",
                    "ended_at": None if state == "running" else began + 5,
                    "outcome": {"accepted": "accepted", "failed": "failed"}.get(state),
                }
                if state == "failed":
                    attempt["error"] = {
                        "code": "node_failed",
                        "type": "VendorNotFound",
                        "message": "No vendor matches 'ACME Ltd.'",
                        "traceback": [
                            {
                                "file": "flows/ingest_invoices.py",
                                "line": 42,
                                "function": "match_vendor",
                            }
                        ],
                    }
                attempts.append(attempt)
            node = {"state": state, "attempts": attempts}
            if state == "accepted":
                node["out"] = {"v": {"rows": 120 - index * 7, "ok": True}}
            nodes[inv] = node
        status = "cancelled" if run in self.cancelled else summary["status"]
        return {
            "complete": True,
            "projector": {"state": "live", "caught_up": True},
            "run": {
                "run_id": run,
                "status": status,
                "nodes": nodes,
                "applied_tseq": 12,
                "flow_slug": summary["flow_slug"],
                "deployment_id": summary["deployment_id"],
                "stale": False,
            },
        }

    def run_inputs(self, project, run):
        return {
            "availability": "available",
            "input": {"batch": "2026-10-27/inbox.csv", "dry_run": False},
            "bytes": 52,
            "complete": True,
            "reason": None,
        }

    def run_outputs(self, project, run):
        nodes = self.run(project, run)["run"]["nodes"]
        return {
            "outputs": [
                {"inv": inv, "out": node["out"]} for inv, node in nodes.items() if "out" in node
            ],
            "complete": True,
            "stale": False,
            "projector": {"state": "live", "caught_up": True},
        }

    def run_artifacts(self, project, run):
        return {
            "artifacts": [
                {
                    "binding_name": "reports",
                    "snapshot_id": "snap_1",
                    "files": [
                        {
                            "path": "summary.csv",
                            "size_bytes": 2048,
                            "executable": False,
                            "sha256": "9f2c4e1a" * 8,
                        }
                    ],
                }
            ],
            "complete": True,
            "stale": False,
            "projector": {"state": "live", "caught_up": True},
        }

    def observation(self, project, run, inv, g):
        return {
            "truncated": False,
            "messages": [
                {"t": "12:00:01", "level": "INFO", "logger": inv, "message": f"{inv} started"}
            ],
            "agent": [],
        }

    def capabilities(self, project, run=None):
        allowed = not self.read_only
        reason = None if allowed else "read_only"
        return {
            "actions": {
                "run.cancel": {"supported": True, "allowed": allowed, "reason": reason},
                "artifact.download": {"supported": True, "allowed": True, "reason": None},
            }
        }

    def cancel(self, project, run, cmd_id=None):
        if self.read_only:
            from .source import SourceError

            raise SourceError("--read-only disables backend mutations", code="read_only")
        self.cancelled.add(run)
        return {
            "outcome": "accepted",
            "cmd_id": cmd_id or "cmd_fixture",
        }, cmd_id or "cmd_fixture"

    def download(self, project, run, binding, path, destination):
        return {"path": path, "size_bytes": 2048, "sha256": "9f2c4e1a" * 8}

    def project_events(self, project, *, stop, last_event_id=None):
        yield _Frame("ready", None, {"v": 1, "epoch": "e1"})
        while not stop.is_set() and not self.closed.is_set():
            stop.wait(0.5)

    def run_events(self, project, run, *, stop, last_event_id=None):
        start = self._summary(run)["times"][0] / 1000
        for index, (kind, inv) in enumerate(
            (
                ("create", None),
                ("admission", None),
                ("authorize", "fetch_batch"),
                ("accept", "fetch_batch"),
                ("authorize", "parse_lines"),
                ("accept", "parse_lines"),
                ("authorize", "match_vendor"),
            )
        ):
            if stop.is_set():
                return
            yield _Frame(
                "fact",
                str(index + 1),
                {
                    "kind": kind,
                    "inv": inv,
                    "t": start + index * 3,
                    "tseq": index + 1,
                    "decision": "admitted",
                },
            )
        while not stop.is_set() and not self.closed.is_set():
            stop.wait(0.5)

    def close(self) -> None:
        self.closed.set()
