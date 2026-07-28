"""DAG layout engine — structure, rendering, and navigation.

Converts a WorkflowInfo (flat adjacency list) into a SeqGroup/ParGroup tree
for Rich-text rendering with bracket notation for parallel branches.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from rich.style import Style
from rich.text import Text

from .models import NodeStatus, WorkflowInfo, display_name_from_id
from .theme import (
    AGENT_CAPTION_SELECTED_STYLE,
    AGENT_CAPTION_STYLE,
    ARROW_STYLE,
    BRACKET_STYLE,
    DIM_STYLE,
    ICE_FROST,
    ICE_STEEL,
    SKIP_EDGE_COLORS,
    SPINNER_FRAMES,
    STATUS_CHARS,
    STATUS_STYLES,
    VIRTUAL_LABELS,
    VIRTUAL_STYLE,
)

# ── Layout tree nodes ─────────────────────────────────────────────────────


@dataclass
class DagNode:
    """View-model node for DAG rendering. Status comes from RunState at render time."""

    name: str  # node_id — unique graph identity (e.g. "fetch_orders_1")
    node_type: str  # "source" | "step" | "dest" | "virtual"
    display_name: str = ""  # human-readable label (e.g. "fetch_orders")
    is_agent: bool = False
    col: int = 0
    row: int = 0
    render_row: int | None = None
    caption_render_row: int | None = None
    render_col: int | None = None
    caption_col: int | None = None

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.name

    @property
    def virtual(self) -> bool:
        return self.node_type == "virtual"


@dataclass
class SeqGroup:
    steps: list[DagNode | ParGroup] = field(default_factory=list)
    skip_edges: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ParGroup:
    branches: list[SeqGroup] = field(default_factory=list)


# ── Workflow → layout conversion ──────────────────────────────────────────


def _topo_depth(graph: dict[str, list[str]], node_ids: list[str]) -> dict[str, int]:
    """Compute the longest-path depth for each node (topological level)."""
    parents_of: dict[str, list[str]] = defaultdict(list)
    for parent, children in graph.items():
        for child in children:
            parents_of[child].append(parent)
    depth: dict[str, int] = {}
    def _get_depth(nid: str) -> int:
        if nid in depth:
            return depth[nid]
        pars = parents_of.get(nid, [])
        d = max((_get_depth(p) + 1 for p in pars), default=0)
        depth[nid] = d
        return d
    for nid in node_ids:
        _get_depth(nid)
    return depth


def _extract_skip_edges(
    graph: dict[str, list[str]], node_ids: list[str],
) -> tuple[dict[str, list[str]], list[tuple[str, str]]]:
    """Remove skip edges from the graph and return them separately.

    A skip edge jumps over intermediate nodes — the target's topo depth
    is more than 1 level ahead of the source. These create node duplication
    in the layout. We remove them and render as dotted annotations instead.
    """
    depth = _topo_depth(graph, node_ids)

    children_of = defaultdict(list)
    for p, cs in graph.items():
        children_of[p] = list(cs)

    clean_graph: dict[str, list[str]] = {}
    skip_edges: list[tuple[str, str]] = []
    for parent, children in graph.items():
        normal = []
        for child in children:
            if depth.get(child, 0) - depth.get(parent, 0) > 1:
                # Long-range edge — skip it if the child is still
                # reachable from the parent through other paths.
                # (Even for convergence nodes: a redundant shortcut
                # causes node duplication in the layout.)
                other_kids = [c for c in children if c != child]
                still_reachable = any(
                    child in _reachable_between(ok, None, children_of)
                    for ok in other_kids
                )
                if still_reachable:
                    skip_edges.append((parent, child))
                else:
                    normal.append(child)
            else:
                normal.append(child)
        if normal:
            clean_graph[parent] = normal
    return clean_graph, skip_edges


def _reachable_between(
    start: str, stop: str | None, children_of: dict[str, list[str]],
) -> set[str]:
    """All nodes reachable from start via BFS, not crossing stop."""
    visited: set[str] = set()
    queue = [start]
    while queue:
        n = queue.pop(0)
        if n == stop or n in visited:
            continue
        visited.add(n)
        queue.extend(children_of.get(n, []))
    return visited


def workflow_to_layout(info: WorkflowInfo) -> tuple[SeqGroup, list[DagNode]]:
    """Convert WorkflowInfo into a SeqGroup/ParGroup tree.

    Uses a level-based approach mirroring the executor's topological sort:

    Phase 1 — Assign each node a topological level (= column position).
    Phase 2 — At fork points, find the join via minimum-level common descendant.
    Phase 3 — Build SeqGroup/ParGroup tree with cross-fork fan-in dedup.

    Skip edges (cross-cutting deps that jump multiple levels) are extracted
    first and rendered as dotted annotations.

    Returns (layout_tree, all_non_virtual_nodes).
    """
    # ── Phase 1: extract skip edges, compute levels ──────────────────────
    clean_graph, skip_edges = _extract_skip_edges(info.graph, info.node_ids)
    node_types = info.node_types

    children_of: dict[str, list[str]] = defaultdict(list)
    for parent, children in clean_graph.items():
        children_of[parent] = list(children)

    parents_of: dict[str, list[str]] = defaultdict(list)
    for parent, children in clean_graph.items():
        for child in children:
            parents_of[child].append(parent)

    level = _topo_depth(clean_graph, info.node_ids)
    node_order = {nid: i for i, nid in enumerate(info.node_ids)}
    agent_node_ids = frozenset(info.agent_node_ids)

    all_nodes: list[DagNode] = []
    roots = [nid for nid in info.node_ids if not parents_of.get(nid)]

    def make_node(nid: str) -> DagNode:
        nt = node_types.get(nid, "step")
        dname = info.display_names.get(nid) or display_name_from_id(nid)
        dn = DagNode(
            name=nid,
            node_type=nt,
            display_name=dname,
            is_agent=nid in agent_node_ids,
        )
        all_nodes.append(dn)
        return dn

    # ── Level-based join detection ───────────────────────────────────────

    def _join_of(starts: list[str], stop: str | None) -> str | None:
        """Find the lowest-level node reachable from ALL starts.

        Uses topological levels instead of BFS order — more correct for
        complex DAGs where BFS distance ≠ topological depth.
        """
        if len(starts) < 2:
            return None
        reach_sets = [_reachable_between(s, stop, children_of) for s in starts]
        common = set.intersection(*reach_sets)
        if not common:
            return None
        return min(common, key=lambda n: (level.get(n, 0), node_order.get(n, 0)))

    # ── Phase 2 & 3: build layout tree ───────────────────────────────────

    def _build(
        starts: list[str],
        stop: str | None = None,
        blocked: frozenset[str] = frozenset(),
    ) -> list[DagNode | ParGroup]:
        """Recursively build layout tree from frontier nodes."""
        if len(starts) > 1:
            # Parallel entry — find join via levels
            join = _join_of(starts, stop)
            branches = []
            for s in starts:
                steps = _build([s], join, blocked)
                if steps:
                    branches.append(SeqGroup(steps=steps))
            result: list[DagNode | ParGroup] = []
            if len(branches) == 1:
                result = list(branches[0].steps)
            elif branches:
                result = [ParGroup(branches=branches)]
            if join and join != stop:
                result.extend(_build([join], stop, blocked))
            return result

        # Single start
        nid = starts[0]
        if nid == stop or nid in blocked:
            return []

        node = make_node(nid)
        kids = children_of.get(nid, [])

        if not kids or all(k == stop for k in kids):
            return [node]

        kids = [k for k in kids if k != stop]
        blocked_kids = [k for k in kids if k in blocked]
        for bk in blocked_kids:
            skip_edges.append((nid, bk))
        kids = [k for k in kids if k not in blocked]

        if not kids:
            return [node]
        if len(kids) == 1:
            return [node] + _build(kids, stop, blocked)

        # Fork — find join via levels, dedup cross-fork fan-in
        join = _join_of(kids, stop)
        if join == stop:
            join = None
        effective_stop = join or stop

        # Cross-fork fan-in dedup: nodes reachable from multiple branches
        # are assigned to the first owning branch, blocked in others.
        branch_reachable = [
            _reachable_between(k, effective_stop, children_of) for k in kids
        ]
        node_owner: dict[str, int] = {}
        cross_nodes: set[str] = set()
        for bi, reachable in enumerate(branch_reachable):
            for n in reachable:
                if n not in node_owner:
                    node_owner[n] = bi
                else:
                    cross_nodes.add(n)
        # Only dedup partial convergence (not reachable from ALL branches)
        cross_nodes = {
            n for n in cross_nodes
            if not all(n in br for br in branch_reachable)
        }

        branches = []
        for i, k in enumerate(kids):
            branch_blocked = blocked | frozenset(
                n for n in cross_nodes
                if node_owner.get(n) != i and n in branch_reachable[i]
            )
            if k in branch_blocked:
                skip_edges.append((nid, k))
                continue
            steps = _build([k], effective_stop, branch_blocked)
            if steps:
                branches.append(SeqGroup(steps=steps))

        if not branches:
            result = [node]
        elif len(branches) == 1:
            result = [node] + branches[0].steps
        else:
            result = [node, ParGroup(branches=branches)]
        if join and join != stop:
            result.extend(_build([join], stop, blocked))
        return result

    # ── Split roots into connected components ────────────────────────────
    # Use ORIGINAL graph for reachability so skip-edge targets still
    # connect their source roots.
    orig_children: dict[str, list[str]] = defaultdict(list)
    for parent, children in info.graph.items():
        orig_children[parent] = list(children)
    if len(roots) > 1:
        root_reachable = {r: _reachable_between(r, None, orig_children) for r in roots}
        groups: list[list[str]] = [[r] for r in roots]
        for i in range(len(roots)):
            for j in range(i + 1, len(roots)):
                if root_reachable[roots[i]] & root_reachable[roots[j]]:
                    gi = next(g for g in groups if roots[i] in g)
                    gj = next(g for g in groups if roots[j] in g)
                    if gi is not gj:
                        gi.extend(gj)
                        groups.remove(gj)
    else:
        groups = [roots]

    # ── Build tracks and wrap ────────────────────────────────────────────
    start = DagNode(name="start", node_type="virtual")
    end = DagNode(name="end", node_type="virtual")

    all_track_steps: list[list[DagNode | ParGroup]] = []
    for group in groups:
        all_track_steps.append(_build(group))

    # Post-process: when a root ParGroup has branches of very different
    # complexity (one with nested ParGroups, others simple), split into
    # separate visual tracks. This prevents _flatten_to_columns from
    # creating disconnected bracket groups.
    all_track_steps = _split_complex_root(all_track_steps, skip_edges)

    if len(all_track_steps) == 1:
        inner_steps = all_track_steps[0]
        root = SeqGroup(steps=[start] + inner_steps + [end], skip_edges=skip_edges)
    else:
        # Add virtual start/end to the primary track so they render
        all_track_steps[0] = [start] + all_track_steps[0] + [end]
        root = SeqGroup(
            steps=all_track_steps[0],
            skip_edges=skip_edges,
        )
        root.tracks = all_track_steps  # type: ignore[attr-defined]

    return root, all_nodes


def _split_complex_root(
    tracks: list[list[DagNode | ParGroup]],
    skip_edges: list[tuple[str, str]],
) -> list[list[DagNode | ParGroup]]:
    """Split tracks whose root ParGroup mixes complex and simple branches.

    When one branch has nested ParGroups (deep subtree) and others are
    simple chains, _flatten_to_columns creates disconnected bracket groups.
    Instead, split into separate visual tracks:
      - Complex branches get their own track (first one keeps convergence)
      - Simple branches form a shared track
    """
    result: list[list[DagNode | ParGroup]] = []
    for track in tracks:
        if not track or not isinstance(track[0], ParGroup):
            result.append(track)
            continue

        par = track[0]
        continuation = track[1:]  # steps after the ParGroup (convergence etc.)

        # Only split if continuation is short (just a convergence node).
        # Long continuations mean most of the workflow is after the ParGroup,
        # so splitting would disconnect the simple roots from the rest.
        cont_nodes = sum(1 for s in continuation if isinstance(s, DagNode))
        if cont_nodes > 2:
            result.append(track)
            continue

        complex_branches: list[SeqGroup] = []
        simple_branches: list[SeqGroup] = []
        for b in par.branches:
            if any(isinstance(s, ParGroup) for s in b.steps):
                complex_branches.append(b)
            else:
                simple_branches.append(b)

        if not complex_branches or not simple_branches:
            result.append(track)
            continue

        # Find the convergence node name for cross-track skip edges
        conv_name: str | None = None
        for s in continuation:
            if isinstance(s, DagNode) and not s.virtual:
                conv_name = s.name
                break

        # Record cross-track edges: simple branch endpoints → convergence
        if conv_name:
            for b in simple_branches:
                for s in reversed(b.steps):
                    if isinstance(s, DagNode) and not s.virtual:
                        skip_edges.append((s.name, conv_name))
                        break

        # Complex branches each become their own track; first gets continuation
        for i, b in enumerate(complex_branches):
            if i == 0:
                result.append(list(b.steps) + continuation)
            else:
                result.append(list(b.steps))

        # Simple branches form one shared track
        if len(simple_branches) == 1:
            result.append(list(simple_branches[0].steps))
        else:
            result.append([ParGroup(branches=simple_branches)])

    return result


def _collect_node_names(steps: list[DagNode | ParGroup]) -> set[str]:
    """Collect all non-virtual node names from a list of steps."""
    names: set[str] = set()
    for s in steps:
        if isinstance(s, DagNode) and not s.virtual:
            names.add(s.name)
        elif isinstance(s, ParGroup):
            for b in s.branches:
                names |= _collect_node_names(b.steps)
    return names


def _collect_nodes(steps: list[DagNode | ParGroup]) -> list[DagNode]:
    """Collect nodes whose render anchors need a track-row offset."""
    nodes: list[DagNode] = []
    for step in steps:
        if isinstance(step, DagNode):
            nodes.append(step)
        elif isinstance(step, ParGroup):
            for branch in step.branches:
                nodes.extend(_collect_nodes(branch.steps))
    return nodes



def _anchor_rendered_nodes(lines: list[Text], steps: list[DagNode | ParGroup]) -> None:
    """Record each node occurrence's exact label and caption columns."""
    next_col_by_row: dict[int, int] = {}
    for node in _collect_nodes(steps):
        if node.virtual or node.render_row is None:
            continue
        needle = f" {node.display_name} "
        row = node.render_row
        col = lines[row].plain.find(needle, next_col_by_row.get(row, 0))
        if col < 0:
            continue
        node.render_col = col
        node.caption_col = col + 1 if node.is_agent else None
        next_col_by_row[row] = col + len(needle)


def _reserve_drop_lanes(
    lines: list[Text],
    raw_edges: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Allocate bounded, caption-safe drop lanes for skip-edge connectors."""
    max_width = max((len(line.plain) for line in lines), default=0)
    blocked_from_row = [0] * (len(lines) + 1)
    for row in range(len(lines) - 1, -1, -1):
        blocked_columns = sum(
            1 << col for col, glyph in enumerate(lines[row].plain) if glyph != " "
        )
        blocked_from_row[row] = blocked_from_row[row + 1] | blocked_columns

    used_columns: set[int] = set()
    endpoint_lanes: dict[tuple[int, int], int] = {}
    next_gutter_column = max_width + 1

    def _allocate(preferred: int, start_row: int) -> int:
        nonlocal next_gutter_column
        blocked_columns = blocked_from_row[min(start_row, len(lines))]
        for distance in range(33):
            candidates = [preferred] if distance == 0 else [
                preferred - distance,
                preferred + distance,
            ]
            for col in candidates:
                if (
                    col >= 0
                    and not blocked_columns & (1 << col)
                    and col - 1 not in used_columns
                    and col not in used_columns
                    and col + 1 not in used_columns
                ):
                    used_columns.add(col)
                    return col
        while (
            next_gutter_column - 1 in used_columns
            or next_gutter_column in used_columns
            or next_gutter_column + 1 in used_columns
        ):
            next_gutter_column += 2
        lane = next_gutter_column
        used_columns.add(lane)
        next_gutter_column += 2
        return lane

    def _lane(row: int, anchor: int) -> int:
        key = (row, anchor)
        if key not in endpoint_lanes:
            endpoint_lanes[key] = _allocate(anchor, row + 1)
        return endpoint_lanes[key]

    return [
        (src_row, _lane(src_row, src_col), dst_row, _lane(dst_row, dst_col))
        for src_row, src_col, dst_row, dst_col in raw_edges
    ]


def _append_routed_skip_edges(
    lines: list[Text],
    edges: list[tuple[int, int, int, int]],
    raw_edges: list[tuple[int, int, int, int]],
) -> None:
    """Append caption-safe skip connectors with endpoint leaders."""
    max_col = max(max(src_col, dst_col) for _, src_col, _, dst_col in edges) + 1
    for line in lines:
        if len(line.plain) < max_col:
            line.append(" " * (max_col - len(line.plain)))

    routes = sorted(
        zip(edges, raw_edges, strict=True),
        key=lambda route: abs(route[0][3] - route[0][1]),
    )
    edge_styles = [
        Style(color=SKIP_EDGE_COLORS[index % len(SKIP_EDGE_COLORS)])
        for index in range(len(routes))
    ]
    connector_start = len(lines)
    for index, ((_, src_col, _, dst_col), _) in enumerate(routes):
        style = edge_styles[index]
        left, right = min(src_col, dst_col), max(src_col, dst_col)
        line = Text()
        line.append(" " * left)
        line.append("╰", style)
        line.append("┄" * (right - left - 1), style)
        line.append("╯", style)
        line.append(" " * (max_col - right - 1))
        lines.append(line)

    glyphs_by_row: dict[int, dict[int, tuple[str, Style]]] = {}
    leader_intervals_by_row: dict[int, list[tuple[int, int]]] = {}
    scheduled_leaders: set[tuple[int, int, int]] = set()


    def _add_endpoint_leader(
        row: int,
        lane: int,
        anchor: int,
        style: Style,
    ) -> None:
        leader_key = (row, lane, anchor)
        if leader_key in scheduled_leaders:
            return
        scheduled_leaders.add(leader_key)
        plain = lines[row].plain
        direction = 1 if lane > anchor else -1
        attachment = anchor
        while 0 <= attachment < len(plain) and plain[attachment] != " ":
            attachment += direction
        if attachment < 0 or attachment >= len(plain):
            return
        left, right = min(attachment, lane), max(attachment, lane)
        if any(plain[col] != " " for col in range(left, right + 1)):
            return
        if lane == attachment:
            return
        intervals = leader_intervals_by_row.setdefault(row, [])
        if any(
            left <= existing_right and existing_left <= right
            for existing_left, existing_right in intervals
        ):
            raise RuntimeError("overlapping DAG skip-edge endpoint leaders")
        intervals.append((left, right))
        glyphs = glyphs_by_row.setdefault(row, {})
        if lane > attachment:
            glyphs[attachment] = ("╰", style)
            for col in range(attachment + 1, lane):
                glyphs[col] = ("┄", style)
            glyphs[lane] = ("╮", style)
        else:
            glyphs[attachment] = ("╯", style)
            for col in range(lane + 1, attachment):
                glyphs[col] = ("┄", style)
            glyphs[lane] = ("╭", style)

    for index, ((src_row, src_col, dst_row, dst_col), raw_edge) in enumerate(routes):
        style = edge_styles[index]
        _, src_anchor, _, dst_anchor = raw_edge
        horizontal_row = connector_start + index
        for row in range(src_row + 1, horizontal_row):
            glyphs_by_row.setdefault(row, {})[src_col] = ("┆", style)
        for row in range(dst_row + 1, horizontal_row):
            glyphs_by_row.setdefault(row, {})[dst_col] = ("┆", style)
        _add_endpoint_leader(src_row + 1, src_col, src_anchor, style)
        _add_endpoint_leader(dst_row + 1, dst_col, dst_anchor, style)

    for row, glyphs in glyphs_by_row.items():
        line = lines[row]
        characters = list(line.plain)
        styled_glyphs: list[tuple[int, str, Style]] = []
        for col, (glyph, style) in glyphs.items():
            if characters[col] == " ":
                characters[col] = glyph
                styled_glyphs.append((col, glyph, style))
        if styled_glyphs:
            line.plain = "".join(characters)
            for col, _, style in styled_glyphs:
                line.stylize(style, col, col + 1)

def _render_cross_track_skip_edges(
    lines: list[Text],
    skip_edges: list[tuple[str, str]],
    nodes: list[DagNode],
) -> None:
    """Render cross-track skip connectors through caption-safe reserved lanes."""
    node_positions = {
        node.name: (
            node.render_row,
            node.render_col + 1 + len(node.display_name) // 2,
        )
        for node in nodes
        if not node.virtual and node.render_row is not None and node.render_col is not None
    }
    raw_edges = [
        (src_pos[0], src_pos[1], dst_pos[0], dst_pos[1])
        for src, dst in skip_edges
        if (src_pos := node_positions.get(src)) and (dst_pos := node_positions.get(dst))
    ]
    if not raw_edges:
        return

    _append_routed_skip_edges(lines, _reserve_drop_lanes(lines, raw_edges), raw_edges)


# ── Rich text rendering ───────────────────────────────────────────────────


def marker_for(node: DagNode, status: NodeStatus, frame: int) -> str:
    if node.virtual:
        return VIRTUAL_LABELS.get(node.name, node.name)
    if status == NodeStatus.RUNNING:
        return SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]
    return STATUS_CHARS[status]


def _duration_label(elapsed: float | None, status: NodeStatus) -> str:
    if elapsed is not None:
        return _fmt_elapsed(elapsed)
    if status == NodeStatus.PENDING:
        return "pending"
    if status == NodeStatus.SKIPPED:
        return "skipped"
    return "—"


def plain_node_width(
    node: DagNode, elapsed: float | None = None, status: NodeStatus = NodeStatus.PENDING,
) -> int:
    if node.virtual:
        return len(VIRTUAL_LABELS.get(node.name, node.name))
    dur = _duration_label(elapsed, status)
    primary_width = 4 + len(node.display_name) + len(dur) + 3  # " ○ name (dur) "
    if node.is_agent:
        return max(primary_width, 3 + len("(agent)"))
    return primary_width


def measure_seq(
    seq: SeqGroup,
    node_elapsed: dict[str, float | None] | None = None,
    statuses: dict[str, NodeStatus] | None = None,
) -> int:
    if node_elapsed is None:
        node_elapsed = {}
    if statuses is None:
        statuses = {}
    total = 0
    for i, step in enumerate(seq.steps):
        if i > 0:
            total += 4  # " >> "
        if isinstance(step, DagNode):
            total += plain_node_width(
                step, node_elapsed.get(step.name),
                statuses.get(step.name, NodeStatus.PENDING),
            )
        elif isinstance(step, ParGroup):
            # Inline ParGroups render only the widest branch (no brackets)
            widest_w = max(measure_seq(b, node_elapsed, statuses) for b in step.branches)
            total += widest_w
    return total


def measure_par(
    par: ParGroup,
    node_elapsed: dict[str, float | None] | None = None,
    statuses: dict[str, NodeStatus] | None = None,
) -> int:
    max_branch = max(measure_seq(b, node_elapsed, statuses) for b in par.branches)
    return 4 + max_branch + 3  # "┌── " + content + " ─┐"


def _fmt_elapsed(secs: float | None) -> str:
    """Format elapsed time with min 3 significant digits + 's' suffix."""
    if secs is None:
        return ""
    if secs < 10:
        return f"{secs:.2f}s"
    if secs < 100:
        return f"{secs:.1f}s"
    return f"{int(secs)}s"


def append_node(
    text: Text,
    node: DagNode,
    status: NodeStatus,
    frame: int,
    selected: DagNode | None,
    elapsed: float | None = None,
) -> None:
    m = marker_for(node, status, frame)
    style = STATUS_STYLES[status]
    if node.virtual:
        text.append(m, VIRTUAL_STYLE)
        return
    dur = _duration_label(elapsed, status)
    if node is selected:
        sel = Style(bgcolor=ICE_STEEL, bold=True)
        label = (
            f" {m} {node.display_name} ({dur}) "
            if dur
            else f" {m} {node.display_name} "
        )
        text.append(label, Style(color=ICE_FROST) + sel)
    else:
        text.append(f" {m}", style)
        text.append(f" {node.display_name} ", Style(color=ICE_FROST))
        if dur:
            text.append(f"({dur}) ", DIM_STYLE)

def append_node_caption(
    text: Text,
    node: DagNode,
    status: NodeStatus,
    selected: DagNode | None,
    elapsed: float | None = None,
) -> None:
    """Append a node-width caption row aligned under its display name."""
    width = plain_node_width(node, elapsed, status)
    if not node.is_agent:
        text.append(" " * width)
        return
    text.append(" " * 3)
    style = AGENT_CAPTION_SELECTED_STYLE if node is selected else AGENT_CAPTION_STYLE
    text.append("(agent)", style)
    text.append(" " * (width - 3 - len("(agent)")))


def append_seq(
    text: Text,
    seq: SeqGroup,
    statuses: dict[str, NodeStatus],
    frame: int,
    selected: DagNode | None,
    node_elapsed: dict[str, float | None] | None = None,
) -> None:
    if node_elapsed is None:
        node_elapsed = {}
    for i, step in enumerate(seq.steps):
        if i > 0:
            text.append(" >> ", ARROW_STYLE)
        if isinstance(step, DagNode):
            status = statuses.get(step.name, NodeStatus.PENDING)
            elapsed = node_elapsed.get(step.name)
            append_node(text, step, status, frame, selected, elapsed)
        elif isinstance(step, ParGroup):
            # Render the widest branch (same as what measure_par uses)
            widest = max(step.branches, key=lambda b: measure_seq(b, node_elapsed, statuses))
            append_seq(text, widest, statuses, frame, selected, node_elapsed)

def append_seq_caption(
    text: Text,
    seq: SeqGroup,
    statuses: dict[str, NodeStatus],
    selected: DagNode | None,
    node_elapsed: dict[str, float | None] | None = None,
) -> None:
    """Append captions and padding that match a sequence's primary row."""
    if node_elapsed is None:
        node_elapsed = {}
    for i, step in enumerate(seq.steps):
        if i > 0:
            text.append(" " * 4)
        if isinstance(step, DagNode):
            status = statuses.get(step.name, NodeStatus.PENDING)
            elapsed = node_elapsed.get(step.name)
            append_node_caption(text, step, status, selected, elapsed)
        elif isinstance(step, ParGroup):
            widest = max(step.branches, key=lambda b: measure_seq(b, node_elapsed, statuses))
            append_seq_caption(text, widest, statuses, selected, node_elapsed)


def render_dag_rich(
    dag: SeqGroup,
    statuses: dict[str, NodeStatus],
    frame: int,
    selected: DagNode | None,
    node_elapsed: dict[str, float | None] | None = None,
) -> list[Text]:
    """Render the DAG as a list of Rich Text lines.

    Args:
        dag: Layout tree root.
        statuses: node_name -> NodeStatus mapping from RunState.
        frame: Animation frame counter.
        selected: Currently selected node (or None).
    """
    # Multi-track rendering: independent chains get separate track rows
    tracks = getattr(dag, "tracks", None)
    if tracks and len(tracks) > 1:
        all_lines: list[Text] = []
        for ti, track_steps in enumerate(tracks):
            # Pass within-track skip edges (source AND target in this track)
            track_node_names = _collect_node_names(track_steps)
            track_skips = [
                (s, d) for s, d in dag.skip_edges
                if s in track_node_names and d in track_node_names
            ]
            track_dag = SeqGroup(steps=track_steps, skip_edges=track_skips)
            if ti > 0 and all_lines:
                all_lines.append(Text())  # blank separator between tracks
            track_offset = len(all_lines)
            track_lines = render_dag_rich(
                track_dag, statuses, frame, selected, node_elapsed,
            )
            for node in _collect_nodes(track_steps):
                if node.render_row is not None:
                    node.render_row += track_offset
                if node.caption_render_row is not None:
                    node.caption_render_row += track_offset
            all_lines.extend(track_lines)
        # Render cross-track skip edge connectors
        cross_skips = [
            (s, d) for s, d in dag.skip_edges
            if not (s in _collect_node_names(tracks[0]) and d in _collect_node_names(tracks[0]))
            or any(
                s in _collect_node_names(t) and d not in _collect_node_names(t)
                for t in tracks
            )
        ]
        if cross_skips:
            _render_cross_track_skip_edges(
                all_lines,
                cross_skips,
                [node for track in tracks for node in _collect_nodes(track)],
            )
        return all_lines
    def _max_parallel(steps: list) -> int:
        """Recursively find the max number of parallel branches in any ParGroup."""
        m = 1
        for step in steps:
            if isinstance(step, ParGroup):
                m = max(m, len(step.branches))
                for b in step.branches:
                    m = max(m, _max_parallel(b.steps))
        return m

    def _flatten_to_columns(steps: list) -> list:
        """Flatten nested SeqGroup/ParGroup tree into a linear list of steps.

        Nested ParGroups inside branches are pulled out as separate steps
        so the column-building loop can render them with their own brackets.
        """
        flat = []
        for step in steps:
            if isinstance(step, DagNode):
                flat.append(step)
            elif isinstance(step, ParGroup):
                # Flatten each branch — if a branch has a nested ParGroup,
                # split it: pre-par nodes in this ParGroup, then the nested ParGroup
                # becomes a separate top-level step
                has_nested = any(
                    isinstance(s, ParGroup) for b in step.branches for s in b.steps
                )
                if not has_nested:
                    flat.append(step)
                else:
                    # Split branches at nested ParGroups
                    # Collect the "prefix" of each branch (before nested par)
                    prefix_branches = []
                    suffix_steps = []
                    for b in step.branches:
                        prefix = []
                        for s in b.steps:
                            if isinstance(s, DagNode):
                                prefix.append(s)
                            elif isinstance(s, ParGroup):
                                # This nested par becomes a suffix
                                suffix_steps.append(s)
                                break
                        prefix_branches.append(SeqGroup(steps=prefix))
                    flat.append(ParGroup(branches=prefix_branches))
                    # Add nested ParGroups as separate top-level steps
                    for s in suffix_steps:
                        flat.extend(_flatten_to_columns([s]))
        return flat

    has_skip_edges = bool(dag.skip_edges)
    flat_steps = _flatten_to_columns(dag.steps)
    max_branches = _max_parallel(dag.steps)
    if has_skip_edges:
        logical_height = max(1, 2 * max_branches - 1)
    else:
        logical_height = max_branches
    mid = logical_height // 2

    def _seq_has_agent(seq: SeqGroup) -> bool:
        return any(
            step.is_agent
            if isinstance(step, DagNode)
            else any(_seq_has_agent(branch) for branch in step.branches)
            for step in seq.steps
        )

    logical_has_caption = [False] * logical_height
    for step in flat_steps:
        if isinstance(step, DagNode):
            logical_has_caption[mid] |= step.is_agent
        elif isinstance(step, ParGroup):
            n = len(step.branches)
            if has_skip_edges:
                spaced_h = 2 * n - 1
                row_stride = 2
            else:
                spaced_h = n
                row_stride = 1
            par_offset = (logical_height - spaced_h) // 2
            for branch_index, branch in enumerate(step.branches):
                logical_row = par_offset + branch_index * row_stride
                logical_has_caption[logical_row] |= _seq_has_agent(branch)

    logical_row_starts: list[int] = []
    physical_rows: list[int] = []
    for logical_row, has_caption in enumerate(logical_has_caption):
        logical_row_starts.append(len(physical_rows))
        physical_rows.append(logical_row)
        if has_caption:
            physical_rows.append(logical_row)
    render_height = len(physical_rows)

    columns: list[list[tuple[Text, int]]] = []
    is_virtual: list[bool] = []  # track which columns are virtual start/end

    for step in flat_steps:
        if isinstance(step, DagNode):
            step.render_row = logical_row_starts[mid]
            step.caption_render_row = step.render_row + 1 if step.is_agent else None
            elapsed = (node_elapsed or {}).get(step.name)
            status = statuses.get(step.name, NodeStatus.PENDING)
            w = plain_node_width(step, elapsed, status)
            col: list[tuple[Text, int]] = []
            for row in range(render_height):
                if row == step.render_row:
                    t = Text()
                    append_node(t, step, status, frame, selected, elapsed)
                    col.append((t, w))
                elif step.caption_render_row is not None and row == step.caption_render_row:
                    t = Text()
                    append_node_caption(t, step, status, selected, elapsed)
                    col.append((t, w))
                else:
                    col.append((Text(" " * w), w))
            columns.append(col)
            is_virtual.append(step.virtual)

        elif isinstance(step, ParGroup):
            n = len(step.branches)
            branch_data = []
            # Spaced layout when skip edges present, compact otherwise.
            if has_skip_edges:
                spaced_h = 2 * n - 1
                row_stride = 2
            else:
                spaced_h = n
                row_stride = 1
            par_offset = (logical_height - spaced_h) // 2
            for bi, b in enumerate(step.branches):
                logical_row = par_offset + bi * row_stride
                render_row = logical_row_starts[logical_row]
                for s in b.steps:
                    if isinstance(s, DagNode):
                        s.render_row = render_row
                        s.caption_render_row = render_row + 1 if s.is_agent else None
                primary = Text()
                append_seq(primary, b, statuses, frame, selected, node_elapsed)
                caption = Text()
                append_seq_caption(caption, b, statuses, selected, node_elapsed)
                branch_data.append((primary, caption, measure_seq(b, node_elapsed, statuses)))

            max_w = max(w for _, _, w in branch_data)
            col_w = 5 + max_w + 1 + 4  # open_b + content + space/pad + close_b

            col = []
            for row, logical_row in enumerate(physical_rows):
                is_caption_row = row > logical_row_starts[logical_row]
                rel = logical_row - par_offset
                if rel >= 0 and rel < spaced_h and rel % row_stride == 0:
                    branch_idx = rel // row_stride
                    primary, caption, bw = branch_data[branch_idx]
                    if is_caption_row:
                        line = Text(" " * 5)
                        line.append_text(caption)
                        line.append(" " * (col_w - 5 - bw))
                        col.append((line, col_w))
                        continue

                    pad_needed = max_w - bw
                    is_mid = logical_row == mid
                    if n == 1:
                        open_b, close_b = "──── ", "────"
                    elif branch_idx == 0:
                        open_b = "─┬── " if is_mid else " ┌── "
                        close_b = "──┬─" if is_mid else "──┐ "
                    elif branch_idx == n - 1:
                        open_b = "─┴&─ " if is_mid else " └&─ "
                        close_b = "──┴─" if is_mid else "──┘ "
                    else:
                        open_b = "─├&─ " if is_mid else " ├&─ "
                        close_b = "──┤─" if is_mid else "──┤ "

                    line = Text()
                    line.append(open_b, BRACKET_STYLE)
                    line.append_text(primary)
                    line.append(" ")
                    if pad_needed > 0:
                        line.append("─" * pad_needed, BRACKET_STYLE)
                    line.append(close_b, BRACKET_STYLE)
                    col.append((line, col_w))
                elif rel >= 0 and rel < spaced_h and rel % row_stride != 0:
                    # Preserve a vertical bracket connector through both physical rows.
                    t = Text()
                    t.append(" │ ", BRACKET_STYLE)
                    t.append(" " * (col_w - 3))
                    col.append((t, col_w))
                else:
                    col.append((Text(" " * col_w), col_w))
            columns.append(col)
            is_virtual.append(False)

    pad = "        "  # horizontal padding on both sides
    result = []
    for row in range(render_height):
        line = Text()
        line.append(pad)
        for ci, col_data in enumerate(columns):
            if ci > 0:
                # Skip arrows adjacent to virtual start/end nodes.
                prev_virtual = is_virtual[ci - 1]
                curr_virtual = is_virtual[ci]
                if prev_virtual or curr_virtual:
                    line.append(" ")
                elif row == logical_row_starts[mid]:
                    line.append(" >> ", ARROW_STYLE)
                else:
                    line.append("    ")
            text_obj, _ = col_data[row]
            line.append_text(text_obj)
        line.append(pad)
        result.append(line)
    _anchor_rendered_nodes(result, dag.steps)


    # Draw routed dotted connectors for skip edges.
    # Each edge gets vertical drops through empty cells, then a horizontal
    # dotted line below the DAG. Agent captions remain intact.
    if dag.skip_edges:
        node_positions = {
            node.name: (
                node.render_row,
                node.render_col + 1 + len(node.display_name) // 2,
            )
            for node in _collect_nodes(dag.steps)
            if not node.virtual and node.render_row is not None and node.render_col is not None
        }

        raw_edges = []
        for src, dst in dag.skip_edges:
            src_pos = node_positions.get(src)
            dst_pos = node_positions.get(dst)
            if src_pos and dst_pos:
                raw_edges.append((src_pos[0], src_pos[1], dst_pos[0], dst_pos[1]))

        edges = _reserve_drop_lanes(result, raw_edges)

        if edges:
            _append_routed_skip_edges(result, edges, raw_edges)

    return result


# ── Navigation ─────────────────────────────────────────────────────────────


def _grid_from_steps(steps: list) -> list[list[DagNode]]:
    """Build nav grid columns from a flat list of steps."""
    grid: list[list[DagNode]] = []
    for step in steps:
        if isinstance(step, DagNode):
            if not step.virtual:
                grid.append([step])
        elif isinstance(step, ParGroup):
            branch_nodes = [
                [s for s in b.steps if isinstance(s, DagNode) and not s.virtual]
                for b in step.branches
            ]
            max_depth = max((len(bn) for bn in branch_nodes), default=0)
            for depth in range(max_depth):
                col: list[DagNode] = []
                for bi, bn in enumerate(branch_nodes):
                    if depth < len(bn):
                        bn[depth]._branch_index = bi
                        col.append(bn[depth])
                if col:
                    grid.append(col)
    return grid


def build_nav_grid(dag: SeqGroup) -> list[list[DagNode]]:
    """Build a navigation grid from the DAG layout.

    Each column in the grid corresponds to a navigable position.
    For parallel groups, the first node of each branch forms one column,
    and subsequent sequential nodes within branches form additional columns.
    For multi-track DAGs, all tracks' nodes are included in the grid.
    """
    tracks = getattr(dag, "tracks", None)
    if tracks and len(tracks) > 1:
        grid: list[list[DagNode]] = []
        for track_steps in tracks:
            grid.extend(_grid_from_steps(track_steps))
    else:
        grid = _grid_from_steps(dag.steps)
    for ci, col in enumerate(grid):
        for ri, node in enumerate(col):
            node.col = ci
            node.row = ri
    return grid


def nav_move(
    grid: list[list[DagNode]],
    current: DagNode,
    preferred_row: int,
    dx: int,
    dy: int,
) -> tuple[DagNode, int]:
    """Returns (new_node, new_preferred_row)."""
    new_col = max(0, min(current.col + dx, len(grid) - 1))
    col_nodes = grid[new_col]
    if dx != 0:
        # For horizontal moves, try to follow the same branch first
        branch = getattr(current, "_branch_index", None)
        if branch is not None:
            for n in col_nodes:
                if getattr(n, "_branch_index", None) == branch:
                    return n, n.row
        # Match by the primary visual row (e.g. evaluate → deploy_staging
        # when both render on the middle row).
        render_row = current.render_row
        if render_row is not None:
            for n in col_nodes:
                if n.render_row == render_row:
                    return n, n.row
        # Fall back to preferred row
        new_row = min(preferred_row, len(col_nodes) - 1)
        return col_nodes[new_row], preferred_row
    else:
        new_row = max(0, min(current.row + dy, len(col_nodes) - 1))
        return col_nodes[new_row], new_row
