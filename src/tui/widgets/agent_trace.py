"""Scrollable turn-by-turn rendering for finalized agent traces."""

from __future__ import annotations

import json
from typing import Any

from rich.syntax import Syntax
from rich.text import Text
from textual.widgets import Static


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str, ensure_ascii=False)


def _event_kind(event: dict[str, Any]) -> str:
    return str(event.get("event_kind") or event.get("kind") or "unknown")


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _usage_label(usage: Any) -> str:
    if not isinstance(usage, dict):
        return "0 in / 0 out · 0 cache · $0.000000"
    return (
        f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out"
        f" · {usage.get('cache_hits', 0)} cache"
        f" · ${float(usage.get('cost', 0) or 0):.6f}"
    )


def _append_block(
    text: Text,
    label: str,
    value: Any,
    *,
    indent: int = 4,
    style: str = "dim",
) -> None:
    text.append(f"{' ' * indent}{label}:\n", style="bold")
    rendered = value if isinstance(value, str) else _json(value)
    rendered = rendered if rendered else "(empty)"
    body_indent = " " * (indent + 4)
    for line in rendered.splitlines() or [rendered]:
        text.append(f"{body_indent}{line}\n", style=style)


def _append_python(text: Text, code: str, *, indent: int = 4) -> None:
    text.append(f"{' ' * indent}code:\n", style="bold #60dce4")
    source = code or "(empty)"
    highlighted = Syntax(
        source,
        "python",
        theme="monokai",
        background_color="default",
        word_wrap=False,
    ).highlight(source)
    body_indent = " " * (indent + 4)
    for line in highlighted.split("\n", allow_blank=True):
        text.append(body_indent)
        text.append_text(line)
        text.append("\n")


def _count_predict_calls(step: dict[str, Any]) -> int:
    groups = step.get("predict_calls")
    if not isinstance(groups, list):
        return 0
    return sum(len(group.get("calls") or []) for group in groups if isinstance(group, dict))


def _append_tabs(text: Text, active: str) -> None:
    names = ("Trace", "Output", "Metadata")
    for index, name in enumerate(names):
        style = "bold reverse #60dce4" if name.lower() == active else "dim"
        text.append(f" {name} ", style=style)
        if index < len(names) - 1:
            text.append(" ")
    text.append("\n\n")


def _agent_state_label(store: Any, node_id: str | None) -> str:
    run = store.current_run
    if run is None or node_id is None:
        return "unavailable"
    state = run.nodes.get(node_id)
    if state is None:
        return "pending"
    status = getattr(state, "status", None)
    return str(getattr(status, "value", status) or "unavailable")


class AgentTraceInspector(Static):
    """Render live agent status followed by the finalized structured trace."""

    DEFAULT_CSS = """
    AgentTraceInspector {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    def render(self) -> Text:
        store = self.app.store
        node_id = store.selected_agent_node_id
        workflow = store.current_workflow
        display_name = (
            workflow.display_names.get(node_id, node_id)
            if workflow is not None and node_id is not None
            else "Agent step"
        )
        envelope = store.selected_agent_trace_envelope
        text = Text()
        _append_tabs(text, "trace")
        text.append(f"AGENT TRACE · {display_name}\n", style="bold #60dce4")
        if envelope is None:
            state_label = _agent_state_label(store, node_id)
            if state_label == "pending":
                text.append("This agent step has not run yet for this run.\n", style="yellow")
            elif state_label == "running":
                text.append("Status: running · waiting for live updates\n", style="yellow")
            else:
                text.append("Trace unavailable or malformed\n", style="bold red")
            return text

        trace_value = envelope.get("trace")
        trace = trace_value if isinstance(trace_value, dict) else None
        status = str(envelope.get("status") or "unavailable")
        evidence = trace.get("evidence") if trace is not None else None
        evidence = evidence if isinstance(evidence, dict) else None
        run_id = envelope.get("run_id") or (evidence or {}).get("run_id") or "—"
        status_style = "green" if status in {"completed", "max_iterations"} else "yellow"
        if status in {"error", "unavailable"}:
            status_style = "bold red"
        text.append(f"Status: {status}", style=status_style)
        text.append(f" · Run: {run_id}\n")

        if envelope.get("error"):
            text.append(f"Error: {envelope['error']}\n", style="bold red")

        if trace is None:
            self._append_live_status(text, store.selected_agent_events)
            steps = store.selected_agent_live_steps
            text.append(f"\nLIVE TRACE · {len(steps)} turn(s)\n", style="bold #b38cff")
            if not steps:
                text.append("No executable turn captured yet.\n", style="dim")
                return text
            self._append_turns(
                text,
                steps,
                max_turns=len(steps),
                selected_index=store.trace_turn_index,
                collapsed_turns=store.trace_collapsed_turns,
                show_full_output=store.trace_show_full_output,
                live=True,
            )
            text.append(
                "←/→ tabs · ↑/↓ select turn · Enter collapse · o full output · "
                "PgUp/PgDn scroll · Esc back\n",
                style="dim",
            )
            return text

        self._append_trace_summary(text, trace, evidence)
        steps = store.selected_agent_steps
        text.append(f"\nSTRUCTURED TRACE · {len(steps)} turn(s)\n", style="bold #b38cff")
        if not steps:
            text.append("No structured turns captured.\n", style="dim")
            return text
        self._append_turns(
            text,
            steps,
            max_turns=trace.get("max_iterations") or len(steps),
            selected_index=store.trace_turn_index,
            collapsed_turns=store.trace_collapsed_turns,
            show_full_output=store.trace_show_full_output,
        )

        text.append(
            "←/→ tabs · ↑/↓ select turn · Enter collapse · o full output · "
            "PgUp/PgDn scroll · Esc back\n",
            style="dim",
        )
        return text

    @staticmethod
    def _append_trace_summary(
        text: Text, trace: dict[str, Any], evidence: dict[str, Any] | None
    ) -> None:
        usage = trace.get("usage") if isinstance(trace.get("usage"), dict) else {}
        main_usage = usage.get("main") if isinstance(usage, dict) else {}
        sub_usage = usage.get("sub") if isinstance(usage, dict) else {}
        text.append(
            f"Model: {trace.get('model', '—')}"
            f" · Sub-model: {trace.get('sub_model') or '—'}\n"
        )
        text.append(
            f"Turns: {trace.get('iterations', 0)}/{trace.get('max_iterations', '—')}"
            f" · Duration: {trace.get('duration_ms', 0)}ms\n"
        )
        text.append(f"Main: {_usage_label(main_usage)}\n")
        text.append(f"Sub:  {_usage_label(sub_usage)}\n")
        if evidence is None:
            text.append("Live record: unavailable\n", style="yellow")
            return
        complete = bool(evidence.get("complete"))
        terminal = evidence.get("terminal_outcome") or "unknown"
        text.append(
            f"Live record: {'complete' if complete else 'incomplete'}"
            f" · terminal={terminal}\n",
            style="green" if complete else "yellow",
        )

    @staticmethod
    def _append_live_status(text: Text, events: list[dict]) -> None:
        text.append(f"\nLIVE AGENT STATUS · {len(events)} update(s)\n", style="bold #b38cff")
        if not events:
            text.append("No live updates observed yet.\n", style="dim")
            return
        labels = {
            "run.started": "Run started",
            "code.generated": "Code generated",
            "code.executed": "Sandbox output received",
            "iteration.recorded": "Turn completed",
            "predict.started": "Sub-model call started",
            "predict.finished": "Sub-model call finished",
            "tool.started": "Tool call started",
            "tool.finished": "Tool call finished",
            "run.succeeded": "Run completed",
            "run.failed": "Run failed",
            "run.cancelled": "Run cancelled",
        }
        for event in events:
            kind = _event_kind(event)
            data = _event_data(event)
            iteration = data.get("iteration")
            recorded_step = data.get("step")
            if iteration is None and isinstance(recorded_step, dict):
                iteration = recorded_step.get("iteration")
            detail = f" · turn {iteration}" if iteration is not None else ""
            name = data.get("name") or data.get("signature")
            if name:
                detail += f" · {name}"
            failed = bool(data.get("error")) or kind in {"run.failed", "run.cancelled"}
            marker = "!" if failed else "•"
            text.append(
                f"{marker} {labels.get(kind, kind)}{detail}\n",
                style="red" if failed else "",
            )

    @classmethod
    def _append_turns(
        cls,
        text: Text,
        steps: list[dict],
        *,
        max_turns: int,
        selected_index: int,
        collapsed_turns: set[int],
        show_full_output: bool,
        live: bool = False,
    ) -> None:
        selected_index = min(selected_index, len(steps) - 1)
        for index, step in enumerate(steps):
            cls._append_turn(
                text,
                step,
                index=index,
                max_turns=max_turns,
                selected=index == selected_index,
                collapsed=index in collapsed_turns,
                show_full_output=show_full_output,
                live=live,
            )

    @staticmethod
    def _append_turn(
        text: Text,
        step: dict[str, Any],
        *,
        index: int,
        max_turns: int,
        selected: bool,
        collapsed: bool,
        show_full_output: bool,
        live: bool = False,
    ) -> None:
        iteration = step.get("iteration", index + 1)
        duration = step.get("duration_ms", 0)
        tool_calls = step.get("tool_calls") if isinstance(step.get("tool_calls"), list) else []
        tool_count = step.get("tool_count")
        tool_count = tool_count if isinstance(tool_count, int) else len(tool_calls)
        predict_count = step.get("predict_count")
        predict_count = (
            predict_count if isinstance(predict_count, int) else _count_predict_calls(step)
        )
        error = bool(step.get("error"))
        marker = "▶" if selected else " "
        disclosure = "▸" if collapsed else "▾"
        counts = f"{tool_count} tool · {predict_count} predict"
        header = (
            f"\n{marker} {disclosure} AGENT TURN {iteration}/{max_turns}"
            f" · {duration}ms · {counts}"
            f"{' · LIVE' if live else ''}"
            f"{' · ERROR' if error else ''}\n"
        )
        style = "reverse" if selected else ("bold red" if error else "bold #60dce4")
        text.append(header, style=style)
        if collapsed:
            return

        reasoning = step.get("reasoning")
        if reasoning:
            _append_block(text, "reasoning", reasoning, style="dim italic")
        _append_python(text, str(step.get("code") or ""))

        output_key = "untruncated_output" if show_full_output else "output"
        output_label = "output (full)" if show_full_output else "output"
        _append_block(
            text,
            output_label,
            step.get(output_key) or "(no output)",
            style="red" if error else "green",
        )

        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            call_error = call.get("error")
            text.append(f"    tool · {call.get('name', 'unknown')}\n", style="bold magenta")
            payload = {key: call.get(key) for key in ("args", "kwargs") if call.get(key)}
            if payload:
                _append_block(text, "input", payload, indent=8)
            _append_block(
                text,
                "error" if call_error else "result",
                call_error or call.get("result"),
                indent=8,
                style="red" if call_error else "dim",
            )

        predict_groups = step.get("predict_calls")
        if isinstance(predict_groups, list):
            for group in predict_groups:
                if not isinstance(group, dict):
                    continue
                text.append(
                    f"    predict · {group.get('signature', 'unknown')}"
                    f" · {group.get('model', '—')}\n",
                    style="bold magenta",
                )
                for call_index, call in enumerate(group.get("calls") or [], start=1):
                    if not isinstance(call, dict):
                        continue
                    text.append(f"        call {call_index}\n", style="bold")
                    _append_block(text, "input", call.get("input"), indent=12)
                    call_error = call.get("error")
                    _append_block(
                        text,
                        "error" if call_error else "output",
                        call_error or call.get("output"),
                        indent=12,
                        style="red" if call_error else "dim",
                    )

        lm = step.get("lm")
        usage = step.get("usage") if isinstance(step.get("usage"), dict) else {}
        finish_reason = lm.get("finish_reason") if isinstance(lm, dict) else None
        text.append(
            f"    turn meta: finish={finish_reason or '—'}"
            f" · main {_usage_label(usage.get('main'))}"
            f" · sub {_usage_label(usage.get('sub'))}\n",
            style="dim",
        )


class AgentOutputInspector(Static):
    """Render the final structured prediction declared by the agent signature."""

    DEFAULT_CSS = """
    AgentOutputInspector {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    def render(self) -> Text:
        store = self.app.store
        node_id = store.selected_agent_node_id
        workflow = store.current_workflow
        display_name = (
            workflow.display_names.get(node_id, node_id)
            if workflow is not None and node_id is not None
            else "Agent step"
        )
        outputs = store.selected_agent_outputs
        text = Text()
        _append_tabs(text, "output")
        text.append(f"AGENT OUTPUT · {display_name}\n", style="bold #60dce4")
        if outputs is None:
            state_label = _agent_state_label(store, node_id)
            if state_label == "pending":
                text.append("This agent step has not run yet for this run.\n", style="yellow")
            elif state_label == "running":
                text.append(
                    "Output will be available after this agent step completes.\n",
                    style="yellow",
                )
            else:
                text.append("Output unavailable\n", style="bold red")
            text.append("\n←/→ tabs · PgUp/PgDn scroll · Esc back\n", style="dim")
            return text

        metadata = store.selected_agent_metadata
        signature = metadata.get("signature") if isinstance(metadata, dict) else None
        fields = signature.get("outputs") if isinstance(signature, dict) else None
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                    continue
                name = field["name"]
                annotation = field.get("annotation")
                description = field.get("description")
                label = name
                if isinstance(annotation, str) and annotation:
                    label += f": {annotation}"
                if isinstance(description, str) and description:
                    label += f" — {description}"
                rendered = _json(outputs[name]) if name in outputs else "Unavailable"
                _append_block(text, label, rendered, indent=2)
        else:
            for name, value in outputs.items():
                _append_block(text, str(name), _json(value), indent=2)

        text.append("\n←/→ tabs · PgUp/PgDn scroll · Esc back\n", style="dim")
        return text


class AgentMetadataInspector(Static):
    """Render static agent declaration metadata as hierarchical text."""

    DEFAULT_CSS = """
    AgentMetadataInspector {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    def render(self) -> Text:
        store = self.app.store
        node_id = store.selected_agent_node_id
        workflow = store.current_workflow
        display_name = (
            workflow.display_names.get(node_id, node_id)
            if workflow is not None and node_id is not None
            else "Agent step"
        )
        metadata = store.selected_agent_metadata
        text = Text()
        _append_tabs(text, "metadata")
        text.append(f"AGENT METADATA · {display_name}\n", style="bold #60dce4")
        if metadata is None:
            text.append("Metadata unavailable or malformed\n", style="bold red")
            text.append("\n←/→ tabs · PgUp/PgDn scroll · Esc back\n", style="dim")
            return text

        self._append_signature(text, metadata.get("signature"))
        self._append_runtime(text, metadata.get("runtime"))
        self._append_skills(text, metadata.get("skills"))
        self._append_text_section(
            text,
            "AGGREGATED STATIC INSTRUCTIONS",
            metadata.get("aggregated_static_instructions"),
        )
        self._append_names(text, "PACKAGES", metadata.get("packages"))
        self._append_names(text, "MODULES", metadata.get("modules"))
        self._append_tools(text, metadata.get("tools"))
        text.append("\n←/→ tabs · PgUp/PgDn scroll · Esc back\n", style="dim")
        return text

    @classmethod
    def _append_signature(cls, text: Text, value: Any) -> None:
        text.append("\nSIGNATURE\n", style="bold #b38cff")
        if not isinstance(value, dict):
            text.append("  Unavailable\n", style="dim")
            return
        text.append(f"  Name: {cls._scalar(value.get('name'))}\n")
        cls._append_text_block(text, "Instructions", value.get("instructions"))
        cls._append_fields(text, "Inputs", value.get("inputs"))
        cls._append_fields(text, "Outputs", value.get("outputs"))

    @classmethod
    def _append_fields(cls, text: Text, label: str, value: Any) -> None:
        text.append(f"  {label}:\n", style="bold")
        fields = value if isinstance(value, list) else []
        if not fields:
            text.append("    None\n", style="dim")
            return
        for field in fields:
            if not isinstance(field, dict):
                continue
            name = cls._scalar(field.get("name"))
            annotation = cls._scalar(field.get("annotation"))
            description = cls._scalar(field.get("description"))
            suffix = f" — {description}" if description else ""
            text.append(f"    {name}: {annotation}{suffix}\n")

    @classmethod
    def _append_runtime(cls, text: Text, value: Any) -> None:
        text.append("\nMODEL / RUNTIME SETTINGS\n", style="bold #b38cff")
        runtime = value if isinstance(value, dict) else {}
        if not runtime:
            text.append("  Unavailable\n", style="dim")
            return
        preferred = ("lm", "sub_lm", "max_iterations", "max_llm_calls")
        names = [name for name in preferred if name in runtime]
        names.extend(sorted(name for name in runtime if name not in preferred))
        for name in names:
            text.append(f"  {name}: {cls._metadata_value(runtime[name])}\n")

    @classmethod
    def _append_skills(cls, text: Text, value: Any) -> None:
        text.append("\nSKILLS\n", style="bold #b38cff")
        skills = value if isinstance(value, list) else []
        if not skills:
            text.append("  None\n", style="dim")
            return
        for index, skill in enumerate(skills, start=1):
            if not isinstance(skill, dict):
                continue
            text.append(f"  {index}. {cls._scalar(skill.get('name'))}\n", style="bold magenta")
            cls._append_text_block(text, "Instructions", skill.get("instructions"), indent=4)
            for label, key in (
                ("Packages", "packages"),
                ("Modules", "modules"),
                ("Tools", "tools"),
            ):
                names = skill.get(key)
                rendered = cls._name_list(names)
                text.append(f"    {label}: {rendered}\n")

    @classmethod
    def _append_text_section(cls, text: Text, label: str, value: Any) -> None:
        text.append(f"\n{label}\n", style="bold #b38cff")
        cls._append_text_lines(text, value, indent=2)

    @classmethod
    def _append_names(cls, text: Text, label: str, value: Any) -> None:
        text.append(f"\n{label}\n", style="bold #b38cff")
        text.append(f"  {cls._name_list(value)}\n")

    @classmethod
    def _append_tools(cls, text: Text, value: Any) -> None:
        text.append("\nTOOLS\n", style="bold #b38cff")
        tools = value if isinstance(value, list) else []
        if not tools:
            text.append("  None\n", style="dim")
            return
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            text.append(f"  {cls._scalar(tool.get('name'))}\n", style="bold magenta")
            cls._append_text_lines(text, tool.get("description"), indent=4)

    @classmethod
    def _append_text_block(cls, text: Text, label: str, value: Any, *, indent: int = 2) -> None:
        text.append(f"{' ' * indent}{label}:\n", style="bold")
        cls._append_text_lines(text, value, indent=indent + 2)

    @classmethod
    def _append_text_lines(cls, text: Text, value: Any, *, indent: int) -> None:
        rendered = cls._scalar(value) or "(empty)"
        for line in rendered.splitlines() or [rendered]:
            text.append(f"{' ' * indent}{line}\n", style="dim")

    @classmethod
    def _metadata_value(cls, value: Any) -> str:
        if isinstance(value, dict):
            type_name = value.get("type")
            if isinstance(type_name, str):
                short_type = type_name.rsplit(".", 1)[-1]
                instance_name = value.get("name")
                if isinstance(instance_name, str):
                    return f"{instance_name} ({short_type})"
                return short_type
            return ", ".join(
                f"{key}={cls._metadata_value(item)}"
                for key, item in value.items()
                if isinstance(key, str)
            )
        if isinstance(value, list):
            return ", ".join(cls._metadata_value(item) for item in value) or "None"
        return cls._scalar(value)

    @classmethod
    def _name_list(cls, value: Any) -> str:
        if not isinstance(value, list):
            return "None"
        names = [cls._scalar(item) for item in value]
        return ", ".join(name for name in names if name) or "None"

    @staticmethod
    def _scalar(value: Any) -> str:
        if value is None:
            return "None"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (str, int, float)):
            return str(value)
        return type(value).__name__
