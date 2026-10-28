"""Console presentation helpers, ported 1:1 from apps/console/src/lib (no data ownership)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from rich.text import Text

# id-format.ts: Delta resource ID prefixes; flow slugs and other names are never abbreviated.
_PREFIXED = re.compile(r"^(run|dpl|proj|flow|sch|sched|schedule|org|rt)_(.+)$")


def abbrev_id(value: str) -> str:
    match = _PREFIXED.match(value)
    if not match or len(match[2]) <= 9:
        return value
    return f"{match[1]}_{match[2][:4]}…{match[2][-4:]}"


# run-status.ts
_TONES = {
    "submitted": "waiting",
    "queued": "waiting",
    "admitted": "active",
    "running": "active",
    "waiting": "waiting",
    "resumable": "active",
    "parked": "waiting",
    "succeeded": "success",
    "failed": "danger",
    "cancelled": "neutral",
    "rejected": "danger",
    "expired": "neutral",
    "integrity": "danger",
}


def tone(status: str | None) -> str:
    return _TONES.get(status or "", "neutral")


# status-groups.ts
STATUS_GROUPS = {
    "active": ("Active", ("admitted", "running", "resumable")),
    "waiting": ("Queued", ("queued", "submitted")),
}


def matches_status(status: str, wanted: str | None) -> bool:
    if not wanted:
        return True
    group = STATUS_GROUPS.get(wanted)
    return status in group[1] if group else status == wanted


# console-views.tsx status ordering for count badges.
STATUS_ORDER = [
    "failed",
    "integrity",
    "rejected",
    "admitted",
    "running",
    "resumable",
    "queued",
    "submitted",
    "waiting",
    "parked",
    "succeeded",
]


def status_rank(status: str) -> int:
    return STATUS_ORDER.index(status) + 1 if status in STATUS_ORDER else 99


# glyph-clock.ts + flow-detail.tsx RUN_GLYPH (the Avalanche TUI glyph set).
SPINNER = ("◐", "◑", "◒", "◓")
GLYPH = {
    "running": "◐",
    "success": "✓",
    "failed": "✗",
    "pending": "○",
    "queued": "◌",
    "cancelled": "⊘",
}
RUN_GLYPH = {
    "submitted": "queued",
    "queued": "pending",
    "waiting": "pending",
    "parked": "pending",
    "admitted": "running",
    "running": "running",
    "resumable": "running",
    "succeeded": "success",
    "failed": "failed",
    "rejected": "failed",
    "integrity": "failed",
    "cancelled": "cancelled",
    "expired": "cancelled",
}
NODE_GLYPH = {
    "accepted": "success",
    "running": "running",
    "failed": "failed",
    "ready": "pending",
    "intended": "pending",
    "waiting": "pending",
    "cancelled": "cancelled",
    "expired": "cancelled",
    "indeterminate": "failed",
}


def glyph(kind: str, frame: int) -> str:
    return SPINNER[frame % 4] if kind == "running" else GLYPH.get(kind, "○")


# run-format.ts
def run_times(run: dict) -> tuple[float | None, float | None]:
    """[base, created, updated, started, finished] offsets → (start ms, duration ms)."""
    times = run.get("times") or []
    base, created, _, started, finished = (list(times) + [None] * 5)[:5]
    if base is None:
        return None, None

    def at(offset):
        return None if offset is None else base + offset

    start = at(started) if started is not None else at(created)
    end = at(finished)
    return start, (end - start if start is not None and end is not None else None)


def format_when(ms: float | None) -> str | None:
    if ms is None:
        return None
    moment = datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone()
    return moment.strftime("%b %-d, %H:%M")


def format_iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return format_when(
            datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        )
    except ValueError:
        return value


def iso_ms(value: str | None) -> float:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return 0.0


def format_duration(ms: float | None) -> str | None:
    if ms is None or ms < 0:
        return None
    s = round(ms / 1000)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m {s % 60}s"
    return f"{m // 60}h {m % 60}m"


def plural(count: int | None, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


def relative_age(updated_ms: float, now_ms: float) -> str:
    seconds = max(0, int((now_ms - updated_ms) / 1000))
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{seconds} s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def newest_deployment(deployments: list[dict]) -> dict | None:
    if not deployments:
        return None
    return max(deployments, key=lambda d: (iso_ms(d.get("created_at")), d["deployment_id"]))


def schedule_rows(
    records: list[dict], flows: list[dict], flow_slug: str | None = None
) -> list[dict]:
    """schedule-rows.ts: schedule records plus cron triggers declared in the catalog."""
    rows: list[dict] = [
        dict(
            key=r["schedule_id"],
            flow_slug=r["flow_slug"],
            cron=r["cron"],
            timezone=r.get("timezone") or "",
            enabled=r["enabled"],
            deployment_id=r["deployment_id"],
            source="record",
            schedule=r,
        )
        for r in records
    ]
    for flow in flows:
        if flow.get("cron") and not any(r["flow_slug"] == flow["flow_slug"] for r in records):
            rows.append(
                dict(
                    key="flow:" + flow["flow_slug"],
                    flow_slug=flow["flow_slug"],
                    cron=flow["cron"],
                    timezone="",
                    enabled=True,
                    deployment_id=flow["deployment_id"],
                    source="flow",
                    schedule=None,
                )
            )
    return sorted(
        (r for r in rows if not flow_slug or r["flow_slug"] == flow_slug),
        key=lambda r: r["flow_slug"],
    )


# ── Rich renderables shared by every screen ──────────────────────────────────


def tone_style(name: str) -> str:
    return {
        "danger": "$error",
        "active": "$primary",
        "waiting": "$warning",
        "success": "$foreground",
        "neutral": "$text-muted",
    }[name]


def status_badge(status: str | None, palette: dict) -> Text:
    """run-status-badge.tsx: outlined mono tag with a square marker → `■ FAILED`."""
    color = palette[tone(status)]
    return Text.assemble(("■ ", color), ((status or "unknown").upper(), f"bold {color}"))


def glyph_status(status: str, frame: int, palette: dict) -> Text:
    """flow-detail.tsx RunGlyphStatus: animated TUI glyph + status."""
    color = palette[tone(status) if tone(status) != "success" else "ok"]
    return Text.assemble(
        (glyph(RUN_GLYPH.get(status, "pending"), frame), color), " ", (status.upper(), color)
    )


def count_badge(count: int, label: str, tone_name: str, palette: dict) -> Text:
    color = palette[tone_name]
    return Text(f"[{count} {label.upper()}]", style=color)


def status_counts(runs: list[dict], palette: dict, empty: str = "No runs yet") -> Text:
    if not runs:
        return Text(empty, style=palette["neutral"])
    groups: dict[str, int] = {}
    for run in runs:
        groups[run["status"]] = groups.get(run["status"], 0) + 1
    out = Text()
    for status, count in sorted(groups.items(), key=lambda item: status_rank(item[0])):
        if out:
            out.append(" ")
        out.append_text(count_badge(count, status, tone(status), palette))
    return out


def load_counts(runs: list[dict], palette: dict) -> Text:
    active = sum(run["status"] in STATUS_GROUPS["active"][1] for run in runs)
    queued = sum(run["status"] in STATUS_GROUPS["waiting"][1] for run in runs)
    return Text.assemble(
        count_badge(active, "active", "active" if active else "neutral", palette),
        " ",
        count_badge(queued, "queued", "waiting" if queued else "neutral", palette),
    )
