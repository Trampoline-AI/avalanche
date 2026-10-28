"""Run tab bodies shared by the flow page (selected run) and the run page, plus the step drawer.

Ports durable-run-detail.tsx: Inputs & Outputs, Timeline (live facts), Artifacts, and the
NodePanel tabs (Overview, IO, Attempts, Trace, Source).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from rich.text import Text

from .fmt import abbrev_id

TERMINAL = {"succeeded", "failed", "cancelled", "rejected", "expired", "integrity"}
WHY = {
    "not_recorded": "Not recorded for this run (created before inputs were kept).",
    "redacted": "Redacted.",
    "oversized": "Too large to show (over 1 MiB).",
}


def fact_stage(fact: dict) -> str:
    kind = fact.get("kind", "")
    if kind == "create":
        return "Submitted"
    if kind == "admission":
        decision = fact.get("decision", "")
        return {"queued": "Queued", "admitted": "Admitted"}.get(
            decision, decision.replace("_", " ")
        )
    if kind == "seal":
        return str(fact.get("status", "")).capitalize()
    if kind == "control":
        return f"{str(fact.get('cmd', '')).capitalize()} requested"
    return {
        "intent": "Step scheduled",
        "ready": "Step ready",
        "authorize": "Started",
        "accept": "Step accepted",
        "fail": "Step failed",
        "cancel_node": "Step cancelled",
        "retry": "Step retry",
        "node_timeout": "Step timed out",
        "admission_rejected": "Admission rejected",
        "reject": "Rejected",
    }.get(kind, kind.replace("_", " "))


def preview(value, limit=160) -> str:
    if isinstance(value, str):
        return (value[:limit] + "…") if len(value) > limit else (value or "Empty string")
    if value is None:
        return "null"
    if isinstance(value, list):
        head = ", ".join(preview(item, 40) for item in value[:3])
        return f"{len(value)} items" + (
            f" · {head}{', …' if len(value) > 3 else ''}" if value else ""
        )
    if isinstance(value, dict):
        parts = []
        for key, item in list(value.items())[:3]:
            shown = (
                f"{len(item)} fields"
                if isinstance(item, dict)
                else f"{len(item)} items"
                if isinstance(item, list)
                else preview(item, 60)
            )
            parts.append(f"{key}: {shown}")
        return (
            " · ".join(parts) + (f" · +{len(value) - 3} fields" if len(value) > 3 else "")
        ) or "Empty object"
    return str(value)


def output_summary(out) -> str:
    if isinstance(out, str):
        return f"SHA-256 · {out}"
    if isinstance(out, dict) and "ref" in out:
        ref = out["ref"]
        return f"Reference only · {ref.get('size')} bytes · {ref.get('sha256')}"
    if isinstance(out, dict) and "v" in out:
        return preview(out["v"], 240)
    return preview(out, 240)


def section(title: str, note: str, pal: dict) -> Text:
    return Text.assemble(
        (title.upper(), f"bold {pal['ink']}"), "  ", (note, pal["muted"]), "\n"
    )


def json_block(value, limit=4000) -> str:
    text = json.dumps(value, indent=2, default=str)
    return text if len(text) <= limit else text[:limit] + "\n… (truncated in the terminal view)"


def io_body(inputs, outputs, pal: dict) -> Text:
    out = section("Input", "Submitted input of this run", pal)
    if inputs.error:
        unavailable = inputs.status == 503
        out.append(
            (
                "Submitted input is not available on this API (inputs feature off)."
                if unavailable
                else inputs.error
            )
            + "\n",
            pal["muted"],
        )
    elif not inputs.loaded:
        out.append("Loading…\n", pal["muted"])
    else:
        value = inputs.data
        if value.get("availability") == "available" and value.get("input") is not None:
            out.append(json_block(value["input"]) + "\n", pal["ink"])
        else:
            out.append(
                WHY.get(value.get("availability"), value.get("reason") or "Not available.")
                + "\n",
                pal["muted"],
            )
        if value.get("bytes") is not None:
            out.append(
                f"{value['bytes']} bytes"
                + ("" if value.get("complete") else " · incomplete")
                + "\n",
                pal["muted"],
            )
    out.append("\n")
    out.append_text(
        section("Outputs", "Accepted step outputs so far; not a single final result", pal)
    )
    if outputs.error:
        out.append(outputs.error + "\n", pal["danger"])
    elif not outputs.loaded:
        out.append("Loading…\n", pal["muted"])
    else:
        rows = outputs.data.get("outputs") or []
        if not rows:
            out.append("No step outputs yet.\n", pal["muted"])
        width = max((len(row["inv"]) for row in rows), default=0)
        for row in rows:
            out.append(row["inv"].ljust(width + 2), f"bold {pal['ink']}")
            out.append(output_summary(row["out"]) + "\n", pal["ink"])
        freshness(outputs.data, out, pal)
    return out


def freshness(value: dict, out: Text, pal: dict) -> None:
    projector = value.get("projector") or {}
    out.append(
        f"\ncomplete: {value.get('complete')} · stale: {value.get('stale')} · projector: "
        f"{projector.get('state')} · caught_up: {projector.get('caught_up')}\n",
        pal["muted"],
    )


def timeline_body(frames: list[dict], message: str, pal: dict) -> Text:
    out = section("Timeline", "Live lifecycle, most recent 200 events", pal)
    out.append(message + "\n\n", pal["muted"])
    facts = [frame for frame in frames if frame.get("event") == "fact"]
    if not facts:
        out.append("No lifecycle facts received yet.\n", pal["muted"])
    for frame in facts[-200:]:
        data = frame["data"]
        when = (
            datetime.fromtimestamp(data.get("t", 0), tz=UTC)
            .astimezone()
            .strftime("%b %-d, %H:%M:%S")
        )
        out.append("■ ", pal["accent"])
        out.append(fact_stage(data).ljust(22), f"bold {pal['ink']}")
        out.append(when.ljust(18), pal["muted"])
        if data.get("inv"):
            out.append(str(data["inv"]), pal["muted"])
        out.append("\n")
    return out


def artifact_rows(artifacts) -> list[tuple[str, list, dict]]:
    rows = []
    for binding in (artifacts.data or {}).get("artifacts") or []:
        for file in binding.get("files") or []:
            key = f"{binding['binding_name']}/{file['path']}"
            rows.append(
                (
                    key,
                    [
                        binding["binding_name"],
                        file["path"],
                        str(file["size_bytes"]),
                        file["sha256"][:16] + "…",
                        "yes" if file.get("executable") else "no",
                    ],
                    {"binding": binding["binding_name"], "path": file["path"], "file": file},
                )
            )
    return rows


def drawer_body(
    tab: str, inv: str, run: dict, flow: dict | None, observation, pal: dict
) -> Text:
    node = (run.get("nodes") or {}).get(inv) or {}
    flow = flow or {}
    name = (flow.get("display_names") or {}).get(inv) or inv
    out = Text.assemble(
        ("STEP\n", pal["muted"]),
        (name + "\n", f"bold {pal['ink']}"),
        (f"{inv} · {abbrev_id(run['run_id'])}\n\n", pal["muted"]),
    )
    agent = (flow.get("agent_nodes") or {}).get(inv)
    if tab == "overview":
        out.append(
            f"[{(node.get('state') or 'unknown').upper()}] ",
            pal["danger"] if node.get("state") == "failed" else pal["ink"],
        )
        out.append(
            "["
            + str((flow.get("node_types") or {}).get(inv) or ("agent" if agent else "step"))
            + "]\n\n",
            pal["muted"],
        )
        docs = (flow.get("node_docs") or {}).get(inv)
        out.append("Documentation\n", pal["muted"])
        out.append((docs or "not reported") + "\n\n", pal["ink"] if docs else pal["muted"])
        if isinstance(agent, dict):
            for label, value in (
                ("Model", agent.get("models") or agent.get("model")),
                ("Signature", agent.get("signature")),
                (
                    "Instructions",
                    agent.get("aggregated_static_instructions") or agent.get("instructions"),
                ),
            ):
                out.append(label + "\n", pal["muted"])
                out.append(
                    (
                        value
                        if isinstance(value, str)
                        else json_block(value, 1200)
                        if value
                        else "not reported"
                    )
                    + "\n\n"
                )
        out.append("From the current deployment, not pinned to this run\n", pal["muted"])
    elif tab == "io":
        parents = sorted(
            parent for parent, children in (flow.get("graph") or {}).items() if inv in children
        )
        out.append("INPUTS\n", pal["muted"])
        if not parents:
            out.append("Run input: see the run's Inputs & Outputs tab\n", pal["muted"])
        for parent in parents:
            parent_out = ((run.get("nodes") or {}).get(parent) or {}).get("out")
            out.append(parent + "\n", f"bold {pal['ink']}")
            out.append(
                (output_summary(parent_out) if parent_out is not None else "not reported")
                + "\n"
            )
        out.append("\nOUTPUTS\n", pal["muted"])
        out.append(
            (
                output_summary(node["out"])
                if node.get("out") is not None
                else "No accepted output yet."
            )
            + "\n"
        )
    elif tab == "attempts":
        attempts = node.get("attempts") or []
        if not attempts:
            out.append("No attempts yet.\n", pal["muted"])
        for attempt in attempts:
            started, ended = attempt.get("started_at"), attempt.get("ended_at")
            out.append(f"g{attempt.get('g')} ", f"bold {pal['ink']}")
            if attempt.get("outcome"):
                bad = attempt["outcome"] in {"failed", "timed_out", "lost"}
                out.append(f"[{attempt['outcome']}] ", pal["danger"] if bad else pal["muted"])
            if isinstance(started, (int, float)):
                local = datetime.fromtimestamp(started, tz=UTC).astimezone()
                out.append(local.strftime("%b %-d, %H:%M:%S") + "  ", pal["muted"])
                if isinstance(ended, (int, float)):
                    out.append(f"{ended - started:.2f} s", pal["ink"])
            out.append("\n" + str(attempt.get("auth", "")) + "\n", pal["muted"])
            error = attempt.get("error")
            if isinstance(error, dict):
                out.append(f"{error.get('code')} {error.get('type') or ''}\n", pal["danger"])
                out.append(str(error.get("message", "")) + "\n")
                for frame in error.get("traceback") or []:
                    out.append(
                        f"  {frame.get('file')}:{frame.get('line')} "
                        f"in {frame.get('function')}\n",
                        pal["muted"],
                    )
            out.append("\n")
    elif tab == "trace":
        if observation is None:
            out.append("No trace recorded for the latest attempt.\n", pal["muted"])
        elif observation.error:
            out.append(f"observation unavailable: {observation.error}\n", pal["muted"])
        elif not observation.loaded:
            out.append("Loading trace…\n", pal["muted"])
        else:
            value = observation.data
            if value.get("truncated"):
                out.append(
                    "[truncated]\n", pal["warning"] if "warning" in pal else pal["muted"]
                )
            out.append("MESSAGES\n", pal["muted"])
            for message in (value.get("messages") or [])[:200]:
                out.append(
                    f"{message.get('t')} {message.get('level')} {message.get('logger')}\n",
                    pal["muted"],
                )
                out.append(preview(message.get("message"), 600) + "\n")
            if not value.get("messages"):
                out.append("No messages recorded\n", pal["muted"])
            out.append("\nAGENT INVOCATIONS\n", pal["muted"])
            for agent_row in value.get("agent") or []:
                trace = agent_row.get("trace") or {}
                out.append(
                    f"{agent_row.get('invocation_id')} [{agent_row.get('status')}] "
                    f"{trace.get('model', '')} {trace.get('iterations', '')} iterations\n"
                )
                for step in (trace.get("steps") or [])[:50]:
                    out.append(
                        f"  Iteration {step.get('iteration')} · {step.get('duration_ms')} ms"
                        + (" · error" if step.get("error") else "")
                        + "\n",
                        pal["muted"],
                    )
                    for key in ("reasoning", "code", "output"):
                        if step.get(key):
                            out.append(f"    {key}: " + preview(step[key], 400) + "\n")
            if not value.get("agent"):
                out.append("No agent trace recorded\n", pal["muted"])
    elif tab == "source":
        out.append("From the current deployment, not pinned to this run\n\n", pal["muted"])
        source = (flow.get("node_sources") or {}).get(inv)
        out.append(
            source if isinstance(source, str) else "Source not captured for this deployment",
            pal["ink"] if isinstance(source, str) else pal["muted"],
        )
    return out


DRAWER_TABS = (
    ("overview", "Overview"),
    ("io", "IO"),
    ("attempts", "Attempts"),
    ("trace", "Trace"),
    ("source", "Source"),
)
