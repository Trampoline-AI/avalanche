"""Virtualized hierarchical rendering for agent trace inspection."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.syntax import Syntax
from rich.text import Text
from textual.strip import Strip
from textual.widgets import Static

InspectorPath = tuple[str, ...]
InspectorLayoutKey = tuple[str, str, str, str, int, bool, int]
_BODY_ESTIMATE_LINES = 8
_VIEWPORT_BUFFER_LINES = 12
_TRACE_CONTROLS = (
    "←/→ tabs · ↑/↓ select · Enter expand/collapse · o full output · "
    "PgUp/PgDn page · Esc back\n"
)


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


def _count_predict_calls(step: dict[str, Any]) -> int:
    groups = step.get("predict_calls")
    if not isinstance(groups, list):
        return 0
    return sum(len(group.get("calls") or []) for group in groups if isinstance(group, dict))


def _append_tabs(text: Text, active: str) -> None:
    for index, name in enumerate(("Trace", "Output", "Metadata")):
        style = "bold reverse #60dce4" if name.lower() == active else "dim"
        text.append(f" {name} ", style=style)
        if index < 2:
            text.append(" ")
    text.append("\n\n")


def _is_active_inspector_tab(store: Any, tab: str) -> bool:
    return store.trace_inspector_open and store.trace_inspector_tab == tab


def _layout_key(store: Any, tab: str) -> tuple[str, str, str, str, int, bool]:
    return (
        tab,
        store.selected_agent_trace_content_token,
        store.selected_agent_metadata_content_token,
        _agent_state_label(store, store.selected_agent_node_id),
        store.trace_hierarchy_revision,
        store.trace_show_full_output,
    )


def _agent_state_label(store: Any, node_id: str | None) -> str:
    run = store.current_run
    if run is None or node_id is None:
        return "unavailable"
    state = run.nodes.get(node_id)
    if state is None:
        return "pending"
    status = getattr(state, "status", None)
    return str(getattr(status, "value", status) or "unavailable")


def _value_body(value: Any, *, indent: int, style: str = "dim") -> Text:
    text = Text()
    rendered = value if isinstance(value, str) else _json(value)
    rendered = rendered if rendered else "(empty)"
    for line in rendered.splitlines() or [rendered]:
        text.append(f"{' ' * indent}{line}\n", style=style)
    return text


def _python_body(code: str, *, indent: int) -> Text:
    source = code or "(empty)"
    highlighted = Syntax(
        source,
        "python",
        theme="monokai",
        background_color="default",
        word_wrap=False,
    ).highlight(source)
    text = Text()
    for line in highlighted.split("\n", allow_blank=True):
        text.append(" " * indent)
        text.append_text(line)
        text.append("\n")
    return text


def _line_count(text: Text) -> int:
    return max(1, text.plain.count("\n"))


@dataclass(frozen=True)
class _InspectorRow:
    path: InspectorPath | None
    label: str
    depth: int
    style: str = ""
    expandable: bool = False
    expanded: bool = False
    body: Callable[[], Text] | None = None
    body_token: tuple[str, bool] = ("", False)


@dataclass
class _HierarchyLayout:
    """Indexed row geometry for one immutable inspector hierarchy."""

    key: InspectorLayoutKey
    leading: Text
    leading_lines: int
    rows: list[_InspectorRow]
    heights: list[int]
    fenwick: list[int]
    header_rows: dict[InspectorPath, int]

    @classmethod
    def create(
        cls,
        key: InspectorLayoutKey,
        leading: Text,
        rows: list[_InspectorRow],
        cache: dict[InspectorPath, tuple[tuple[str | bool | int, ...], Text]],
    ) -> _HierarchyLayout:
        heights = [
            _line_count(cache[row.path][1])
            if row.body is not None
            and row.path is not None
            and row.path in cache
            and cache[row.path][0][:-1] == row.body_token
            else (_BODY_ESTIMATE_LINES if row.body is not None else 1)
            for row in rows
        ]
        fenwick = [0] * (len(rows) + 1)
        layout = cls(
            key,
            leading,
            _line_count(leading),
            rows,
            heights,
            fenwick,
            {
                row.path: index
                for index, row in enumerate(rows)
                if row.path is not None and row.body is None
            },
        )
        for index, height in enumerate(heights):
            layout._add(index, height)
        return layout

    def _add(self, index: int, delta: int) -> None:
        index += 1
        while index < len(self.fenwick):
            self.fenwick[index] += delta
            index += index & -index

    def _prefix(self, index: int) -> int:
        total = 0
        while index:
            total += self.fenwick[index]
            index -= index & -index
        return total

    @property
    def total_lines(self) -> int:
        return self.leading_lines + self._prefix(len(self.rows))

    def bounds(self, index: int) -> tuple[int, int]:
        start = self.leading_lines + self._prefix(index)
        return start, start + self.heights[index]

    def row_at(self, line: int) -> int:
        """Return the row containing a non-leading virtual line."""
        target = max(0, line - self.leading_lines)
        low = 0
        high = len(self.rows)
        while low < high:
            middle = (low + high) // 2
            if self._prefix(middle + 1) <= target:
                low = middle + 1
            else:
                high = middle
        return low

    def resize(self, index: int, height: int) -> None:
        delta = height - self.heights[index]
        if delta:
            self.heights[index] = height
            self._add(index, delta)


class _VirtualInspector(Static):
    """Viewport-bounded rendering over an indexed inspector hierarchy."""

    DEFAULT_CSS = """
    _VirtualInspector {
        width: 100%;
        height: auto;
        padding: 0 1;
    }
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._body_cache: dict[InspectorPath, tuple[tuple[str | bool | int, ...], Text]] = {}
        self._layout: _HierarchyLayout | None = None

    def _viewport(self) -> tuple[int, int]:
        parent = self.parent
        if parent is None:
            return (0, 24)
        return (int(parent.scroll_y), max(1, parent.size.height))

    @staticmethod
    def _header(row: _InspectorRow, selected: bool) -> Text:
        marker = "▶" if selected else " "
        disclosure = "▾" if row.expanded else "▸"
        text = Text()
        text.append(f"{' ' * row.depth}{marker} ")
        text.append(f"{disclosure} " if row.expandable else "  ")
        text.append(f"{row.label}\n", style="reverse" if selected else row.style)
        return text

    def _body(self, row: _InspectorRow) -> Text | None:
        if row.path is None or row.body is None:
            return None
        body_token = (*row.body_token, self.size.width)
        cached = self._body_cache.get(row.path)
        if cached is not None and cached[0] == body_token:
            return cached[1]
        body = Text("\n").join(self._wrap_body(row.body()))
        self._body_cache[row.path] = (body_token, body)
        return body

    def _wrap_body(self, body: Text) -> list[Text]:
        return list(body.wrap(self.app.console, max(1, self.size.width)))

    def _render_indexed(
        self,
        store: Any,
        key: tuple[str, str, str, str, int, bool],
        build: Callable[[], tuple[Text, list[_InspectorRow]]],
    ) -> Text:
        layout_key: InspectorLayoutKey = (*key, self.size.width)
        if self._layout is None or self._layout.key != layout_key:
            leading, rows = build()
            self._layout = _HierarchyLayout.create(layout_key, leading, rows, self._body_cache)
        return self._render_virtual(self._layout, store)

    def _materialize_visible_body(self, layout: _HierarchyLayout, index: int) -> None:
        row = layout.rows[index]
        if row.body is None:
            return
        body = self._body(row)
        if body is not None:
            layout.resize(index, _line_count(body))

    @staticmethod
    def _append_text_lines(text: Text, source: Text, start: int, end: int) -> None:
        for line in source.split("\n", allow_blank=False)[start:end]:
            text.append_text(line)
            text.append("\n")

    def _render_window(
        self, layout: _HierarchyLayout, store: Any, start: int, height: int
    ) -> Text:
        end = min(layout.total_lines, start + height)
        if end <= start:
            return Text()
        first = layout.row_at(start) if start >= layout.leading_lines else 0
        last = layout.row_at(max(start, end - 1)) + 1
        for index in range(first, min(len(layout.rows), last)):
            row_start, row_end = layout.bounds(index)
            if row_start < end and row_end > start:
                self._materialize_visible_body(layout, index)
        end = min(layout.total_lines, start + height)
        first = layout.row_at(start) if start >= layout.leading_lines else 0
        last = layout.row_at(max(start, end - 1)) + 1
        text = Text()
        if start < layout.leading_lines:
            self._append_text_lines(text, layout.leading, start, min(end, layout.leading_lines))
        for index in range(first, min(len(layout.rows), last)):
            row = layout.rows[index]
            row_start, row_end = layout.bounds(index)
            if row_end <= start or row_start >= end:
                continue
            source = (
                self._body(row)
                if row.body is not None
                else self._header(row, row.path == store.trace_selected_path)
            )
            if source is not None:
                self._append_text_lines(
                    text,
                    source,
                    max(0, start - row_start),
                    min(row_end, end) - row_start,
                )
        return text

    def _render_virtual(self, layout: _HierarchyLayout, store: Any) -> Text:
        scroll_y, viewport_height = self._viewport()
        return self._render_window(
            layout,
            store,
            scroll_y,
            viewport_height + _VIEWPORT_BUFFER_LINES,
        )

    def get_content_height(self, container: Any, viewport: Any, width: int) -> int:
        self.render()
        return 0 if self._layout is None else self._layout.total_lines

    def render_line(self, y: int) -> Strip:
        if self._layout is None:
            self.render()
        if self._layout is None:
            return Strip.blank(self.size.width)
        text = self._render_window(self._layout, self.app.store, y, 1)
        options = self.app.console.options.update(width=self.size.width, height=1)
        return Strip.from_lines(self.app.console.render_lines(text, options))[0].apply_style(
            self.visual_style.rich_style
        )

    def scroll_selected_into_view(self) -> None:
        """Scroll the outer inspector only when its selected row is clipped."""
        layout = self._layout
        selected = self.app.store.trace_selected_path
        if layout is None or selected is None:
            return
        index = layout.header_rows.get(selected)
        parent = self.parent
        if index is None or parent is None:
            return
        start, end = layout.bounds(index)
        viewport_start = int(parent.scroll_y)
        if (
            self.app.store.trace_show_full_output
            and len(selected) == 3
            and selected[-1] == "output"
        ):
            parent.scroll_to(y=start, animate=False)
            return
        viewport_end = viewport_start + max(1, parent.size.height)
        if start < viewport_start:
            parent.scroll_to(y=start, animate=False)
        elif end > viewport_end:
            parent.scroll_to(y=max(0, end - max(1, parent.size.height)), animate=False)


def _append_live_status(text: Text, events: list[dict[str, Any]]) -> None:
    """Summarize recent live evidence without materializing event payloads."""
    text.append(f"LIVE AGENT STATUS · {len(events)} update(s)\n", style="bold #b38cff")
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
    for event in events[-16:]:
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
        text.append(
            f"{'!' if failed else '•'} {labels.get(kind, kind)}{detail}\n",
            style="red" if failed else "",
        )


def _trace_leading(store: Any) -> tuple[Text, list[dict[str, Any]], bool]:
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
        message = {
            "pending": "This agent step has not run yet for this run.\n",
            "running": "Status: running · waiting for live updates\n",
        }.get(state_label, "Trace unavailable or malformed\n")
        text.append(
            message, style="yellow" if state_label in {"pending", "running"} else "bold red"
        )
        return text, [], False

    trace = envelope.get("trace")
    if not isinstance(trace, dict):
        text.append(f"Status: {envelope.get('status') or 'unavailable'}\n", style="yellow")
        if envelope.get("error"):
            text.append(f"Error: {envelope['error']}\n", style="bold red")
        _append_live_status(text, store.selected_agent_events)
        steps = store.selected_agent_live_steps
        text.append(f"LIVE TRACE · {len(steps)} turn(s)\n", style="bold #b38cff")
        text.append(_TRACE_CONTROLS, style="dim")
        return text, steps, True

    evidence = trace.get("evidence") if isinstance(trace.get("evidence"), dict) else None
    status = str(envelope.get("status") or "unavailable")
    run_id = envelope.get("run_id") or (evidence or {}).get("run_id") or "—"
    status_style = "green" if status in {"completed", "max_iterations"} else "yellow"
    if status in {"error", "unavailable"}:
        status_style = "bold red"
    text.append(f"Status: {status}", style=status_style)
    text.append(f" · Run: {run_id}\n")
    if envelope.get("error"):
        text.append(f"Error: {envelope['error']}\n", style="bold red")
    usage = trace.get("usage") if isinstance(trace.get("usage"), dict) else {}
    text.append(
        f"Model: {trace.get('model', '—')} · Sub-model: {trace.get('sub_model') or '—'}\n"
    )
    text.append(
        f"Turns: {trace.get('iterations', 0)}/{trace.get('max_iterations', '—')}"
        f" · Duration: {trace.get('duration_ms', 0)}ms\n"
    )
    text.append(f"Main: {_usage_label(usage.get('main'))}\n")
    text.append(f"Sub:  {_usage_label(usage.get('sub'))}\n")
    if evidence is not None:
        text.append(
            f"Live record: {'complete' if evidence.get('complete') else 'incomplete'}"
            f" · terminal={evidence.get('terminal_outcome') or 'unknown'}\n",
            style="green" if evidence.get("complete") else "yellow",
        )
    steps = store.selected_agent_steps
    text.append(f"\nSTRUCTURED TRACE · {len(steps)} turn(s)\n", style="bold #b38cff")
    text.append(_TRACE_CONTROLS, style="dim")
    return text, steps, False


class AgentTraceInspector(_VirtualInspector):
    """Virtualized, keyboard-addressable hierarchy of agent turns."""

    def render(self) -> Text:
        store = self.app.store
        if not _is_active_inspector_tab(store, "trace"):
            self._layout = None
            return Text()

        def build() -> tuple[Text, list[_InspectorRow]]:
            leading, steps, live = _trace_leading(store)
            if not steps:
                leading.append("No executable turn captured yet.\n", style="dim")
                return leading, []
            max_turns = len(steps)
            envelope = store.selected_agent_trace_envelope
            trace = envelope.get("trace") if isinstance(envelope, dict) else None
            if isinstance(trace, dict):
                max_turns = trace.get("max_iterations") or max_turns
            return leading, self._rows(store, steps, max_turns=max_turns, live=live)

        return self._render_indexed(store, _layout_key(store, "trace"), build)

    @staticmethod
    def _section(
        path: InspectorPath,
        label: str,
        depth: int,
        value: Any,
        *,
        store: Any,
        style: str = "dim",
        python: bool = False,
        body_token: tuple[str, bool],
    ) -> list[_InspectorRow]:
        expanded = path in store.trace_expanded_items
        rows = [_InspectorRow(path, label, depth, "bold #b38cff", True, expanded)]
        if expanded:
            if python:

                def body(value: Any = value, depth: int = depth) -> Text:
                    return _python_body(str(value or ""), indent=depth + 4)

            else:

                def body(value: Any = value, depth: int = depth, style: str = style) -> Text:
                    return _value_body(value, indent=depth + 4, style=style)

            rows.append(
                _InspectorRow(path + ("body",), "", depth + 2, body=body, body_token=body_token)
            )
        return rows

    @classmethod
    def _rows(
        cls, store: Any, steps: list[dict[str, Any]], *, max_turns: int, live: bool
    ) -> list[_InspectorRow]:
        rows: list[_InspectorRow] = []
        body_token = (store.selected_agent_trace_content_token, store.trace_show_full_output)
        for index, step in enumerate(steps):
            turn_path = ("turn", str(index))
            collapsed = index in store.trace_collapsed_turns
            tool_calls = (
                step.get("tool_calls") if isinstance(step.get("tool_calls"), list) else []
            )
            tool_count = step.get("tool_count")
            tool_count = tool_count if isinstance(tool_count, int) else len(tool_calls)
            predict_count = step.get("predict_count")
            predict_count = (
                predict_count if isinstance(predict_count, int) else _count_predict_calls(step)
            )
            label = (
                f"AGENT TURN {step.get('iteration', index + 1)}/{max_turns}"
                f" · {step.get('duration_ms', 0)}ms · "
                f"{tool_count} tool · {predict_count} predict"
                f"{' · LIVE' if live else ''}{' · ERROR' if step.get('error') else ''}"
            )
            rows.append(
                _InspectorRow(
                    turn_path,
                    label,
                    0,
                    "bold red" if step.get("error") else "bold #60dce4",
                    True,
                    not collapsed,
                )
            )
            if collapsed:
                continue
            rows.extend(
                cls._section(
                    turn_path + ("reasoning",),
                    "Reasoning",
                    2,
                    step.get("reasoning"),
                    store=store,
                    style="dim italic",
                    body_token=body_token,
                )
            )
            rows.extend(
                cls._section(
                    turn_path + ("code",),
                    "Code",
                    2,
                    step.get("code"),
                    store=store,
                    python=True,
                    body_token=body_token,
                )
            )
            output_key = "untruncated_output" if store.trace_show_full_output else "output"
            output_label = "Output (full)" if store.trace_show_full_output else "Output"
            rows.extend(
                cls._section(
                    turn_path + ("output",),
                    output_label,
                    2,
                    step.get(output_key) or "(no output)",
                    store=store,
                    style="red" if step.get("error") else "green",
                    body_token=body_token,
                )
            )
            rows.extend(cls._tool_rows(store, turn_path, tool_calls, body_token))
            groups = (
                step.get("predict_calls") if isinstance(step.get("predict_calls"), list) else []
            )
            rows.extend(cls._predict_rows(store, turn_path, groups, body_token))
            metadata = {
                "finish_reason": step.get("lm", {}).get("finish_reason")
                if isinstance(step.get("lm"), dict)
                else None,
                "usage": step.get("usage") if isinstance(step.get("usage"), dict) else {},
            }
            rows.extend(
                cls._section(
                    turn_path + ("metadata",),
                    "Metadata",
                    2,
                    metadata,
                    store=store,
                    body_token=body_token,
                )
            )
        return rows

    @classmethod
    def _tool_rows(
        cls,
        store: Any,
        turn_path: InspectorPath,
        calls: list[Any],
        body_token: tuple[str, bool],
    ) -> list[_InspectorRow]:
        path = turn_path + ("tools",)
        expanded = path in store.trace_expanded_items
        rows = [_InspectorRow(path, f"Tools ({len(calls)})", 2, "bold #b38cff", True, expanded)]
        if not expanded:
            return rows
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            call_path = path + (str(index),)
            call_expanded = call_path in store.trace_expanded_items
            rows.append(
                _InspectorRow(
                    call_path,
                    f"Tool · {call.get('name', 'unknown')}",
                    4,
                    "bold magenta",
                    True,
                    call_expanded,
                )
            )
            if call_expanded:
                rows.extend(
                    cls._section(
                        call_path + ("input",),
                        "Input",
                        6,
                        {key: call.get(key) for key in ("args", "kwargs") if call.get(key)},
                        store=store,
                        body_token=body_token,
                    )
                )
                rows.extend(
                    cls._section(
                        call_path + ("result",),
                        "Error" if call.get("error") else "Result",
                        6,
                        call.get("error") or call.get("result"),
                        store=store,
                        style="red" if call.get("error") else "dim",
                        body_token=body_token,
                    )
                )
        return rows

    @classmethod
    def _predict_rows(
        cls,
        store: Any,
        turn_path: InspectorPath,
        groups: list[Any],
        body_token: tuple[str, bool],
    ) -> list[_InspectorRow]:
        path = turn_path + ("predict",)
        expanded = path in store.trace_expanded_items
        predict_calls = sum(
            len(group.get("calls") or []) for group in groups if isinstance(group, dict)
        )
        rows = [
            _InspectorRow(
                path,
                f"Predict details ({predict_calls})",
                2,
                "bold #b38cff",
                True,
                expanded,
            )
        ]
        if not expanded:
            return rows
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            group_path = path + (str(group_index),)
            group_expanded = group_path in store.trace_expanded_items
            rows.append(
                _InspectorRow(
                    group_path,
                    (
                        f"Predict · {group.get('signature', 'unknown')}"
                        f" · {group.get('model', '—')}"
                    ),
                    4,
                    "bold magenta",
                    True,
                    group_expanded,
                )
            )
            if not group_expanded:
                continue
            calls = group.get("calls") if isinstance(group.get("calls"), list) else []
            for call_index, call in enumerate(calls):
                if not isinstance(call, dict):
                    continue
                call_path = group_path + (str(call_index),)
                call_expanded = call_path in store.trace_expanded_items
                rows.append(
                    _InspectorRow(
                        call_path, f"Call {call_index + 1}", 6, "bold", True, call_expanded
                    )
                )
                if call_expanded:
                    rows.extend(
                        cls._section(
                            call_path + ("input",),
                            "Input",
                            8,
                            call.get("input"),
                            store=store,
                            body_token=body_token,
                        )
                    )
                    rows.extend(
                        cls._section(
                            call_path + ("output",),
                            "Error" if call.get("error") else "Output",
                            8,
                            call.get("error") or call.get("output"),
                            store=store,
                            style="red" if call.get("error") else "dim",
                            body_token=body_token,
                        )
                    )
        return rows


class AgentOutputInspector(_VirtualInspector):
    """Virtualized hierarchy of the final structured agent outputs."""

    def render(self) -> Text:
        store = self.app.store
        if not _is_active_inspector_tab(store, "output"):
            self._layout = None
            return Text()

        def build() -> tuple[Text, list[_InspectorRow]]:
            node_id = store.selected_agent_node_id
            workflow = store.current_workflow
            display_name = (
                workflow.display_names.get(node_id, node_id)
                if workflow is not None and node_id is not None
                else "Agent step"
            )
            leading = Text()
            _append_tabs(leading, "output")
            leading.append(f"AGENT OUTPUT · {display_name}\n", style="bold #60dce4")
            outputs = store.selected_agent_outputs
            if outputs is None:
                state = _agent_state_label(store, node_id)
                if state == "pending":
                    message = "This agent step has not run yet for this run.\n"
                    style = "yellow"
                elif state == "running":
                    message = "Output will be available after this agent step completes.\n"
                    style = "yellow"
                else:
                    message = "Output unavailable\n"
                    style = "bold red"
                leading.append(message, style=style)
                return leading, []
            metadata = store.selected_agent_metadata
            signature = metadata.get("signature") if isinstance(metadata, dict) else None
            fields = signature.get("outputs") if isinstance(signature, dict) else None
            declared = (
                [
                    field
                    for field in fields
                    if isinstance(field, dict) and isinstance(field.get("name"), str)
                ]
                if isinstance(fields, list)
                else []
            )
            names = [field["name"] for field in declared] or list(outputs)
            names.extend(name for name in outputs if name not in names)
            descriptions = {field["name"]: field for field in declared}
            trace_token = store.selected_agent_trace_content_token
            rows: list[_InspectorRow] = []
            for name in names:
                path = ("output", name)
                field = descriptions.get(name)
                label = name
                if field is not None:
                    if isinstance(field.get("annotation"), str) and field["annotation"]:
                        label += f": {field['annotation']}"
                    if isinstance(field.get("description"), str) and field["description"]:
                        label += f" — {field['description']}"
                expanded = path in store.trace_expanded_items
                rows.append(_InspectorRow(path, label, 0, "bold #b38cff", True, expanded))
                if expanded:
                    value = outputs[name] if name in outputs else "Unavailable"
                    rows.append(
                        _InspectorRow(
                            path + ("body",),
                            "",
                            2,
                            body=lambda value=value: _value_body(value, indent=4),
                            body_token=(trace_token, name in outputs),
                        )
                    )
            return leading, rows

        return self._render_indexed(store, _layout_key(store, "output"), build)


class AgentMetadataInspector(_VirtualInspector):
    """Virtualized hierarchy of static agent declaration metadata."""

    _SECTIONS = (
        ("signature", "SIGNATURE"),
        ("runtime", "MODEL / RUNTIME SETTINGS"),
        ("skills", "SKILLS"),
        ("aggregated_static_instructions", "AGGREGATED STATIC INSTRUCTIONS"),
        ("packages", "PACKAGES"),
        ("modules", "MODULES"),
        ("tools", "TOOLS"),
    )

    def render(self) -> Text:
        store = self.app.store
        if not _is_active_inspector_tab(store, "metadata"):
            self._layout = None
            return Text()

        def build() -> tuple[Text, list[_InspectorRow]]:
            node_id = store.selected_agent_node_id
            workflow = store.current_workflow
            display_name = (
                workflow.display_names.get(node_id, node_id)
                if workflow is not None and node_id is not None
                else "Agent step"
            )
            leading = Text()
            _append_tabs(leading, "metadata")
            leading.append(f"AGENT METADATA · {display_name}\n", style="bold #60dce4")
            metadata = store.selected_agent_metadata
            if metadata is None:
                leading.append("Metadata unavailable or malformed\n", style="bold red")
                return leading, []
            rows: list[_InspectorRow] = []
            body_token = (store.selected_agent_metadata_content_token, False)
            for key, label in self._SECTIONS:
                path = ("metadata", key)
                expanded = path in store.trace_expanded_items
                rows.append(_InspectorRow(path, label, 0, "bold #b38cff", True, expanded))
                if expanded:
                    value = metadata.get(key)
                    rows.append(
                        _InspectorRow(
                            path + ("body",),
                            "",
                            2,
                            body=lambda value=value: _value_body(value, indent=4),
                            body_token=body_token,
                        )
                    )
            return leading, rows

        return self._render_indexed(store, _layout_key(store, "metadata"), build)
