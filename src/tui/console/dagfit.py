"""Fit Avalanche's left-to-right DAG rendering into a width by folding it into stacked rows.

The renderer lays a flow out as one horizontal sequence of top-level steps (nodes and parallel
groups). When that is wider than the pane, split the sequence into consecutive chunks that each
fit, render every chunk separately, and stack them with a `↳` continuation marker, the way
multi-track graphs already stack. Skip edges that cross a fold are listed under the graph
instead of drawn. One step that alone is wider than the pane is kept whole (scroll then).
"""

from __future__ import annotations

from rich.text import Text

from ..dag_layout import DagNode, ParGroup, SeqGroup, render_dag_rich

GAP = 1  # Blank rows between folded rows.
MARKERS = 4  # "↳ " indent before a continued row + " ↴" after a folded one.


def _names(steps) -> set[str]:
    names: set[str] = set()
    for step in steps:
        if isinstance(step, DagNode):
            names.add(step.name)
        elif isinstance(step, ParGroup):
            for branch in step.branches:
                names |= _names(branch.steps)
    return names


def _nodes(steps) -> list[DagNode]:
    out: list[DagNode] = []
    for step in steps:
        if isinstance(step, DagNode):
            out.append(step)
        elif isinstance(step, ParGroup):
            for branch in step.branches:
                out.extend(_nodes(branch.steps))
    return out


MARK = "#b898e0"  # Glacier muted; recolor() maps it to the theme's muted colour.


def _mark_start(line: Text) -> Text:
    """Put `↳ ` right before the row's first drawn cell (the indent guarantees room)."""
    plain = line.plain
    first = len(plain) - len(plain.lstrip(" "))
    if first < 2:
        return Text("↳ ", style=MARK) + line
    return line[: first - 2] + Text("↳ ", style=MARK) + line[first:]


def _render(steps, skips, statuses, frame, selected, elapsed) -> list[Text]:
    return render_dag_rich(
        SeqGroup(steps=list(steps), skip_edges=skips), statuses, frame, selected, elapsed
    )


def _width(lines: list[Text]) -> int:
    return max((line.cell_len for line in lines), default=0)


def _chunk_skips(steps, skips):
    names = _names(steps)
    return [(src, dst) for src, dst in skips if src in names and dst in names]


def render_fitted(
    graph: SeqGroup, statuses, frame, selected, elapsed, width: int
) -> tuple[list[Text], list[tuple[str, str]]]:
    """Render `graph` within `width` columns → (lines, skip edges not drawn)."""
    lines = render_dag_rich(graph, statuses, frame, selected, elapsed)
    if _width(lines) <= width or getattr(graph, "tracks", None) or len(graph.steps) < 2:
        return lines, []
    # Greedy: extend each chunk while its own rendering still fits.
    chunks: list[list] = []
    current: list = []
    for step in graph.steps:
        trial = current + [step]
        fits = (
            _width(
                _render(
                    trial,
                    _chunk_skips(trial, graph.skip_edges),
                    statuses,
                    frame,
                    selected,
                    elapsed,
                )
            )
            + MARKERS
            <= width
        )
        if current and not fits:
            chunks.append(current)
            current = [step]
        else:
            current = trial
    if current:
        chunks.append(current)
    # Final render per chunk in order (rendering assigns node positions), then offset rows.
    out: list[Text] = []
    for index, chunk in enumerate(chunks):
        part = _render(
            chunk, _chunk_skips(chunk, graph.skip_edges), statuses, frame, selected, elapsed
        )
        if index:
            out.extend(Text() for _ in range(GAP))
        offset, indent = len(out), 2 if index else 0
        for node in _nodes(chunk):
            if node.render_row is not None:
                node.render_row += offset
            if node.caption_render_row is not None:
                node.caption_render_row += offset
            if node.render_col is not None:
                node.render_col += indent
        middle = len(part) // 2
        for row, line in enumerate(part):
            line = Text(" " * indent) + line if indent else line
            if index and row == middle:
                line = _mark_start(line)
            if index < len(chunks) - 1 and row == middle:
                line = line.copy()
                line.rstrip()
                line.append(" ↴", style=MARK)
            out.append(line)
    drawn = {edge for chunk in chunks for edge in _chunk_skips(chunk, graph.skip_edges)}
    return out, [edge for edge in graph.skip_edges if edge not in drawn]
