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
    AGENT_MARKER,
    AGENT_STYLE,
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


def _render_cross_track_skip_edges(
    lines: list[Text], skip_edges: list[tuple[str, str]],
) -> None:
    """Render skip edge connectors across tracks by searching rendered text."""
    from rich.style import Style as _Style

    node_positions: dict[str, tuple[int, int]] = {}
    for ri, line in enumerate(lines):
        plain = line.plain
        for src, dst in skip_edges:
            for node_id in (src, dst):
                if node_id not in node_positions:
                    dname = display_name_from_id(node_id)
                    idx = plain.find(f" {dname} ")
                    if idx >= 0:
                        center = idx + 1 + len(dname) // 2
                        node_positions[node_id] = (ri, center)

    raw_edges = []
    for src, dst in skip_edges:
        sp = node_positions.get(src)
        dp = node_positions.get(dst)
        if sp and dp:
            raw_edges.append((sp[0], sp[1], dp[0], dp[1]))

    if not raw_edges:
        return

    cols_used: list[int] = []
    edges: list[tuple[int, int, int, int]] = []
    for sr, sc, dr, dc in raw_edges:
        while any(abs(sc - u) < 2 for u in cols_used):
            sc += 2
        while any(abs(dc - u) < 2 for u in cols_used):
            dc += 2
        cols_used.extend([sc, dc])
        edges.append((sr, sc, dr, dc))

    max_col = max(max(sc, dc) for _, sc, _, dc in edges) + 1
    edges_sorted = sorted(edges, key=lambda e: abs(e[3] - e[1]))
    edge_styles = [
        _Style(color=SKIP_EDGE_COLORS[i % len(SKIP_EDGE_COLORS)])
        for i in range(len(edges_sorted))
    ]

    def _splice(line: Text, col: int, char: str, style: _Style) -> Text:
        plain = line.plain
        if col >= len(plain) or plain[col] != " ":
            return line
        before = line[:col]
        after = line[col + 1:]
        new = Text()
        new.append_text(before)
        new.append(char, style)
        new.append_text(after)
        return new

    # Pad all existing lines to max_col so vertical ┆ can be spliced
    # through short lines (e.g. empty track separators, narrower tracks)
    for i in range(len(lines)):
        plain = lines[i].plain
        if len(plain) < max_col:
            lines[i].append(" " * (max_col - len(plain)))

    connector_start = len(lines)
    for ei, (sr, sc, dr, dc) in enumerate(edges_sorted):
        style = edge_styles[ei]
        left, right = min(sc, dc), max(sc, dc)
        t = Text()
        t.append(" " * left)
        t.append("╰", style)
        t.append("┄" * (right - left - 1), style)
        t.append("╯", style)
        pad = max_col - right - 1
        if pad > 0:
            t.append(" " * pad)
        lines.append(t)

    for ei, (sr, sc, dr, dc) in enumerate(edges_sorted):
        style = edge_styles[ei]
        horiz_row = connector_start + ei
        for ri in range(sr + 1, horiz_row):
            lines[ri] = _splice(lines[ri], sc, "┆", style)
        for ri in range(dr + 1, horiz_row):
            lines[ri] = _splice(lines[ri], dc, "┆", style)


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
    w = 4 + len(node.display_name)  # " ○ name "
    if node.is_agent:
        w += 2  # " ◈"
    dur = _duration_label(elapsed, status)
    w += len(dur) + 3  # " (dur) " — space, parens, trailing space
    return w


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
    agent_marker = f" {AGENT_MARKER}" if node.is_agent else ""
    if node is selected:
        sel = Style(bgcolor=ICE_STEEL, bold=True)
        label = (
            f" {m}{agent_marker} {node.display_name} ({dur}) "
            if dur
            else f" {m}{agent_marker} {node.display_name} "
        )
        text.append(label, Style(color=ICE_FROST) + sel)
    else:
        text.append(f" {m}", style)
        if node.is_agent:
            text.append(agent_marker, AGENT_STYLE)
        text.append(f" {node.display_name} ", Style(color=ICE_FROST))
        if dur:
            text.append(f"({dur}) ", DIM_STYLE)


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
            track_lines = render_dag_rich(
                track_dag, statuses, frame, selected, node_elapsed,
            )
            if ti > 0 and all_lines:
                all_lines.append(Text())  # blank separator between tracks
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
            _render_cross_track_skip_edges(all_lines, cross_skips)
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
    # Add spacer rows between branches only when skip edges need routing
    if has_skip_edges:
        max_height = max(1, 2 * max_branches - 1)
    else:
        max_height = max_branches
    mid = max_height // 2

    columns: list[list[tuple[Text, int]]] = []
    is_virtual: list[bool] = []  # track which columns are virtual start/end
    is_par: list[bool] = []  # track which columns are parallel groups

    for step in flat_steps:
        if isinstance(step, DagNode):
            step._render_row = mid
            elapsed = (node_elapsed or {}).get(step.name)
            status = statuses.get(step.name, NodeStatus.PENDING)
            w = plain_node_width(step, elapsed, status)
            col: list[tuple[Text, int]] = []
            for row in range(max_height):
                if row == mid:
                    t = Text()
                    append_node(t, step, status, frame, selected, elapsed)
                    col.append((t, w))
                else:
                    col.append((Text(" " * w), w))
            columns.append(col)
            is_virtual.append(step.virtual)
            is_par.append(False)

        elif isinstance(step, ParGroup):
            n = len(step.branches)
            branch_data = []
            # Spaced layout when skip edges present, compact otherwise
            if has_skip_edges:
                spaced_h = 2 * n - 1
                row_stride = 2
            else:
                spaced_h = n
                row_stride = 1
            par_offset = (max_height - spaced_h) // 2
            for bi, b in enumerate(step.branches):
                render_row = par_offset + bi * row_stride
                for s in b.steps:
                    if isinstance(s, DagNode):
                        s._render_row = render_row
                t = Text()
                append_seq(t, b, statuses, frame, selected, node_elapsed)
                branch_data.append((t, measure_seq(b, node_elapsed, statuses)))

            max_w = max(w for _, w in branch_data)
            col_w = 5 + max_w + 1 + 4  # open_b + content + space/pad + close_b

            col = []
            for row in range(max_height):
                rel = row - par_offset
                if rel >= 0 and rel < spaced_h and rel % row_stride == 0:
                    # Branch row
                    branch_idx = rel // row_stride
                    branch_text, bw = branch_data[branch_idx]
                    pad_needed = max_w - bw

                    is_mid = (row == mid)
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
                    line.append_text(branch_text)
                    line.append(" ")
                    if pad_needed > 0:
                        line.append("─" * pad_needed, BRACKET_STYLE)
                    line.append(close_b, BRACKET_STYLE)
                    col.append((line, col_w))
                elif rel >= 0 and rel < spaced_h and rel % row_stride != 0:
                    # Spacer row between branches — show vertical connector
                    t = Text()
                    t.append(" │ ", BRACKET_STYLE)
                    t.append(" " * (col_w - 3))
                    col.append((t, col_w))
                else:
                    col.append((Text(" " * col_w), col_w))
            columns.append(col)
            is_virtual.append(False)
            is_par.append(True)

    pad = "        "  # horizontal padding on both sides
    result = []
    for row in range(max_height):
        line = Text()
        line.append(pad)
        for ci, col_data in enumerate(columns):
            if ci > 0:
                # Skip arrows adjacent to virtual start/end nodes
                prev_virtual = is_virtual[ci - 1]
                curr_virtual = is_virtual[ci]
                if prev_virtual or curr_virtual:
                    line.append(" ")
                elif row == mid:
                    line.append(" >> ", ARROW_STYLE)
                else:
                    line.append("    ")
            text_obj, _ = col_data[row]
            line.append_text(text_obj)
        line.append(pad)
        result.append(line)

    # Draw routed dotted connectors for skip edges.
    # Each edge gets vertical dots dropping through DAG rows from source
    # and target, meeting at a horizontal dotted line below.
    if dag.skip_edges:
        # Find column positions of nodes — center of the display_name label
        node_positions: dict[str, tuple[int, int]] = {}  # node_id -> (row, center_col)
        for ri, line in enumerate(result):
            plain = line.plain
            for step in dag.steps:
                if isinstance(step, DagNode) and not step.virtual:
                    idx = plain.find(f" {step.display_name} ")
                    if idx >= 0:
                        center = idx + 1 + len(step.display_name) // 2
                        node_positions.setdefault(step.name, (ri, center))
                elif isinstance(step, ParGroup):
                    for branch in step.branches:
                        for s in branch.steps:
                            if isinstance(s, DagNode) and not s.virtual:
                                idx = plain.find(f" {s.display_name} ")
                                if idx >= 0:
                                    center = idx + 1 + len(s.display_name) // 2
                                    node_positions.setdefault(s.name, (ri, center))

        # Build list of resolved connectors, ensuring min 2-col gap between drops
        raw_edges = []
        for src, dst in dag.skip_edges:
            src_pos = node_positions.get(src)
            dst_pos = node_positions.get(dst)
            if src_pos and dst_pos:
                raw_edges.append((src_pos[0], src_pos[1], dst_pos[0], dst_pos[1]))

        # Offset drop columns to avoid overlap (min 2-col spacing).
        # Use a single shared list so source and destination drops
        # don't land on the same column.
        cols_used: list[int] = []
        edges: list[tuple[int, int, int, int]] = []
        for src_row, src_col, dst_row, dst_col in raw_edges:
            sc = src_col
            while any(abs(sc - u) < 2 for u in cols_used):
                sc += 2
            cols_used.append(sc)
            dc = dst_col
            while any(abs(dc - u) < 2 for u in cols_used):
                dc += 2
            cols_used.append(dc)
            edges.append((src_row, sc, dst_row, dc))

        if edges:
            max_col = max(max(sc, dc) for _, sc, _, dc in edges) + 1

            # Sort: shortest span first (rendered topmost below DAG)
            edges_sorted = sorted(edges, key=lambda e: abs(e[3] - e[1]))

            # Each edge gets a color
            edge_styles = [
                Style(color=SKIP_EDGE_COLORS[i % len(SKIP_EDGE_COLORS)])
                for i in range(len(edges_sorted))
            ]

            # Punch vertical dashed lines through DAG spacer rows.
            # Only replace spaces — don't overwrite label text or brackets.
            # Splice into the Rich Text to preserve existing styling.
            def _splice_char(line: Text, col: int, char: str, style: Style) -> Text:
                """Replace a single space in a Rich Text line with a styled char."""
                plain = line.plain
                if col >= len(plain) or plain[col] != " ":
                    return line
                # Split the Text at the column boundary and reassemble
                before = line[:col]
                after = line[col + 1:]
                new = Text()
                new.append_text(before)
                new.append(char, style)
                new.append_text(after)
                return new

            # Horizontal connector rows below the DAG (dashed lines)
            connector_start = len(result)
            for ei, (src_row, src_col, dst_row, dst_col) in enumerate(edges_sorted):
                style = edge_styles[ei]
                left = min(src_col, dst_col)
                right = max(src_col, dst_col)
                t = Text()
                t.append(" " * left)
                t.append("╰", style)
                t.append("┄" * (right - left - 1), style)
                t.append("╯", style)
                # Pad to max_col so other edges' ┆ drops have space to splice
                pad = max_col - right - 1
                if pad > 0:
                    t.append(" " * pad)
                result.append(t)

            # Second pass: punch ┆ on ALL rows between each endpoint and
            # its horizontal connector row (both through DAG and other connectors)
            for ei, (src_row, src_col, dst_row, dst_col) in enumerate(edges_sorted):
                style = edge_styles[ei]
                horiz_row = connector_start + ei
                # Source drops down from src_row to horiz_row
                for ri in range(src_row + 1, horiz_row):
                    result[ri] = _splice_char(result[ri], src_col, "┆", style)
                # Target drops down from dst_row to horiz_row
                for ri in range(dst_row + 1, horiz_row):
                    result[ri] = _splice_char(result[ri], dst_col, "┆", style)

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
        # Match by visual rendering row (e.g. evaluate → deploy_staging
        # when both are on the middle rendering row)
        render_row = getattr(current, "_render_row", None)
        if render_row is not None:
            for n in col_nodes:
                if getattr(n, "_render_row", None) == render_row:
                    return n, n.row
        # Fall back to preferred row
        new_row = min(preferred_row, len(col_nodes) - 1)
        return col_nodes[new_row], preferred_row
    else:
        new_row = max(0, min(current.row + dy, len(col_nodes) - 1))
        return col_nodes[new_row], new_row
