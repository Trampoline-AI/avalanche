"""
DAG construction for Avalanche workflows.

This module provides:
- Node: Base class for all nodes, handles >> and & operators
- NodeFuture: Represents a node invocation in a workflow (future/promised value)
- ParallelTasks: Represents parallel branches (&)
- Workflow: Executable workflow with DAG and nodes
- Decorators: @source, @step, @dest for creating nodes
- @workflow: Decorator that captures the DAG structure

## Conceptual Model / Ontology

### Core Concepts:

1. **Node**: A decorated function (@source, @step, @dest) that defines
   a unit of work. Nodes are templates - they define WHAT to do but not
   WHEN or with what data.

2. **NodeFuture**: When a Node is invoked within a @workflow context, it returns
   a NodeFuture. A NodeFuture represents a deferred computation - it's a node in the DAG
   that will be executed at some point. NodeFutures can be composed to build DAGs.

3. **Chain**: A sequence of NodeFutures connected via the >> operator, representing
   data flow from one node to the next. In the expression `a() >> b() >> c()`:
   - This creates a chain where b depends on a, and c depends on b
   - The chain_start is the first NodeFuture (a)
   - The chain_end is the last NodeFuture (c)
   - Internally, we track chain boundaries to properly connect subsequent nodes

4. **Parallel Branches**: Multiple NodeFutures combined via the & operator, representing
   nodes that can execute concurrently. In `(a() & b()) >> c()`:
   - a and b form parallel branches that both feed into c
   - c will wait for both a and b to complete

5. **Workflow**: A collection of NodeFutures organized into a DAG, created by the
   @workflow decorator. The workflow captures all the NodeFutures and their
   relationships (edges) for later execution.

### Execution Model:

When a workflow runs:
1. Nodes are executed in topological order (respecting dependencies)
2. The executor handles the actual computation (local, distributed, etc.)
3. NodeFutures resolve to actual values that flow through the DAG
4. Parallel branches execute concurrently when possible
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, CancelledError, Future, ThreadPoolExecutor, wait
from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass, field
from enum import Enum
from functools import update_wrapper, wraps
from threading import Lock
from typing import TYPE_CHECKING, Any, Callable, DefaultDict, TypeVar, get_type_hints

from ulid import ULID

from .input_ref import InputRef
from .run_handle import RunHandle
from .webhook import Webhook

if TYPE_CHECKING:
    from runtime.executor import Executor
    from runtime.operator.hooks import RunHooks

    from .execution_services import ExecutionServicesSpec

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class WorkflowContext:
    """Thread-local context for workflow construction.

    Each workflow construction gets its own isolated context via contextvars,
    ensuring thread-safety and proper isolation for concurrent/nested workflows.
    """

    graph: DefaultDict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    instance_counter: dict[str, int] = field(default_factory=dict)
    slug_counter: dict[str, int] = field(default_factory=dict)
    slug_nodes: dict[str, "Node"] = field(default_factory=dict)
    node_slugs: dict[str, str] = field(default_factory=dict)
    node_instances: dict[str, "NodeFuture"] = field(default_factory=dict)


# Thread-local context variable for workflow construction
_workflow_context: ContextVar[WorkflowContext | None] = ContextVar(
    "_workflow_context", default=None
)


def _add_graph_edge(graph, parent_id: str, child_id: str) -> None:
    children = graph[parent_id]
    if child_id not in children:
        children.append(child_id)


class NodeType(Enum):
    """Type of node in the workflow."""

    SOURCE = "source"
    STEP = "step"
    TRANSFORM = "step"
    DEST = "dest"


class Node:
    """
    Base class for all workflow nodes.

    Nodes are created by decorating functions with @source, @step, or @dest.
    When called within a @workflow function, they return NodeFuture objects that
    build the DAG via >> and & operators.

    Workflow context is managed via contextvars for thread-safety.
    Use _workflow_context.get() to access the current WorkflowContext.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        node_type: NodeType,
        num_returns: int = 1,
        *,
        slug: str | None = None,
    ):
        """
        Initialize a node.

        Args:
            fn: The function to execute
            node_type: Type of node (source, step, dest)
            num_returns: Number of return values (for tuple unpacking support)
        """
        update_wrapper(self, fn)
        self.fn = fn
        self.name = fn.__name__
        self.slug = slug or fn.__name__
        self._explicit_slug = slug is not None
        self.node_type = node_type
        self.num_returns = num_returns

    def __call__(self, *args: Any, **kwargs: Any) -> "NodeFuture":
        """
        Invoke the node within a workflow context. Used for creating NodeFuture objects
        that build the DAG.

        Returns:
            NodeFuture representing this node invocation

        Raises:
            RuntimeError: If called outside a @workflow context
        """
        ctx = _workflow_context.get()
        if ctx is None:
            raise RuntimeError(
                f"Node '{self.name}' invoked outside of workflow context. "
                "Nodes can only be called within @workflow decorated functions."
            )

        # Create unique future ID for this invocation
        if self.name not in ctx.instance_counter:
            ctx.instance_counter[self.name] = 0
        ctx.instance_counter[self.name] += 1
        future_id = f"{self.name}_{ctx.instance_counter[self.name]}"

        existing_node = ctx.slug_nodes.get(self.slug)
        if (
            existing_node is not None
            and existing_node is not self
            and (existing_node._explicit_slug or self._explicit_slug)
        ):
            raise ValueError(
                f"Duplicate node slug {self.slug!r} in workflow; "
                "use unique slug= values for rerun-addressable nodes"
            )
        ctx.slug_nodes[self.slug] = self
        ctx.slug_counter[self.slug] = ctx.slug_counter.get(self.slug, 0) + 1
        slug_count = ctx.slug_counter[self.slug]
        node_slug = self.slug if slug_count == 1 else f"{self.slug}_{slug_count}"
        if node_slug in ctx.node_slugs.values():
            raise ValueError(f"Duplicate node slug {node_slug!r} in workflow")
        ctx.node_slugs[future_id] = node_slug

        result = NodeFuture(
            node=self,
            future_id=future_id,
            node_slug=node_slug,
            graph_ref=ctx.graph,
            args=args,
            kwargs=kwargs,
        )

        # Store node future for execution
        ctx.node_instances[future_id] = result

        # Create implicit dependencies for NodeFuture arguments
        # Graph format: {parent: [children]}
        # When a NodeFuture is passed as an argument, the arg is the parent
        for arg in args:
            if isinstance(arg, NodeFuture):
                _add_graph_edge(ctx.graph, arg.future_id, future_id)

        for kwarg_val in kwargs.values():
            if isinstance(kwarg_val, NodeFuture):
                _add_graph_edge(ctx.graph, kwarg_val.future_id, future_id)

        return result

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Node({self.name}, type={self.node_type.value}, slug={self.slug!r})"


class NodeFuture:
    """
    Represents a node invocation in a workflow (a future/promised value).

    NodeFutures are created when nodes are called in a @workflow function.
    They support:
    - >> (sequential) operator for dependencies
    - & (parallel) operator for concurrent execution
    - Indexing for accessing tuple/list elements: result[0], result[1], etc.

    Attributes:
        node: The Node being invoked
        future_id: Unique ID for this future (includes [index] for unpacked values)
        graph_ref: Reference to the graph being built
        args: Positional arguments for this invocation
        kwargs: Keyword arguments for this invocation
        chain_start: First NodeFuture in this chain (for chaining)
        chain_end: Last NodeFuture/ParallelTasks in this chain (for chaining)
        parent_future_id: For indexed futures, the parent's future_id
        tuple_index: For indexed futures, the index into the parent tuple
    """

    def __init__(
        self,
        node: Node,
        future_id: str,
        graph_ref: DefaultDict[str, list[str]],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        chain_start: "NodeFuture | None" = None,
        chain_end: "NodeFuture | ParallelTasks | None" = None,
        parent_future_id: str | None = None,
        tuple_index: int | None = None,
        node_slug: str | None = None,
    ):
        """
        Initialize NodeFuture.

        Args:
            node: The Node being invoked
            future_id: Unique ID for this future
            graph_ref: Reference to the graph being built
            args: Positional arguments
            kwargs: Keyword arguments
            chain_start: First NodeFuture in this chain
            chain_end: Last NodeFuture/ParallelTasks in this chain
            parent_future_id: For indexed futures, the parent's ID
            tuple_index: For indexed futures, the index into parent tuple
        """
        self.node = node
        self.future_id = future_id
        self.node_slug = node_slug or node.slug
        self.graph_ref = graph_ref
        self.args = args
        self.kwargs = kwargs or {}

        # Track chain boundaries for proper connection when using >>
        # In a >> b >> c, 'a' is chain_start and 'c' is chain_end
        self.chain_start = chain_start or self
        self.chain_end = chain_end or self

        # For tuple indexing support
        self.parent_future_id = parent_future_id
        self.tuple_index = tuple_index

        # Track incoming NodeFutures for data passing (populated by >>)
        # This preserves tuple_index info that would otherwise be lost
        self._incoming_refs: list["NodeFuture"] = []

    def __getitem__(self, index: int) -> "NodeFuture":
        """
        Support indexing: a = node()[0], b = node()[1]

        Returns a NodeFuture that references a specific element from a multi-return node.
        The future_id stays the same (parent's ID), but tuple_index indicates which element.

        Example:
            @workflow
            def my_workflow():
                @source(num_returns=2)
                def load_pair():
                    return df_a, df_b

                pair = load_pair()  # NodeFuture(future_id="load_pair_1")
                a = pair[0]  # NodeFuture(future_id="load_pair_1", tuple_index=0)
                b = pair[1]  # NodeFuture(future_id="load_pair_1", tuple_index=1)
                process(a, b)
        """
        return NodeFuture(
            node=self.node,
            future_id=self.future_id,  # Keep parent's ID, no [index] suffix!
            node_slug=self.node_slug,
            graph_ref=self.graph_ref,
            parent_future_id=None,  # No longer needed
            tuple_index=index,
        )

    def as_dependency_ids(self) -> list[str]:
        """
        Convert chain end to dependency IDs for graph construction.

        When connecting this chain to the next node(s), these are the IDs
        that become dependencies.

        Returns:
            List of future IDs at the end of this chain
        """
        return [ref.future_id for ref in self.as_dependency_refs()]

    def as_dependency_refs(self) -> list["NodeFuture"]:
        """Return terminal dependency refs in branch order.

        Composite branches keep their chain starts in ``ParallelTasks.branches``.
        Recursing through each branch's ``chain_end`` ensures downstream data
        passing uses the actual terminal refs, including any tuple indexes.
        """
        if isinstance(self.chain_end, ParallelTasks):
            return self.chain_end.as_dependency_refs()
        return [self.chain_end]

    def __rshift__(self, next: "NodeFuture | ParallelTasks") -> "NodeFuture":
        """
        Sequential dependency: self >> next (next runs after self).

        Automatically passes return values from self as positional arguments to next
        if next has no explicit arguments.

        Args:
            next: Node or parallel group to run after this one

        Returns:
            NodeFuture representing the chained nodes

        Raises:
            TypeError: If next is not a NodeFuture or ParallelTasks
            RuntimeError: If nodes are from different workflow contexts
        """
        if not isinstance(next, (NodeFuture, ParallelTasks)):
            raise TypeError(
                f"Cannot chain NodeFuture with {type(next).__name__}. "
                f"Expected NodeFuture or ParallelTasks."
            )

        if self.graph_ref is not next.graph_ref:
            raise RuntimeError(
                "Cannot connect nodes from different workflow contexts using '>>'"
            )

        # Get dependency IDs from self (what next will depend on)
        dependency_refs = self.as_dependency_refs()
        dependency_ids = [ref.future_id for ref in dependency_refs]

        if isinstance(next, NodeFuture):  # (set parent as dependency of next)
            next_start = next.chain_start
            # Graph format: {parent: [children]}
            for d in dependency_ids:
                _add_graph_edge(self.graph_ref, d, next_start.future_id)

            # Track incoming ref (preserves tuple_index for data passing)
            next_start._incoming_refs.extend(dependency_refs)

            return NodeFuture(
                node=self.chain_start.node,
                future_id=self.chain_start.future_id,
                node_slug=self.chain_start.node_slug,
                graph_ref=self.graph_ref,
                chain_start=self.chain_start,
                chain_end=next.chain_end,
            )
        else:  # ParallelTasks (set parent as dependency of each branch in next)
            # Graph format: {parent: [children]}
            for d in dependency_ids:
                for branch in next.branches:
                    _add_graph_edge(self.graph_ref, d, branch.chain_start.future_id)

            # Track incoming ref for each branch (preserves tuple_index for data passing)
            for branch in next.branches:
                branch.chain_start._incoming_refs.extend(dependency_refs)

            return NodeFuture(
                node=self.chain_start.node,
                future_id=self.chain_start.future_id,
                node_slug=self.chain_start.node_slug,
                graph_ref=self.graph_ref,
                chain_start=self.chain_start,
                chain_end=next,
            )

    def __and__(self, other: "NodeFuture | ParallelTasks") -> "ParallelTasks":
        """
        Parallel branches: self & other (run in parallel).

        Args:
            other: Node or parallel group to run in parallel

        Returns:
            ParallelTasks representing parallel branches

        Raises:
            TypeError: If other is not a NodeFuture or ParallelTasks
            RuntimeError: If nodes are from different workflow contexts
        """
        if not isinstance(other, (NodeFuture, ParallelTasks)):
            raise TypeError(
                f"Cannot combine NodeFuture with {type(other).__name__} using '&'. "
                f"Expected NodeFuture or ParallelTasks."
            )

        if self.graph_ref is not other.graph_ref:
            raise RuntimeError(
                "Cannot connect nodes from different workflow contexts using '&'"
            )

        if isinstance(other, ParallelTasks):
            return ParallelTasks([self] + other.branches, graph_ref=self.graph_ref)
        else:
            return ParallelTasks([self, other], graph_ref=self.graph_ref)


class ParallelTasks:
    """
    Represents parallel execution branches created by the & operator.

    Attributes:
        branches: List of NodeFuture branches to execute in parallel
        graph_ref: Reference to the graph being built
        chain_start: Common chain start NodeFuture (for chaining)
    """

    def __init__(self, branches: list[NodeFuture], graph_ref: DefaultDict[str, list[str]]):
        self.branches = list(branches)
        self.graph_ref = graph_ref
        self.chain_start = branches[0].chain_start

    def as_dependency_refs(self) -> list[NodeFuture]:
        """Return every branch's terminal refs in stable branch order."""
        refs: list[NodeFuture] = []
        for branch in self.branches:
            refs.extend(branch.as_dependency_refs())
        return refs

    def __rshift__(self, next: "NodeFuture | ParallelTasks") -> NodeFuture:
        """Sequential after parallel: (a & b) >> c"""
        if not isinstance(next, (NodeFuture, ParallelTasks)):
            raise TypeError(
                f"Cannot chain ParallelTasks with {type(next).__name__}. "
                f"Expected NodeFuture or ParallelTasks."
            )

        if self.graph_ref is not next.graph_ref:
            raise RuntimeError(
                "Cannot connect nodes from different workflow contexts using '>>'"
            )

        # Connect all branch ends to next node(s)
        # Graph format: {parent: [children]}
        # Keep refs in branch order without deduplicating: indexed refs can share
        # a future_id while selecting different tuple elements.
        incoming_refs = self.as_dependency_refs()
        for incoming_ref in incoming_refs:
            if isinstance(next, NodeFuture):
                _add_graph_edge(
                    self.graph_ref,
                    incoming_ref.future_id,
                    next.chain_start.future_id,
                )
            else:  # ParallelTasks
                for next_branch in next.branches:
                    _add_graph_edge(
                        self.graph_ref,
                        incoming_ref.future_id,
                        next_branch.chain_start.future_id,
                    )

        if isinstance(next, NodeFuture):
            next.chain_start._incoming_refs.extend(incoming_refs)
            return NodeFuture(
                node=self.chain_start.node,
                future_id=self.chain_start.future_id,
                node_slug=self.chain_start.node_slug,
                graph_ref=self.graph_ref,
                chain_start=self.chain_start,
                chain_end=next.chain_end,
            )
        else:  # ParallelTasks
            for next_branch in next.branches:
                next_branch.chain_start._incoming_refs.extend(incoming_refs)

            return NodeFuture(
                node=self.chain_start.node,
                future_id=self.chain_start.future_id,
                node_slug=self.chain_start.node_slug,
                graph_ref=self.graph_ref,
                chain_start=self.chain_start,
                chain_end=next,
            )

    def __and__(self, other: "NodeFuture | ParallelTasks") -> "ParallelTasks":
        """Add more parallel branches: (a & b) & c"""
        if not isinstance(other, (NodeFuture, ParallelTasks)):
            raise TypeError(
                f"Cannot combine ParallelTasks with {type(other).__name__} using '&'. "
                f"Expected NodeFuture or ParallelTasks."
            )

        if self.graph_ref is not other.graph_ref:
            raise RuntimeError(
                "Cannot connect nodes from different workflow contexts using '&'"
            )

        if isinstance(other, ParallelTasks):
            branches = [*self.branches, *other.branches]
        else:
            branches = [*self.branches, other]
        return ParallelTasks(branches, graph_ref=self.graph_ref)


# Decorators


def source(
    fn: F | None = None,
    *,
    num_returns: int = 1,
    slug: str | None = None,
) -> Node | Callable[[F], Node]:
    """
    Decorator for source nodes.

    Sources ingest data from external systems into the data engine.

    Args:
        num_returns: Number of values this node returns (for tuple unpacking)

    Example:
        @ava.source
        def load_from_s3(*, cursor=ava.Cursor(ns().staging), staging=ns().staging):
            ...

        @ava.source(num_returns=2)
        def load_pair():
            return df_a, df_b
    """

    def decorator(f: F) -> Node:
        return Node(f, NodeType.SOURCE, num_returns=num_returns, slug=slug)

    if fn is None:
        return decorator
    return decorator(fn)


def step(
    fn: F | None = None,
    *,
    num_returns: int = 1,
    slug: str | None = None,
) -> Node | Callable[[F], Node]:
    """
    Decorator for step nodes.

    Steps process data within the engine.

    Args:
        num_returns: Number of values this node returns (for tuple unpacking)

    Example:
        @ava.step
        def chunk_docs(*, docs=ava.Stream(ns().docs), chunks=ns().chunks):
            ...
    """

    def decorator(f: F) -> Node:
        return Node(f, NodeType.STEP, num_returns=num_returns, slug=slug)

    if fn is None:
        return decorator
    return decorator(fn)


transform = step


def dest(
    fn: F | None = None,
    *,
    num_returns: int = 1,
    slug: str | None = None,
) -> Node | Callable[[F], Node]:
    """
    Decorator for destination nodes.

    Destinations export data to external systems.

    Args:
        num_returns: Number of values this node returns (for tuple unpacking)

    Example:
        @ava.dest
        def push_to_vespa(*, embeddings=ava.Stream(ns().embeddings)):
            ...
    """

    def decorator(f: F) -> Node:
        return Node(f, NodeType.DEST, num_returns=num_returns, slug=slug)

    if fn is None:
        return decorator
    return decorator(fn)


# Workflow execution helpers


def _deferred_stream_upstream_of(value: Any):
    """Find a ``DeferredStreamUpstream`` nested in a resolved provider value.

    Stream resolution yields ``(stream, upstream_data, source_node_slugs)``, so
    the carrier is nested inside a tuple. Recurses through tuple/list/dict.
    """
    from .types import DeferredStreamUpstream

    if isinstance(value, DeferredStreamUpstream):
        return value
    if isinstance(value, (tuple, list)):
        for item in value:
            found = _deferred_stream_upstream_of(item)
            if found is not None:
                return found
    if isinstance(value, dict):
        for item in value.values():
            found = _deferred_stream_upstream_of(item)
            if found is not None:
                return found
    return None


def _stamp_deferred_stream_upstream(value: Any, *, parent_kwarg: str) -> Any:
    """Stamp the collision-free hidden kwarg name and clear the parent ref.

    ``Stream.resolve`` emits a ``DeferredStreamUpstream`` with a placeholder
    ``parent_kwarg`` and the parent payload ref. The DAG submit lift generates
    the final, node+param-scoped hidden kwarg name (see ``_safe_hidden_kwarg``),
    lifts the ref into a top-level task kwarg so Ray tracks the dependency, and
    stamps that name here while clearing the ref — so the wrapper closure carries
    only the kwarg name plus tiny metadata, never an ObjectRef. Preserves
    tuple/list/dict shape.
    """
    from dataclasses import replace

    from .types import DeferredStreamUpstream

    if isinstance(value, DeferredStreamUpstream):
        return replace(value, parent_kwarg=parent_kwarg, ref=None)
    if isinstance(value, tuple):
        return tuple(
            _stamp_deferred_stream_upstream(item, parent_kwarg=parent_kwarg) for item in value
        )
    if isinstance(value, list):
        return [
            _stamp_deferred_stream_upstream(item, parent_kwarg=parent_kwarg) for item in value
        ]
    if isinstance(value, dict):
        return {
            k: _stamp_deferred_stream_upstream(v, parent_kwarg=parent_kwarg)
            for k, v in value.items()
        }
    return value


def _safe_hidden_kwarg(node_id: str, param_name: str, existing: dict[str, Any]) -> str:
    """Generate a unique, sanitized hidden kwarg name for a deferred Stream ref.

    Scoped by node id + param name so multiple Stream params on one node never
    collide, with a numeric suffix as a final guard against any residual clash
    with existing kwargs.
    """
    safe_node = "".join(ch if ch.isalnum() else "_" for ch in str(node_id))
    safe_param = "".join(ch if ch.isalnum() else "_" for ch in str(param_name))
    base = f"__ava_stream_parent_{safe_node}_{safe_param}"
    name = base
    counter = 0
    while name in existing:
        counter += 1
        name = f"{base}_{counter}"
    return name


def _resolve_to_ref(arg, result_refs, executor=None):
    """Convert a NodeFuture argument to its ref, handling tuple indexing.

    For an indexed future:
    - true multi-return (``result_refs[future_id]`` is a tuple/list of refs):
      return the selected element ref directly — no materialization;
    - single-return whose payload is a tuple/list (one ref under Ray): use
      ``executor.project`` so a worker opens the tuple instead of the driver;
    - local / already-materialized value: index in place.
    """
    if not isinstance(arg, NodeFuture):
        return arg

    ref = result_refs[arg.future_id]
    if arg.tuple_index is None:
        return ref

    # True multi-return: the ref is already a tuple/list of per-slot refs.
    if isinstance(ref, (tuple, list)):
        return ref[arg.tuple_index]

    # Single-return node whose payload is a tuple/list. Under a distributed
    # executor the ref is opaque; project it worker-side rather than fetching
    # the whole tuple to the driver just to index one element.
    if (
        executor is not None
        and _is_executor_ref(ref, executor)
        and hasattr(executor, "project")
    ):
        return executor.project(ref, arg.tuple_index)

    return ref[arg.tuple_index]


def _resolve_input_refs(
    resolved_args: list,
    resolved_kwargs: dict,
    run_input: Any,
    node_name: str,
    workflow_name: str,
) -> tuple[list, dict]:
    """Resolve top-level ava.input references against the workflow run input."""

    def resolve_value(value: Any) -> Any:
        if not isinstance(value, InputRef):
            return value
        if run_input is None:
            raise ValueError(
                f"Node '{node_name}' references {value!r} in workflow '{workflow_name}', "
                "but no run input is available. Declare @workflow(input=...) and/or "
                "pass input= to run()."
            )

        current = run_input
        for attr in value.path:
            try:
                current = getattr(current, attr)
            except AttributeError as exc:
                raise AttributeError(
                    f"Node '{node_name}' references {value!r} in workflow '{workflow_name}', "
                    f"but attribute '{attr}' is missing on {type(current).__name__}."
                ) from exc
        return current

    return (
        [resolve_value(value) for value in resolved_args],
        {key: resolve_value(value) for key, value in resolved_kwargs.items()},
    )


def _reverse_graph(parent_to_children: dict[str, list[str]]) -> dict[str, list[str]]:
    """
    Reverse a directed graph from parent -> children to child -> parents mapping.

    Useful for converting execution order (forward edges) to dependency lookup
    (reverse edges), enabling efficient "who are my parents?" queries.

    Args:
        parent_to_children: Graph as {parent_id: [child_ids]}

    Returns:
        Reversed graph as {child_id: [parent_ids]}

    Example:
        >>> graph = {"a": ["b", "c"], "b": ["d"]}
        >>> _reverse_graph(graph)
        {"b": ["a"], "c": ["a"], "d": ["b"]}
    """
    from collections import defaultdict

    reversed_graph: defaultdict[str, list[str]] = defaultdict(list)
    for parent_id, children in parent_to_children.items():
        for child_id in children:
            reversed_graph[child_id].append(parent_id)
    return dict(reversed_graph)


def _coerce_rerun(rerun: Any):
    if rerun is None:
        return None
    from .runtime import Rerun

    if isinstance(rerun, Rerun):
        return rerun
    return Rerun.model_validate(rerun)


def _filter_dependencies(
    dependencies_map: dict[str, list[str]],
    scheduled_node_ids: set[str],
) -> dict[str, list[str]]:
    return {
        node_id: [parent for parent in parents if parent in scheduled_node_ids]
        for node_id, parents in dependencies_map.items()
        if node_id in scheduled_node_ids
    }


def _node_future_refs(value: Any) -> list[NodeFuture]:
    if isinstance(value, NodeFuture):
        return [value]
    if isinstance(value, (list, tuple, set)):
        refs: list[NodeFuture] = []
        for item in value:
            refs.extend(_node_future_refs(item))
        return refs
    if isinstance(value, dict):
        refs = []
        for item in value.values():
            refs.extend(_node_future_refs(item))
        return refs
    return []


def _runtime_param_names_from_signature(fn: Callable) -> set[str]:
    """Names of params that get runtime-injected (RunContext/BaseContext/BaseInput).

    Signature/annotation based so it works without live context/input values,
    unlike _inspect_runtime_params which checks isinstance against provided
    values. Used by rerun validation to avoid misclassifying a runtime-injected
    param as a normal Python positional slot.
    """
    import inspect

    from .runtime import BaseContext, BaseInput

    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return set()
    try:
        type_hints = get_type_hints(fn)
    except Exception:
        type_hints = {}

    names: set[str] = set()
    for param_name, param in sig.parameters.items():
        annotation = type_hints.get(param_name, param.annotation)
        if annotation is inspect.Parameter.empty:
            continue
        if _safe_issubclass(annotation, BaseContext) or _safe_issubclass(annotation, BaseInput):
            names.add(param_name)
    return names


@dataclass(frozen=True)
class _ProviderSelector:
    future_id: str
    tuple_index: int | None
    explicit: bool


@dataclass(frozen=True)
class _PositionalCallAdapter:
    fixed_arg_names: tuple[str, ...]


@dataclass(frozen=True)
class _ImplicitParentBinding:
    ref: NodeFuture
    slot_index: int
    slot_kind: str
    may_expand_single_return: bool


@dataclass
class _NodeBindingPlan:
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    provider_selectors: dict[str, _ProviderSelector]
    implicit_slot_kinds: tuple[str, ...]
    implicit_parent_bindings: tuple[_ImplicitParentBinding, ...]
    positional_call_adapter: _PositionalCallAdapter | None = None

    @property
    def has_explicit_provider_selectors(self) -> bool:
        return any(selector.explicit for selector in self.provider_selectors.values())


def _matching_provider(value: Any, providers: list) -> Any | None:
    for provider in providers:
        if provider.can_resolve(value):
            return provider
    return None


def _expand_incoming_refs(incoming_refs: list[NodeFuture]) -> list[NodeFuture]:
    """Expand physical upstream refs into ordered logical output refs.

    A true multi-return node contributes one logical slot per declared return
    when its ref is unindexed. Explicitly indexed refs and single-return refs
    already identify exactly one logical output.
    """
    logical_refs: list[NodeFuture] = []
    for incoming in incoming_refs:
        if incoming.tuple_index is None and incoming.node.num_returns > 1:
            logical_refs.extend(incoming[index] for index in range(incoming.node.num_returns))
        else:
            logical_refs.append(incoming)
    return logical_refs


def _build_node_binding_plan(
    fn: Callable,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    incoming_refs: list[NodeFuture],
    *,
    providers: list,
) -> _NodeBindingPlan:
    """Bind workflow arguments without treating injected params as call slots."""
    import inspect

    try:
        params = list(inspect.signature(fn).parameters.values())
    except (ValueError, TypeError):
        return _NodeBindingPlan(args, dict(kwargs), {}, (), ())

    from .runtime import BaseInput

    try:
        type_hints = get_type_hints(fn)
    except Exception:
        type_hints = {}
    runtime_param_names = _runtime_param_names_from_signature(fn)
    selectors: dict[str, _ProviderSelector] = {}

    # A keyword NodeFuture can select only a provider declared by the function
    # default. Removing it then exposes that provider default to inspection.
    for param in params:
        value = kwargs.get(param.name)
        if not isinstance(value, NodeFuture):
            continue
        if param.default is inspect.Parameter.empty:
            continue
        provider = _matching_provider(param.default, providers)
        if provider is not None and getattr(provider, "consumes_upstream", False):
            selectors[param.name] = _ProviderSelector(
                future_id=value.future_id,
                tuple_index=value.tuple_index,
                explicit=True,
            )

    bound_kwargs = {name: value for name, value in kwargs.items() if name not in selectors}
    positional_slots: list[tuple[inspect.Parameter, str]] = []
    implicit_slots: list[tuple[inspect.Parameter, str]] = []

    for param in params:
        if param.name in runtime_param_names:
            annotation = type_hints.get(param.name, param.annotation)
            if _safe_issubclass(annotation, BaseInput) and param.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                positional_slots.append((param, "runtime_input"))
            continue

        if param.name in bound_kwargs:
            provider = _matching_provider(bound_kwargs[param.name], providers)
        elif param.default is not inspect.Parameter.empty:
            provider = _matching_provider(param.default, providers)
        else:
            provider = None

        if provider is not None:
            if getattr(provider, "consumes_upstream", False) and param.name not in selectors:
                # Providers can consume an upstream logical slot for ``>>`` even
                # when Python forbids binding that parameter positionally.
                implicit_slots.append((param, "stream"))
                if param.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                ):
                    positional_slots.append((param, "stream"))
            # Non-upstream providers never consume a workflow argument.
            continue

        if param.name in bound_kwargs:
            continue
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional_slots.append((param, "python"))
            implicit_slots.append((param, "python"))
        elif param.kind == inspect.Parameter.VAR_POSITIONAL:
            positional_slots.append((param, "varargs"))
            implicit_slots.append((param, "varargs"))

    positional_bindings: list[tuple[inspect.Parameter, Any]] = []
    arg_index = 0
    for param, slot_kind in positional_slots:
        if slot_kind == "root_input":
            if (
                arg_index < len(args)
                and isinstance(args[arg_index], InputRef)
                and not args[arg_index].path
            ):
                positional_bindings.append((param, args[arg_index]))
                arg_index += 1
            continue

        if arg_index >= len(args):
            break
        if slot_kind == "varargs":
            positional_bindings.extend((param, value) for value in args[arg_index:])
            arg_index = len(args)
            break

        value = args[arg_index]
        if slot_kind == "runtime_input":
            if isinstance(value, InputRef) and not value.path:
                arg_index += 1
                positional_bindings.append((param, value))
            continue
        arg_index += 1
        if slot_kind == "stream" and isinstance(value, NodeFuture):
            selectors[param.name] = _ProviderSelector(
                future_id=value.future_id,
                tuple_index=value.tuple_index,
                explicit=True,
            )
        else:
            positional_bindings.append((param, value))

    implicit_slot_kinds = tuple(
        "python" if kind == "varargs" else kind for _, kind in implicit_slots
    )
    logical_incoming_refs = _expand_incoming_refs(incoming_refs)
    implicit_parent_bindings = tuple(
        _ImplicitParentBinding(
            ref=incoming,
            slot_index=slot_index,
            slot_kind=slot_kind,
            may_expand_single_return=(
                incoming.tuple_index is None and incoming.node.num_returns == 1
            ),
        )
        for slot_index, (slot_kind, incoming) in enumerate(
            zip(implicit_slot_kinds, logical_incoming_refs)
        )
    )

    # ``>>`` stores its exact upstream refs separately from args/kwargs. Match
    # those refs to implicit upstream slots, which include upstream-consuming
    # keyword-only providers but keep ordinary explicit positional binding
    # restricted to parameters Python permits positionally. Explicit
    # positional/provider selectors continue to suppress implicit chain binding.
    if not args and not any(selector.explicit for selector in selectors.values()):
        for (param, _), binding in zip(implicit_slots, implicit_parent_bindings):
            if binding.slot_kind == "stream":
                selectors[param.name] = _ProviderSelector(
                    future_id=binding.ref.future_id,
                    tuple_index=binding.ref.tuple_index,
                    explicit=False,
                )

    uses_varargs = any(
        param.kind == inspect.Parameter.VAR_POSITIONAL for param, _ in positional_bindings
    )
    bound_args: list[Any] = []
    fixed_arg_names: list[str] = []
    for param, value in positional_bindings:
        if param.kind == inspect.Parameter.POSITIONAL_ONLY or uses_varargs:
            bound_args.append(value)
            if param.kind != inspect.Parameter.VAR_POSITIONAL:
                fixed_arg_names.append(param.name)
        elif param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
            bound_kwargs[param.name] = value
        else:
            bound_args.append(value)
    bound_args.extend(args[arg_index:])

    has_varargs = any(param.kind == inspect.Parameter.VAR_POSITIONAL for param in params)
    has_positional_only = any(
        param.kind == inspect.Parameter.POSITIONAL_ONLY for param in params
    )

    return _NodeBindingPlan(
        args=tuple(bound_args),
        kwargs=bound_kwargs,
        provider_selectors=selectors,
        implicit_slot_kinds=implicit_slot_kinds,
        implicit_parent_bindings=implicit_parent_bindings,
        positional_call_adapter=(
            _PositionalCallAdapter(tuple(fixed_arg_names))
            if has_varargs or has_positional_only
            else None
        ),
    )


def _adapt_positional_call(
    fn: Callable[..., Any],
    signature_fn: Callable[..., Any],
    adapter: _PositionalCallAdapter,
) -> Callable[..., Any]:
    """Rebuild positional calls after framework values are injected.

    The planner removes provider selectors and skips runtime/provider slots, so
    its top-level executor args are a compressed logical sequence. Provider
    wrappers then inject those skipped values by keyword. Before invoking user
    code, expand positional parameters in declaration order and append any
    user's ``*args`` tail. Only slot metadata is captured; executor payloads
    remain visible as top-level task args/kwargs.
    """
    import inspect

    prefix_params: list[inspect.Parameter] = []
    for param in inspect.signature(signature_fn).parameters.values():
        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            break
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            prefix_params.append(param)

    @wraps(fn)
    def adapted(*args: Any, **kwargs: Any) -> Any:
        fixed_count = len(adapter.fixed_arg_names)
        fixed_values = dict(zip(adapter.fixed_arg_names, args[:fixed_count]))
        final_args: list[Any] = []

        for param in prefix_params:
            if param.name in fixed_values:
                final_args.append(fixed_values[param.name])
            elif param.name in kwargs:
                final_args.append(kwargs.pop(param.name))
            elif param.default is not inspect.Parameter.empty:
                final_args.append(param.default)
            else:
                raise TypeError(
                    f"Cannot reconstruct positional call for {signature_fn.__name__}: "
                    f"parameter {param.name!r} was not supplied or injected"
                )

        final_args.extend(args[fixed_count:])
        return fn(*final_args, **kwargs)

    return adapted


def _incoming_refs_for_node(
    nodes: dict[str, NodeFuture],
    node_ref: NodeFuture,
    original_dependencies_map: dict[str, list[str]],
    node_id: str,
) -> list[NodeFuture]:
    """Return ordered incoming refs, including tuple selectors when available."""
    if node_ref._incoming_refs:
        return node_ref._incoming_refs
    return [nodes[parent_id] for parent_id in original_dependencies_map.get(node_id, [])]


def _validate_no_skipped_non_stream_inputs(
    nodes: dict[str, NodeFuture],
    scheduled_node_ids: set[str],
    original_dependencies_map: dict[str, list[str]],
) -> None:
    """Reject rerun schedules where a skipped upstream feeds a non-stream input.

    v1 reruns require skipped upstream data to be consumed through ava.Stream.
    This runs before any node is submitted so the failure is a clear validation
    error rather than a downstream Python TypeError.
    """
    from .runtime.providers import PROVIDERS

    for node_id in scheduled_node_ids:
        node_ref = nodes[node_id]
        binding_plan = _build_node_binding_plan(
            node_ref.node.fn,
            node_ref.args,
            node_ref.kwargs,
            _incoming_refs_for_node(
                nodes,
                node_ref,
                original_dependencies_map,
                node_id,
            ),
            providers=PROVIDERS,
        )

        for selector in binding_plan.provider_selectors.values():
            producer = nodes[selector.future_id]
            if (
                selector.tuple_index is not None or producer.node.num_returns > 1
            ) and selector.future_id not in scheduled_node_ids:
                raise ValueError(
                    f"Rerun node {node_id!r} has an indexed or multi-return "
                    "ava.Stream selector "
                    f"for skipped upstream node {selector.future_id!r}; indexed "
                    "Stream selectors cannot replay skipped upstreams because "
                    "durable lineage is keyed only by (run_id, node_slug). Include "
                    "the producer in the rerun, or model its outputs as distinct "
                    "source nodes."
                )

        # 1. Explicit NodeFuture args/kwargs referencing skipped upstreams.
        for arg in binding_plan.args:
            for ref in _node_future_refs(arg):
                if ref.future_id not in scheduled_node_ids:
                    raise ValueError(
                        f"Rerun node {node_id!r} has an explicit dependency on skipped "
                        f"upstream node {ref.future_id!r}; skipped upstream data "
                        "must be consumed through ava.Stream"
                    )

        for kwarg in binding_plan.kwargs.values():
            for ref in _node_future_refs(kwarg):
                if ref.future_id not in scheduled_node_ids:
                    raise ValueError(
                        f"Rerun node {node_id!r} has an explicit dependency on skipped "
                        f"upstream node {ref.future_id!r}; skipped upstream data "
                        "must be consumed through ava.Stream"
                    )

        # 2. Implicit chain refs assigned by logical upstream slot order.
        if node_ref.args or binding_plan.has_explicit_provider_selectors:
            # Explicit args suppress implicit positional binding.
            continue

        if not binding_plan.implicit_slot_kinds:
            continue

        for binding in binding_plan.implicit_parent_bindings:
            parent_id = binding.ref.future_id
            if binding.slot_kind == "stream":
                if (
                    parent_id not in scheduled_node_ids
                    and binding.may_expand_single_return
                    and "python" in binding_plan.implicit_slot_kinds[binding.slot_index + 1 :]
                ):
                    raise ValueError(
                        f"Rerun node {node_id!r} has an ambiguous single-return "
                        f"container from skipped upstream node {parent_id!r}; its "
                        "ava.Stream slot is replayable, but tuple/list values could "
                        "also populate a non-stream parameter. Include the producer "
                        "in the rerun or declare separate returns."
                    )
                continue
            if parent_id not in scheduled_node_ids:
                raise ValueError(
                    f"Rerun node {node_id!r} has an implicit dependency on skipped "
                    f"upstream node {parent_id!r} bound to a non-stream parameter; "
                    "v1 reruns require skipped upstream data to be consumed through "
                    "ava.Stream"
                )


def _is_executor_ref(value: Any, executor: Any) -> bool:
    """Return True when value is a concrete future/ref for this executor."""
    ray = getattr(executor, "ray", None)
    object_ref_type = getattr(ray, "ObjectRef", None)
    return object_ref_type is not None and isinstance(value, object_ref_type)


def _should_fetch_with_executor(value: Any, executor: Any) -> bool:
    """Ray can only fetch ObjectRefs; LocalExecutor can fetch materialized values."""
    if getattr(executor, "ray", None) is not None:
        return _is_executor_ref(value, executor)
    return True


def _all_fetchable_with_executor(values: list[Any], executor: Any) -> bool:
    """Return True when all values can be passed to executor.get()."""
    if getattr(executor, "ray", None) is not None:
        return all(_is_executor_ref(value, executor) for value in values)
    return True


def _inspect_providers(fn: Callable, kwargs: dict[str, Any], providers: list) -> dict[str, Any]:
    """
    Inspect function signature and kwargs for injectable parameter types.

    Checks both explicitly passed kwargs and default parameter values
    for any parameter that matches a registered provider (dependency injection).

    Args:
        fn: Function to inspect
        kwargs: Keyword arguments passed to the function
        providers: List of provider classes to check against

    Returns:
        Dict mapping parameter name to injectable parameter instance
        eg.
        ```python
        def my_fn(docs: Stream, chunks: Stream):
            ...
        maps to:
        ```python
        {
            "docs": Stream(table),  # run-scoped
            "chunks": Stream(table, key="chunks_to_embeddings", mode="append_scan"),
            ...
        }
        ```
    """
    import inspect

    injectable_params = {}

    # Check explicitly passed kwargs for injectable params
    # Use case: Override default providers or pass them explicitly when no default
    # Examples:
    #   Given: @step
    #          def process(
    #              data: pl.DataFrame = Stream(table_a),
    #              logger=Logger()
    #          ):
    #              ...
    #
    #   - Override: process(data=Stream(table_b))  # Explicit wins over default
    #   - Explicit: process(data=Stream(table))    # When no default exists
    #   - Custom:   process(logger=Logger(level=DEBUG))          # Custom config override
    for param_name, param_value in kwargs.items():
        for provider in providers:
            if provider.can_resolve(param_value):
                injectable_params[param_name] = param_value
                break

    # Check default parameter values in function signature
    # Use case: Most common - provider markers as defaults (like FastAPI's Depends)
    # Example:
    #   Given: @step
    #          def process(
    #              data: pl.DataFrame = Stream(table),
    #              *,
    #              logger=Logger()
    #          ):
    #              logger.info(f"Processing {len(data)} rows")
    #              return step(data)
    #
    #   Then call: process()  # No args - both Stream and Logger auto-injected!
    try:
        sig = inspect.signature(fn)
        for param_name, param in sig.parameters.items():
            if param.default != inspect.Parameter.empty:
                for provider in providers:
                    if provider.can_resolve(param.default):
                        # Only add if not already explicitly passed (explicit wins)
                        if param_name not in kwargs:
                            injectable_params[param_name] = param.default
                        break
    except (ValueError, TypeError):
        # Can't inspect signature (C function, etc.)
        pass

    return injectable_params


def _safe_issubclass(value: Any, class_or_tuple: Any) -> bool:
    try:
        return isinstance(value, type) and issubclass(value, class_or_tuple)
    except TypeError:
        return False


def _build_input_value(input_type: type | None, raw_input: Any) -> Any:
    from .runtime import BaseInput

    if raw_input is None:
        if input_type is None:
            return None
        return input_type()
    if input_type is None:
        return raw_input
    if isinstance(raw_input, input_type):
        return raw_input
    if not _safe_issubclass(input_type, BaseInput):
        raise TypeError("workflow input type must inherit from ava.BaseInput")
    return input_type.model_validate(raw_input)


def _validate_workflow_types(input_type: type | None, context_type: type | None) -> None:
    from .runtime import BaseContext, BaseInput

    if input_type is not None and not _safe_issubclass(input_type, BaseInput):
        raise TypeError("workflow input type must inherit from ava.BaseInput")
    if context_type is not None and not _safe_issubclass(context_type, BaseContext):
        raise TypeError("workflow context type must inherit from ava.BaseContext")


def _build_context_values(
    context_type: type | None,
    raw_context: Any,
    *,
    run_id: str,
    workflow_name: str,
    executor_type: str,
    rerun: Any = None,
) -> tuple[Any, Any]:
    from .runtime import BaseContext, RunContext

    system_context = RunContext(
        run_id=run_id,
        workflow_name=workflow_name,
        executor_type=executor_type,
        rerun=rerun,
    )
    target_type = context_type or RunContext
    if not _safe_issubclass(target_type, BaseContext):
        raise TypeError("workflow context type must inherit from ava.BaseContext")

    runtime_fields = {
        "run_id": run_id,
        "workflow_name": workflow_name,
        "executor_type": executor_type,
        "rerun": rerun,
        "node_id": None,
        "node_name": None,
        "node_slug": None,
        "lineage_vector": {},
    }

    if raw_context is not None and isinstance(raw_context, target_type):
        if _safe_issubclass(target_type, RunContext):
            context_value = raw_context.model_copy(update=runtime_fields)
        else:
            context_value = raw_context
    elif _safe_issubclass(target_type, RunContext):
        data = raw_context or {}
        if not isinstance(data, dict):
            raise TypeError("workflow context must be a mapping or BaseContext instance")
        context_value = target_type.model_validate({**data, **runtime_fields})
    else:
        context_value = target_type.model_validate(raw_context or {})

    return system_context, context_value


def _context_for_node(
    context: Any,
    *,
    node_id: str,
    node_name: str,
    node_slug: str,
) -> Any:
    from .runtime import RunContext

    if isinstance(context, RunContext):
        return context.for_node(node_id=node_id, node_name=node_name, node_slug=node_slug)
    return context


def _unwrap_lineaged_tree(value: Any) -> Any:
    """Strip LineagedResult envelopes recursively through tuple/list/dict.

    Only unwraps envelopes; other containers/values pass through unchanged so
    user args keep their structure.
    """
    from .types import LineagedResult

    if isinstance(value, LineagedResult):
        return _unwrap_lineaged_tree(value.value)
    if isinstance(value, tuple):
        return tuple(_unwrap_lineaged_tree(item) for item in value)
    if isinstance(value, list):
        return [_unwrap_lineaged_tree(item) for item in value]
    if isinstance(value, dict):
        return {k: _unwrap_lineaged_tree(v) for k, v in value.items()}
    return value


def _lineage_from_tree(value: Any) -> dict[str, str]:
    """Collect and merge lineage vectors from LineagedResult envelopes.

    Walks tuple/list/dict containers. Later producers win on key conflicts,
    matching the row-write merge semantics.
    """
    from .types import LineagedResult

    merged: dict[str, str] = {}
    if isinstance(value, LineagedResult):
        merged.update(value.lineage_vector)
        merged.update(_lineage_from_tree(value.value))
    elif isinstance(value, (tuple, list)):
        for item in value:
            merged.update(_lineage_from_tree(item))
    elif isinstance(value, dict):
        for item in value.values():
            merged.update(_lineage_from_tree(item))
    return merged


def _reattach_lineage(value: Any, lineage_source: Any) -> Any:
    """Reattach lineage from lineage_source onto a hook-replaced value.

    Hooks that replace a node's user-facing value must not strip the internal
    lineage needed by downstream Python-arg consumers. Preserves tuple/list
    shape for multi-return nodes; otherwise wraps in a single LineagedResult
    when there is any lineage to carry.
    """
    from .types import LineagedResult

    if isinstance(value, LineagedResult):
        return value

    if isinstance(lineage_source, LineagedResult):
        return LineagedResult(value, dict(lineage_source.lineage_vector))

    if (
        isinstance(value, tuple)
        and isinstance(lineage_source, tuple)
        and len(value) == len(lineage_source)
    ):
        return tuple(
            _reattach_lineage(item, source) for item, source in zip(value, lineage_source)
        )

    if (
        isinstance(value, list)
        and isinstance(lineage_source, list)
        and len(value) == len(lineage_source)
    ):
        return [_reattach_lineage(item, source) for item, source in zip(value, lineage_source)]

    lineage = _lineage_from_tree(lineage_source)
    return LineagedResult(value, lineage) if lineage else value


def _wrap_lineaged_result(value: Any, context: Any, *, num_returns: int) -> Any:
    """Wrap a node return value with the lineage vector of its producers.

    Multi-return nodes wrap each item individually so executor multi-return
    (e.g. Ray) still sees the expected number of results.
    """
    from .types import LineagedResult

    lineage = dict(context.lineage_vector)
    if context.node_slug is not None:
        lineage[context.node_slug] = context.run_id

    if num_returns > 1 and isinstance(value, tuple):
        return tuple(LineagedResult(item, lineage) for item in value)
    if num_returns > 1 and isinstance(value, list):
        return [LineagedResult(item, lineage) for item in value]
    return LineagedResult(value, lineage)


_STREAM_PARENT_KWARG_PREFIX = "__ava_stream_parent_"


def _materialize_append_handles_for_worker(value: Any) -> Any:
    """Convert ``AppendResultHandle``s to ``AppendResult`` inside a Ray worker.

    Runs before user code / provider wrappers so no user function or Stream
    consumer ever receives the internal handle. Dereferences data refs via
    ``ray.get`` (worker-side, not the driver). No-op when Ray is unavailable
    (LocalExecutor) or there are no handles.
    """
    from .types import materialize_append_handles

    try:
        import ray
    except ImportError:
        return value
    return materialize_append_handles(value, ray.get)


def _materialize_worker_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Materialize worker kwargs, EXCEPT hidden Stream parent kwargs.

    A deferred Stream upstream is lifted into a top-level hidden kwarg named
    ``__ava_stream_parent_*`` so Ray tracks the producer as a dependency. That
    value must NOT be generic-materialized here: ``stream_wrapper`` owns its
    control/data split and pops the hidden kwarg itself, converting the small
    ``AppendResultHandle`` into an ``AppendResult`` only when it decides to.
    Fetching the handle's ``data_ref`` here would defeat the split (early frame
    fetch) and bypass Stream's own worker-side resolver.
    """
    return {
        key: value
        if isinstance(key, str) and key.startswith(_STREAM_PARENT_KWARG_PREFIX)
        else _materialize_append_handles_for_worker(value)
        for key, value in kwargs.items()
    }


def _materialize_append_handles_for_driver(value: Any, executor: Any) -> Any:
    """Convert ``AppendResultHandle``s to ``AppendResult`` on the driver.

    Used only where driver-side payload materialization is already intentional:
    explicit workflow returns and ``unwrap_result`` hooks. Fetches each data ref
    via ``executor.get`` (Ray) or returns it as-is (already materialized). Never
    called on progress-only / no-return paths.
    """
    from .types import materialize_append_handles

    def get_data(ref: Any) -> Any:
        if _should_fetch_with_executor(ref, executor):
            return executor.get([ref])[0]
        return ref

    return materialize_append_handles(value, get_data)


def _with_current_run_context(
    fn: Callable[..., Any], context: Any, *, num_returns: int = 1
) -> Callable[..., Any]:
    """Wrap a node function so framework helpers can read its RunContext.

    Also carries producer lineage across the executor boundary: parent
    LineagedResult envelopes in the args/kwargs are merged into this node's
    lineage vector (so downstream rows record actual producer versions even for
    ordinary Python-argument dependencies), then stripped before the user
    function runs, and the node's own result is re-wrapped with the resulting
    vector.

    Under Ray, upstream ``AppendResultHandle``s in args/kwargs are materialized
    back into ``AppendResult`` here (worker-side) so user code and Stream
    wrappers never see the internal transport type.
    """
    from .runtime import RunContext
    from .runtime.context import _run_with_context

    if not isinstance(context, RunContext):
        # Still unwrap parent envelopes so non-context nodes never receive an
        # internal transport type.
        @wraps(fn)
        def plain(*args: Any, **kwargs: Any) -> Any:
            args = _materialize_append_handles_for_worker(args)
            kwargs = _materialize_worker_kwargs(kwargs)
            return fn(*_unwrap_lineaged_tree(args), **_unwrap_lineaged_tree(kwargs))

        return plain

    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        # Materialize off-driver AppendResult handles worker-side first so
        # lineage collection and user code operate on public AppendResults.
        # Hidden Stream parent kwargs are skipped — stream_wrapper owns their
        # control/data split (see _materialize_worker_kwargs).
        args = _materialize_append_handles_for_worker(args)
        kwargs = _materialize_worker_kwargs(kwargs)

        parent_lineage = _lineage_from_tree(args)
        parent_lineage.update(_lineage_from_tree(kwargs))
        if parent_lineage:
            merged = dict(context.lineage_vector)
            merged.update(parent_lineage)
            context.lineage_vector = merged

        unwrapped_args = _unwrap_lineaged_tree(args)
        unwrapped_kwargs = _unwrap_lineaged_tree(kwargs)
        result = _run_with_context(context, fn, *unwrapped_args, **unwrapped_kwargs)
        return _wrap_lineaged_result(result, context, num_returns=num_returns)

    return wrapped


def _inspect_runtime_params(
    fn: Callable,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    run_input: Any,
    run_context: Any,
    system_context: Any,
    *,
    defer_input: bool = False,
) -> dict[str, Any]:
    import inspect

    from .runtime import BaseContext, BaseInput

    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return {}
    try:
        type_hints = get_type_hints(fn)
    except Exception:
        type_hints = {}

    position_bound_param_names: set[str] = set()
    positional_count = len(args)
    consumed_positionals = 0
    for param in sig.parameters.values():
        if consumed_positionals >= positional_count:
            break
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            position_bound_param_names.add(param.name)
            consumed_positionals += 1
        elif param.kind is inspect.Parameter.VAR_POSITIONAL:
            position_bound_param_names.add(param.name)
            break

    injected: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        if param_name in kwargs or param_name in position_bound_param_names:
            continue
        annotation = type_hints.get(param_name, param.annotation)
        if annotation is inspect.Parameter.empty:
            continue

        if _safe_issubclass(annotation, BaseContext):
            if run_context is not None and isinstance(run_context, annotation):
                injected[param_name] = run_context
            elif system_context is not None and isinstance(system_context, annotation):
                injected[param_name] = system_context
        elif _safe_issubclass(annotation, BaseInput):
            if defer_input:
                injected[param_name] = _DEFERRED_EXECUTION_SERVICE_INPUT
            elif run_input is not None and isinstance(run_input, annotation):
                injected[param_name] = run_input

    return injected


_DEFERRED_EXECUTION_SERVICE_INPUT = object()


def _data_param_position(
    fn: Callable,
    param_name: str,
    skip_param_names: set[str],
    position_consuming_param_names: set[str],
) -> int:
    import inspect

    try:
        params = list(inspect.signature(fn).parameters.values())
    except (ValueError, TypeError):
        return -1

    position = 0
    for param in params:
        if param.name == param_name:
            return position
        if param.name in skip_param_names:
            continue
        if param.name in position_consuming_param_names:
            position += 1
            continue
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            position += 1
    return -1


def _fetch_node_result(
    nf: NodeFuture,
    result_refs,
    nodes,
    executor,
    scheduled_node_ids: set[str] | None = None,
):
    """Fetch the actual result value for a NodeFuture, handling chains and multi-return."""
    # If this is a chain composite, fetch the chain_end instead
    fetch_target = nf.chain_end if nf.chain_end != nf else nf

    # Handle ParallelTasks: return tuple of all branch results
    if isinstance(fetch_target, ParallelTasks):
        results = []
        for branch in fetch_target.branches:
            branch_result = _fetch_node_result(
                branch,
                result_refs,
                nodes,
                executor,
                scheduled_node_ids,
            )
            results.append(branch_result)
        return tuple(results)

    # Regular NodeFuture handling
    if fetch_target.future_id not in result_refs:
        if scheduled_node_ids is not None and fetch_target.future_id not in scheduled_node_ids:
            return None
        raise ValueError(
            f"Workflow return node {fetch_target.future_id!r} was not scheduled by rerun"
        )
    ref = result_refs[fetch_target.future_id]
    node = nodes[fetch_target.future_id]

    # Handle tuple indexing if this is an indexed ref
    if fetch_target.tuple_index is not None:
        # Indexed into a parent result. Select only the requested element
        # (true multi-return returns the chosen ref; single-return tuple is
        # projected worker-side) so we never materialize sibling elements just
        # to return one. Then fetch only the selected value for the explicit
        # workflow return.
        selected = _indexed_parent_result(ref, fetch_target.tuple_index, executor)
        if _should_fetch_with_executor(selected, executor):
            selected = executor.get([selected])[0]
        selected = _materialize_append_handles_for_driver(selected, executor)
        return _unwrap_lineaged_tree(selected)
    elif node.node.num_returns > 1:
        # Multi-return node: ref is tuple/list of refs
        refs_to_fetch = list(ref)
        if _all_fetchable_with_executor(refs_to_fetch, executor):
            fetched = executor.get(refs_to_fetch)
            value = _materialize_append_handles_for_driver(tuple(fetched), executor)
            return _unwrap_lineaged_tree(value)
        value = _materialize_append_handles_for_driver(tuple(refs_to_fetch), executor)
        return _unwrap_lineaged_tree(value)
    else:
        # Single return node: ref is a single ref
        if _should_fetch_with_executor(ref, executor):
            value = executor.get([ref])[0]
        else:
            value = ref
        value = _materialize_append_handles_for_driver(value, executor)
        return _unwrap_lineaged_tree(value)


def _implicit_value_from_upstream(item: Any) -> Any:
    """Prepare an upstream value for implicit binding, preserving lineage.

    - Bare AppendResult -> its .data (legacy zero-copy behavior).
    - LineagedResult(AppendResult) -> LineagedResult(.data) so the outer context
      wrapper can still merge the producer lineage before unwrapping for the
      user function.
    - Other LineagedResult / values -> passed through unchanged (the wrapper
      merges + unwraps them).
    """
    from .types import AppendResult, LineagedResult

    if isinstance(item, LineagedResult):
        inner = item.value
        if isinstance(inner, AppendResult):
            return LineagedResult(inner.data, item.lineage_vector)
        return item
    if isinstance(item, AppendResult):
        return item.data
    return item


def _implicit_items_from_parent_result(presult: Any) -> list[Any]:
    """Flatten a parent result into implicit-binding items, preserving lineage.

    A single-return node that returns a tuple/list is flattened into multiple
    implicit args (legacy behavior). When wrapped in a LineagedResult envelope,
    each flattened item keeps the parent's lineage so downstream rows still
    record the producer.
    """
    from .types import LineagedResult

    if isinstance(presult, LineagedResult) and isinstance(presult.value, (tuple, list)):
        return [LineagedResult(item, dict(presult.lineage_vector)) for item in presult.value]
    if isinstance(presult, (tuple, list)):
        return list(presult)
    return [presult]


def _indexed_parent_result(presult: Any, tuple_index: int, executor: Any = None) -> Any:
    """Index into a parent result without materializing payloads on the driver.

    Preserves any ``LineagedResult`` envelope on the selected element so
    downstream Python-arg consumers still record the producer. Cases:

    - already-materialized ``LineagedResult`` (local): index its value in place;
    - true Ray multi-return (``presult`` is a tuple/list of per-slot refs):
      return the selected element ref directly — no ``get``;
    - single-return tuple/list hidden behind one Ray ObjectRef: project it in a
      worker via ``executor.project`` rather than fetching the whole tuple to
      the driver;
    - local tuple/list: index in place.
    """
    from .types import LineagedResult

    if isinstance(presult, LineagedResult):
        return LineagedResult(presult.value[tuple_index], dict(presult.lineage_vector))

    # True multi-return: presult is already a tuple/list of per-slot refs.
    if isinstance(presult, (tuple, list)):
        return presult[tuple_index]

    # Single-return tuple/list behind one distributed ref: project worker-side.
    if (
        executor is not None
        and _is_executor_ref(presult, executor)
        and hasattr(executor, "project")
    ):
        return executor.project(presult, tuple_index)

    return presult[tuple_index]


def _collect_implicit_parent_results(
    node_ref: NodeFuture,
    result_refs: dict[str, Any],
    dependencies_map: dict[str, list[str]],
    node_id: str,
    *,
    executor: Any = None,
) -> list[Any]:
    """Collect flattened parent results for implicit data passing.

    Parents whose NodeFuture was passed explicitly (as a positional or keyword
    argument) are excluded: their results already flow through normal argument
    resolution, so re-binding them implicitly would double-pass them.
    """
    explicit_ids = {
        value.future_id
        for value in (*node_ref.args, *node_ref.kwargs.values())
        if isinstance(value, NodeFuture)
    }
    auto_values: list[Any] = []

    # Use _incoming_refs if available (preserves tuple_index info).
    # Otherwise fall back to dependencies_map.
    if node_ref._incoming_refs:
        for incoming in node_ref._incoming_refs:
            if incoming.future_id in explicit_ids:
                continue
            presult = result_refs.get(incoming.future_id)
            if presult is not None:
                if incoming.tuple_index is not None:
                    auto_values.append(
                        _indexed_parent_result(presult, incoming.tuple_index, executor)
                    )
                else:
                    auto_values.extend(_implicit_items_from_parent_result(presult))
    else:
        parent_ids = dependencies_map.get(node_id, [])
        for pid in parent_ids:
            if pid in explicit_ids:
                continue
            presult = result_refs.get(pid)
            if presult is not None:
                auto_values.extend(_implicit_items_from_parent_result(presult))

    return auto_values


def _bind_implicit_parent_results(
    fn: Callable[..., Any],
    upstream_values: list[Any],
    skip_param_names: set[str],
    position_consuming_param_names: set[str],
    resolved_kwargs: dict[str, Any],
    *,
    adapt_positionals: bool = False,
) -> tuple[list[Any], dict[str, Any]]:
    """
    Bind implicit upstream values by signature position.

    Normal POSITIONAL_OR_KEYWORD parameters are passed by keyword so provider
    wrappers can also inject named values without double-binding. Positional-only
    parameters and real *args cases still require positional invocation.
    """
    import inspect

    if not upstream_values:
        return [], {}

    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    accepts_implicit_positionals = any(
        param.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        )
        for param in params
    )
    if not accepts_implicit_positionals:
        return [], {}

    resolved_args: list[Any] = []
    implicit_kwargs: dict[str, Any] = {}
    upstream_index = 0

    for param in params:
        if param.name in skip_param_names:
            continue
        if param.name in position_consuming_param_names:
            if upstream_index < len(upstream_values):
                upstream_index += 1
            continue
        if upstream_index >= len(upstream_values):
            break

        item = upstream_values[upstream_index]
        value = _implicit_value_from_upstream(item)

        if param.kind == inspect.Parameter.POSITIONAL_ONLY:
            if adapt_positionals:
                # The positional adapter accepts positional-only values as named
                # slot carriers, then reconstructs the final call after
                # provider/runtime injection.
                implicit_kwargs[param.name] = value
            else:
                resolved_args.append(value)
            upstream_index += 1
            continue

        if param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
            if param.name in resolved_kwargs:
                raise TypeError(
                    f"Cannot implicitly bind upstream result at position {upstream_index} for "
                    f"{fn.__name__}: parameter {param.name!r} was also passed explicitly"
                )
            implicit_kwargs[param.name] = value
            upstream_index += 1
            continue

        if param.kind == inspect.Parameter.VAR_POSITIONAL:
            resolved_args.extend(
                _implicit_value_from_upstream(item) for item in upstream_values[upstream_index:]
            )
            upstream_index = len(upstream_values)
            continue

        raise TypeError(
            f"Cannot implicitly bind upstream result at position {upstream_index} for "
            f"{fn.__name__}: parameter {param.name!r} is {param.kind.description}"
        )

    if (
        upstream_index < len(upstream_values)
        and not skip_param_names
        and not position_consuming_param_names
    ):
        raise TypeError(
            f"Cannot implicitly bind upstream result at position {upstream_index} for "
            f"{fn.__name__}: function signature has no parameter at that position"
        )

    return resolved_args, implicit_kwargs


class Workflow:
    """
    Executable workflow with DAG and node instances.

    Created by @workflow decorator. Provides run() method for execution.
    """

    def __init__(
        self,
        graph: DefaultDict[str, list[str]],
        nodes: dict[str, NodeFuture],
        node_slugs: dict[str, str] | None = None,
        name: str = "workflow",
        returns: Any = None,
        cron: str | None = None,
        webhook: Webhook | bool | None = None,
        input_type: type | None = None,
        context_type: type | None = None,
        agent_defaults: dict[str, Any] | None = None,
    ):
        """
        Initialize workflow.

        Args:
            graph: DAG as adjacency list (future_id -> [dependency_ids])
            nodes: Node futures by future_id
            name: Workflow name
            returns: Value(s) returned by workflow function (NodeFuture, tuple, or None)
            cron: Optional cron expression for scheduled execution
        """
        self.graph = graph
        self.nodes = nodes
        self.node_slugs = node_slugs or {
            node_id: node_future.node_slug for node_id, node_future in nodes.items()
        }
        self.name = name
        self.returns = returns
        self.cron = cron
        self.webhook = _normalize_webhook(webhook)
        _validate_workflow_types(input_type, context_type)
        self.input_type = input_type
        self.context_type = context_type
        self.agent_defaults = dict(agent_defaults or {})

        # Validate: detect cycles via Kahn's algorithm (incomplete sort = cycle)
        order = self._topological_sort()
        if len(order) != len(self.nodes):
            in_cycle = set(self.nodes) - set(order)
            raise ValueError(
                f"Workflow '{name}' contains a cycle involving: {', '.join(sorted(in_cycle))}"
            )

    def _topological_sort(self) -> list[str]:
        """
        Topological sort of the DAG (graph is a dict of parent -> [children]).

        Returns:
            List of future IDs in execution order

        Note:
            Graph format: {parent_id: [child_ids]} where edges go from parent to children.
        """
        # Compute indegree from parent->children graph (Kahn's algorithm)
        indegree: dict[str, int] = {parent_id: 0 for parent_id in self.nodes.keys()}
        for parent_id, children in self.graph.items():
            indegree.setdefault(parent_id, 0)
            for child_id in children:
                indegree[child_id] = indegree.get(child_id, 0) + 1

        # Start with nodes that have no incoming edges (ie deg == 0)
        queue = [node_id for node_id, deg in indegree.items() if deg == 0]
        order: list[str] = []

        while queue:
            node_id = queue.pop(0)
            order.append(node_id)
            for child in self.graph.get(node_id, []):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)

        return order

    def _plan_rerun_execution(self, rerun: Any, execution_order: list[str]) -> list[str]:
        slug_to_node_id = {slug: node_id for node_id, slug in self.node_slugs.items()}
        missing = [slug for slug in rerun.start if slug not in slug_to_node_id]
        if missing:
            known = ", ".join(sorted(slug_to_node_id))
            raise ValueError(
                f"Unknown rerun start slug(s): {', '.join(missing)}. " f"Known slugs: {known}"
            )

        start_node_ids = {slug_to_node_id[slug] for slug in rerun.start}
        if rerun.mode == "lazy":
            scheduled_node_ids = start_node_ids
        else:
            scheduled_node_ids = set(start_node_ids)
            queue = list(start_node_ids)
            while queue:
                node_id = queue.pop(0)
                for child_id in self.graph.get(node_id, []):
                    if child_id not in scheduled_node_ids:
                        scheduled_node_ids.add(child_id)
                        queue.append(child_id)

        original_dependencies_map = _reverse_graph(self.graph)
        _validate_no_skipped_non_stream_inputs(
            self.nodes,
            scheduled_node_ids,
            original_dependencies_map,
        )
        return [node_id for node_id in execution_order if node_id in scheduled_node_ids]

    def run(
        self,
        executor: "Executor | None" = None,
        hooks: "RunHooks | None" = None,
        input: Any = None,
        context: Any = None,
        run_id: str | None = None,
        rerun: Any = None,
        execution_services: "ExecutionServicesSpec | None" = None,
    ) -> RunHandle[Any]:
        """
        Start the workflow and return its process-local lifecycle handle.

        Args:
            executor: Execution engine (defaults to RayExecutor or LocalExecutor)
            hooks: Optional callbacks for monitoring node lifecycle
            input: Optional run input payload or BaseInput instance
            context: Optional run context payload or BaseContext instance
            run_id: Optional caller-owned run identity
            rerun: Optional Rerun spec for re-executing part of a previous run
            execution_services: Optional versioned worker lifecycle service request

        The handle is returned immediately. Call ``handle.result()`` to block in
        synchronous code or ``await handle`` from asynchronous code.
        """
        canonical_run_id = run_id if run_id is not None else str(ULID())
        handle: RunHandle[Any] = RunHandle(canonical_run_id)
        from runtime.executor import RayExecutor, get_default_executor

        resolved_executor = executor if executor is not None else get_default_executor()
        if isinstance(resolved_executor, RayExecutor):
            resolved_executor._prepare_for_run()

        cancel_requested = handle._compose_cancel_requested(
            hooks.cancel_requested if hooks is not None else None
        )
        driver_hooks = copy(hooks) if hooks is not None else None
        if driver_hooks is not None:
            driver_hooks.cancel_requested = cancel_requested
        handle._start(
            lambda: self._run_driver(
                executor=resolved_executor,
                hooks=driver_hooks,
                cancel_requested=cancel_requested,
                input=input,
                context=context,
                run_id=canonical_run_id,
                rerun=rerun,
                execution_services=execution_services,
                receipt_sink=handle._set_execution_receipts,
            )
        )
        return handle

    def _run_driver(
        self,
        executor: "Executor | None" = None,
        hooks: "RunHooks | None" = None,
        cancel_requested: Callable[[], bool] | None = None,
        input: Any = None,
        context: Any = None,
        run_id: str | None = None,
        rerun: Any = None,
        execution_services: "ExecutionServicesSpec | None" = None,
        receipt_sink: Callable[[tuple[Any, ...]], None] | None = None,
    ) -> Any:
        """Execute the blocking workflow driver in the run-handle thread.

        Implementation:
            1. Topological sort to determine execution order
            2. Submit all nodes
            3. Only fetch explicitly returned values

        Note:
            Intermediate results stay as refs (zero-copy).
        """
        if executor is None:
            from runtime.executor import get_default_executor

            executor = get_default_executor()

        # Topological sort
        execution_order = self._topological_sort()
        rerun_spec = _coerce_rerun(rerun)
        if rerun_spec is not None:
            execution_order = self._plan_rerun_execution(rerun_spec, execution_order)
        scheduled_node_ids = set(execution_order)

        if run_id is None:
            raise ValueError("workflow driver requires a run_id")
        executor_type = "ray" if type(executor).__name__ == "RayExecutor" else "local"
        if execution_services is not None:
            from .execution_services import ExecutionServicesSpec

            if not isinstance(execution_services, ExecutionServicesSpec):
                raise TypeError("execution_services must be an ExecutionServicesSpec")
            if not hasattr(executor, "submit_with_services"):
                raise TypeError(
                    f"{type(executor).__name__} does not support execution services"
                )
        run_input = (
            None
            if execution_services is not None
            else _build_input_value(self.input_type, input)
        )
        system_context, run_context = _build_context_values(
            self.context_type,
            context,
            run_id=run_id,
            workflow_name=self.name,
            executor_type=executor_type,
            rerun=rerun_spec,
        )

        result_refs: dict[str, Any] = {}
        # Ray-only: small per-node status refs from submit_with_status. Fetching
        # a status ref surfaces a task failure without materializing the payload.
        status_refs: dict[str, Any] = {}
        # Service receipts are a separate control channel. They are never
        # stored in result_refs or exposed to workflow arguments/context.
        receipt_refs: dict[str, Any] = {}
        receipt_refs_lock = Lock()

        # Build reverse dependency map (child -> parents) for execution.
        # Keep the original map so rerun stream providers can still identify
        # skipped upstream producer slugs after scheduler pruning.
        original_dependencies_map = _reverse_graph(self.graph)
        dependencies_map = original_dependencies_map
        if rerun_spec is not None:
            dependencies_map = _filter_dependencies(dependencies_map, scheduled_node_ids)

        from .runtime.providers import PROVIDERS

        is_ray_executor = (
            type(executor).__name__ == "RayExecutor"
            and getattr(executor, "ray", None) is not None
        )

        # Ray only: request a small status ref (submit_with_status) when we need
        # to observe completion/failure or drain a no-return workflow without
        # materializing node payloads on the driver.
        needs_status = bool(
            is_ray_executor
            and (
                cancel_requested is not None
                or self.returns is None
                or (
                    hooks
                    and (hooks.on_node_success or hooks.on_node_failure or hooks.unwrap_result)
                )
            )
        )

        def submit_node(
            node_id: str,
            *,
            emit_hooks: bool = True,
        ) -> tuple[NodeFuture, Any]:
            node_ref = self.nodes[node_id]

            if emit_hooks and hooks and hooks.on_node_start:
                hooks.on_node_start(node_id)

            node_system_context = _context_for_node(
                system_context,
                node_id=node_id,
                node_name=node_ref.node.fn.__name__,
                node_slug=node_ref.node_slug,
            )
            node_run_context = _context_for_node(
                run_context,
                node_id=node_id,
                node_name=node_ref.node.fn.__name__,
                node_slug=node_ref.node_slug,
            )
            binding_plan = _build_node_binding_plan(
                node_ref.node.fn,
                node_ref.args,
                node_ref.kwargs,
                node_ref._incoming_refs,
                providers=PROVIDERS,
            )
            binding_kwargs = binding_plan.kwargs
            runtime_binding_kwargs = dict(binding_kwargs)
            if binding_plan.positional_call_adapter is not None:
                runtime_binding_kwargs.update(
                    dict.fromkeys(
                        binding_plan.positional_call_adapter.fixed_arg_names,
                    )
                )
            runtime_params = _inspect_runtime_params(
                node_ref.node.fn,
                (),
                runtime_binding_kwargs,
                run_input,
                node_run_context,
                node_system_context,
                defer_input=execution_services is not None,
            )
            input_param_names = tuple(
                name
                for name, value in runtime_params.items()
                if value is _DEFERRED_EXECUTION_SERVICE_INPUT
            )
            runtime_params = {
                name: value
                for name, value in runtime_params.items()
                if value is not _DEFERRED_EXECUTION_SERVICE_INPUT
            }
            inspected_params = _inspect_providers(node_ref.node.fn, binding_kwargs, PROVIDERS)
            provider_by_param = {}
            position_consuming_params = set()
            for param_name, param_value in inspected_params.items():
                for provider in PROVIDERS:
                    if provider.can_resolve(param_value):
                        provider_by_param[param_name] = provider
                        if getattr(provider, "consumes_upstream", False):
                            position_consuming_params.add(param_name)
                        break
            skip_param_names = (
                set(runtime_params)
                | set(input_param_names)
                | (set(inspected_params) - position_consuming_params)
            )

            injectable_params = {}
            if inspected_params:
                from .types import ParamContext

                original_parent_ids = original_dependencies_map.get(node_id, [])
                provider_parent_results = [result_refs.get(pid) for pid in original_parent_ids]
                upstream_node_slugs = [
                    self.node_slugs[parent_id]
                    for parent_id in original_parent_ids
                    for _ in range(self.nodes[parent_id].node.num_returns)
                ]

                for param_name, param_value in inspected_params.items():
                    provider = provider_by_param.get(param_name)
                    if provider is None:
                        continue
                    selector = binding_plan.provider_selectors.get(param_name)
                    if selector is not None:
                        selected_result = result_refs.get(selector.future_id)
                        if selected_result is not None and selector.tuple_index is not None:
                            selected_result = _indexed_parent_result(
                                selected_result,
                                selector.tuple_index,
                                executor,
                            )
                        parent_results = [selected_result]
                        param_position = 0
                        parent_slugs = [self.node_slugs[selector.future_id]]
                    else:
                        parent_results = provider_parent_results
                        param_position = _data_param_position(
                            node_ref.node.fn,
                            param_name,
                            skip_param_names,
                            position_consuming_params,
                        )
                        parent_slugs = upstream_node_slugs

                    param_context = ParamContext(
                        parent_results=parent_results,
                        param_position=param_position,
                        node_name=node_ref.node.fn.__name__,
                        node_slug=node_ref.node_slug,
                        upstream_node_slugs=parent_slugs,
                        run_id=run_id,
                        rerun=rerun_spec,
                        preserve_missing_results=rerun_spec is not None,
                        executor_type=executor_type,
                        executor=executor,
                    )

                    resolved_value = provider.resolve(param_value, param_context)
                    injectable_params[param_name] = (
                        provider,
                        param_value,
                        resolved_value,
                    )

            # Resolve explicit arguments (args and kwargs use same logic)
            resolved_args = [
                _resolve_to_ref(arg, result_refs, executor) for arg in binding_plan.args
            ]
            resolved_kwargs = {
                k: _resolve_to_ref(v, result_refs, executor)
                for k, v in binding_kwargs.items()
                # Exclude injectable params from normal resolution
                if k not in injectable_params and k not in runtime_params
            }
            if execution_services is None:
                resolved_args, resolved_kwargs = _resolve_input_refs(
                    resolved_args,
                    resolved_kwargs,
                    run_input,
                    node_ref.node.fn.__name__,
                    self.name,
                )

            # Get original function, optionally wrapped by hooks
            actual_fn = node_ref.node.fn
            agent_step_spec = getattr(actual_fn, "__agent_step__", None)
            if agent_step_spec is not None:
                actual_fn = agent_step_spec.with_workflow_defaults(
                    actual_fn, self.agent_defaults
                )
            if hooks and hooks.wrap_fn:
                actual_fn = hooks.wrap_fn(node_id, actual_fn)
            if binding_plan.positional_call_adapter is not None:
                actual_fn = _adapt_positional_call(
                    actual_fn,
                    node_ref.node.fn,
                    binding_plan.positional_call_adapter,
                )

            # Implicit data passing: If a node was called with no explicit
            # arguments, route parent results by logical upstream slot order.
            # This enables: `a() >> b() >> c()` without manual wiring.
            # Injectable params (Logger, Stream, etc.) are excluded — they're
            # handled separately by the provider system.
            if not node_ref.args and not binding_plan.has_explicit_provider_selectors:
                upstream_values = _collect_implicit_parent_results(
                    node_ref,
                    result_refs,
                    dependencies_map,
                    node_id,
                    executor=executor,
                )
                implicit_args, implicit_kwargs = _bind_implicit_parent_results(
                    node_ref.node.fn,
                    upstream_values,
                    skip_param_names,
                    position_consuming_params,
                    resolved_kwargs,
                    adapt_positionals=binding_plan.positional_call_adapter is not None,
                )
                resolved_args = implicit_args
                resolved_kwargs.update(implicit_kwargs)

            # Handle injectable parameters that may need wrappers (using provider pattern)
            #
            # Some providers wrap the function to add behavior (e.g., context managers).
            # Examples:
            #   - Stream: Returns wrapper to enter consume_stream() context manager
            #   - Logger: Returns None (no wrapper needed, just inject LoggerInstance)
            #   - Future Config: Might return wrapper for transaction/connection management
            #
            # Grouping: If a function has multiple params of the SAME provider type
            # (e.g., docs=Stream(...), chunks=Stream(...)), they're wrapped TOGETHER
            # in a single wrapper that manages all contexts.
            #
            # Chaining: If multiple DIFFERENT provider types return wrappers, they're
            # applied in sequence: actual_fn = provider2(provider1(original_fn))
            if injectable_params:
                # Step 1: Group parameters by provider type
                # Structure: {provider_id: (provider, param_value, {param_name: resolved})}
                params_by_provider = {}
                for param_name, (
                    provider,
                    param_value,
                    resolved_value,
                ) in injectable_params.items():
                    provider_id = id(provider)
                    if provider_id not in params_by_provider:
                        params_by_provider[provider_id] = (provider, param_value, {})
                    params_by_provider[provider_id][2][param_name] = resolved_value

                # Apply wrappers or collect params for direct injection
                params_to_inject = {}
                for provider, param_value, resolved_params in params_by_provider.values():
                    # Ray dependency visibility: for deferred Stream upstreams,
                    # lift the parent payload ref into a TOP-LEVEL hidden task
                    # kwarg so Ray tracks it as a real scheduling dependency
                    # (the consumer must not start before the producer). Generate
                    # a unique, node+param-scoped hidden kwarg name here (where
                    # node_id and param name are available), stamp it onto the
                    # carrier, and clear the ref so the wrapper closure never
                    # serializes an ObjectRef.
                    for rp_name, rp_value in list(resolved_params.items()):
                        deferred = _deferred_stream_upstream_of(rp_value)
                        if deferred is not None and deferred.ref is not None:
                            parent_kwarg = _safe_hidden_kwarg(
                                node_id,
                                rp_name,
                                {**resolved_kwargs, **runtime_params},
                            )
                            resolved_kwargs[parent_kwarg] = deferred.ref
                            resolved_params[rp_name] = _stamp_deferred_stream_upstream(
                                rp_value,
                                parent_kwarg=parent_kwarg,
                            )

                    wrapper = provider.create_wrapper(param_value, actual_fn, resolved_params)
                    if wrapper is not None:
                        actual_fn = wrapper
                    else:
                        params_to_inject.update(resolved_params)

                resolved_kwargs.update(params_to_inject)

            resolved_kwargs.update(runtime_params)
            actual_fn = _with_current_run_context(
                actual_fn,
                node_run_context,
                num_returns=node_ref.node.num_returns,
            )

            try:
                if execution_services is not None:
                    from .execution_services import ExecutionTaskSpec

                    with receipt_refs_lock:
                        parent_receipts = tuple(
                            receipt_refs[parent_id]
                            for parent_id in dependencies_map.get(node_id, [])
                            if parent_id in receipt_refs
                        )
                    result, receipt_ref, status_ref = executor.submit_with_services(
                        actual_fn,
                        execution_services,
                        ExecutionTaskSpec(
                            run_id=run_id,
                            workflow_name=self.name,
                            node_id=node_id,
                            node_name=node_ref.node.fn.__name__,
                            node_slug=node_ref.node_slug,
                            executor_type=executor_type,
                        ),
                        self.input_type,
                        input,
                        input_param_names,
                        parent_receipts,
                        node_ref.node.num_returns,
                        *resolved_args,
                        **resolved_kwargs,
                    )
                    with receipt_refs_lock:
                        receipt_refs[node_id] = receipt_ref
                    if is_ray_executor and status_ref is not None:
                        # Status is a same-task completion/failure marker. Keep
                        # receipts worker-side until terminal publication.
                        status_refs[node_id] = status_ref
                elif needs_status and hasattr(executor, "submit_with_status"):
                    # Ray: get a small status ref alongside the payload so
                    # completion/failure can be observed without materializing
                    # the payload. The status ref is produced by the same task.
                    result, status_ref = executor.submit_with_status(
                        actual_fn,
                        *resolved_args,
                        num_returns=node_ref.node.num_returns,
                        **resolved_kwargs,
                    )
                    status_refs[node_id] = status_ref
                else:
                    result = executor.submit(
                        actual_fn,
                        *resolved_args,
                        num_returns=node_ref.node.num_returns,
                        **resolved_kwargs,
                    )
            except Exception as exc:
                if emit_hooks and hooks and hooks.on_node_failure:
                    hooks.on_node_failure(node_id, exc)
                raise

            return node_ref, result

        def publish_terminal_receipts() -> None:
            if receipt_sink is None:
                return
            if execution_services is None:
                receipt_sink(())
                return

            from .execution_services import ExecutionServiceReceipt

            terminal_node_ids = [
                node_id
                for node_id in execution_order
                if not any(
                    child_id in scheduled_node_ids for child_id in self.graph.get(node_id, [])
                )
            ]
            refs = [receipt_refs[node_id] for node_id in terminal_node_ids]
            values = executor.get(refs) if refs else []
            receipt_sink(
                tuple(
                    ExecutionServiceReceipt(
                        node_id=node_id,
                        node_slug=self.node_slugs[node_id],
                        value=value,
                    )
                    for node_id, value in zip(terminal_node_ids, values)
                )
            )

        def resolve_submitted_result(node_ref: NodeFuture, result: Any) -> Any:
            if node_ref.node.num_returns > 1 and isinstance(result, tuple):
                value: Any = executor.get(list(result))
            elif _should_fetch_with_executor(result, executor):
                value = executor.get([result])[0]
            else:
                value = result
            # unwrap_result / driver boundary: convert internal handles back to
            # public AppendResult (payload fetch here is intentional).
            return _materialize_append_handles_for_driver(value, executor)

        def complete_non_ray_node(
            node_id: str,
            node_ref: NodeFuture,
            result: Any,
        ) -> None:
            try:
                if hooks and (hooks.on_node_success or hooks.unwrap_result):
                    resolved_val = resolve_submitted_result(node_ref, result)
                    if hooks.unwrap_result:
                        user_val = _unwrap_lineaged_tree(resolved_val)
                        replacement = hooks.unwrap_result(node_id, user_val)
                        result = _reattach_lineage(replacement, resolved_val)
                    else:
                        result = resolved_val
                    if hooks.on_node_success:
                        hooks.on_node_success(node_id)
                result_refs[node_id] = result
            except Exception as exc:
                if hooks and hooks.on_node_failure:
                    hooks.on_node_failure(node_id, exc)
                raise

        from runtime.executor import LocalExecutor

        is_local_executor = isinstance(executor, LocalExecutor)

        observe_ray_completion = bool(
            is_ray_executor
            and (
                cancel_requested is not None
                or (
                    hooks
                    and (hooks.on_node_success or hooks.on_node_failure or hooks.unwrap_result)
                )
            )
        )

        if observe_ray_completion:
            ray = executor.ray
            pending_refs: dict[Any, str] = {}
            node_pending_refs: dict[str, list[Any]] = {}
            completed_nodes: set[str] = set()

            def ray_refs_for_result(result: Any) -> list[Any]:
                if isinstance(result, (tuple, list)):
                    return [item for item in result if _is_executor_ref(item, executor)]
                if _is_executor_ref(result, executor):
                    return [result]
                return []

            def complete_ray_node(node_id: str) -> None:
                node_ref = self.nodes[node_id]
                result = result_refs[node_id]
                try:
                    if hooks and hooks.unwrap_result:
                        # unwrap_result needs the user-facing value, so a payload
                        # fetch here is intentional. Keep the lineage-preserving
                        # envelope in result_refs for downstream dataflow,
                        # reattaching any hook replacement.
                        resolved_val = resolve_submitted_result(node_ref, result)
                        user_val = _unwrap_lineaged_tree(resolved_val)
                        replacement = hooks.unwrap_result(node_id, user_val)
                        result_refs[node_id] = _reattach_lineage(replacement, resolved_val)
                    elif node_id in status_refs:
                        # Progress-only: fetch just the tiny status ref to
                        # surface a task failure. Never materialize the payload;
                        # result_refs keeps the payload ref for downstream tasks.
                        executor.get([status_refs[node_id]])
                    if hooks and hooks.on_node_success:
                        hooks.on_node_success(node_id)
                    completed_nodes.add(node_id)
                except Exception as exc:
                    if hooks and hooks.on_node_failure:
                        hooks.on_node_failure(node_id, exc)
                    raise

            def track_ray_node(node_id: str, result: Any) -> None:
                # Prefer waiting on the tiny status ref (progress-only mode) so
                # readiness never depends on materializing the payload. When
                # unwrap_result is active we still only wait here; the payload
                # fetch happens in complete_ray_node.
                if node_id in status_refs:
                    refs = [status_refs[node_id]]
                else:
                    refs = ray_refs_for_result(result)
                if not refs:
                    complete_ray_node(node_id)
                    return
                node_pending_refs[node_id] = refs
                for ref in refs:
                    pending_refs[ref] = node_id

            def drain_ray_completions(*, block: bool) -> bool:
                if not pending_refs:
                    return False

                timeout = 0.1 if block else 0
                ready, _ = ray.wait(
                    list(pending_refs.keys()),
                    num_returns=1 if block else len(pending_refs),
                    timeout=timeout,
                )
                if not ready:
                    return False

                more_ready, _ = ray.wait(
                    list(pending_refs.keys()),
                    num_returns=len(pending_refs),
                    timeout=0,
                )
                ready_refs = list(dict.fromkeys([*ready, *more_ready]))

                for ref in ready_refs:
                    node_id = pending_refs.pop(ref)
                    refs = node_pending_refs[node_id]
                    if any(node_ref in pending_refs for node_ref in refs):
                        continue

                    del node_pending_refs[node_id]
                    complete_ray_node(node_id)

                return True

            def dependencies_ready(node_id: str) -> bool:
                return all(
                    parent_id in completed_nodes
                    for parent_id in dependencies_map.get(node_id, [])
                )

            remaining_nodes = list(execution_order)
            cancelled = False

            while remaining_nodes:
                submitted_any = False

                for node_id in list(remaining_nodes):
                    # Check cancellation before starting each node
                    if cancel_requested and cancel_requested():
                        cancelled = True
                        break
                    if not dependencies_ready(node_id):
                        continue

                    _node_ref, result = submit_node(node_id)
                    result_refs[node_id] = result
                    track_ray_node(node_id, result)
                    remaining_nodes.remove(node_id)
                    submitted_any = True

                if cancelled:
                    break
                if submitted_any:
                    continue
                if pending_refs:
                    drain_ray_completions(block=True)
                    if pending_refs and cancel_requested and cancel_requested():
                        cancelled = True
                    continue
                break

            while pending_refs:
                try:
                    drain_ray_completions(block=True)
                except Exception:
                    if not cancelled:
                        raise
                if pending_refs and cancel_requested and cancel_requested():
                    cancelled = True
            if cancelled:
                raise CancelledError()
        elif is_local_executor:
            completed_nodes: set[str] = set()
            remaining_nodes = list(execution_order)
            pending_tasks: dict[Future[tuple[NodeFuture, Any]], str] = {}
            primary_failure: Exception | None = None
            cancelled = False

            def dependencies_ready(node_id: str) -> bool:
                return all(
                    parent_id in completed_nodes
                    for parent_id in dependencies_map.get(node_id, [])
                )

            with ThreadPoolExecutor(
                max_workers=executor.max_workers,
                thread_name_prefix=f"avalanche-local-{run_id}",
            ) as pool:
                while remaining_nodes or pending_tasks:
                    if primary_failure is None and not cancelled:
                        for node_id in list(remaining_nodes):
                            if cancel_requested and cancel_requested():
                                cancelled = True
                                break
                            if not dependencies_ready(node_id):
                                continue

                            if hooks and hooks.on_node_start:
                                hooks.on_node_start(node_id)
                            task = pool.submit(submit_node, node_id, emit_hooks=False)
                            pending_tasks[task] = node_id
                            remaining_nodes.remove(node_id)

                    if not pending_tasks:
                        if primary_failure is not None or cancelled:
                            break
                        if remaining_nodes:
                            raise RuntimeError("Local workflow scheduler has no runnable nodes")
                        break

                    wait(tuple(pending_tasks), return_when=FIRST_COMPLETED)
                    completed_tasks = [task for task in pending_tasks if task.done()]
                    for task in completed_tasks:
                        node_id = pending_tasks.pop(task)
                        try:
                            node_ref, result = task.result()
                        except Exception as exc:
                            if hooks and hooks.on_node_failure:
                                hooks.on_node_failure(node_id, exc)
                            if primary_failure is None:
                                primary_failure = exc
                            continue

                        try:
                            complete_non_ray_node(node_id, node_ref, result)
                        except Exception as exc:
                            if primary_failure is None:
                                primary_failure = exc
                        else:
                            completed_nodes.add(node_id)

            if primary_failure is not None:
                raise primary_failure
            if cancelled:
                raise CancelledError()
        else:
            for node_id in execution_order:
                # Check cancellation before starting each node
                if cancel_requested and cancel_requested():
                    raise CancelledError()

                node_ref, result = submit_node(node_id)
                complete_non_ray_node(node_id, node_ref, result)

        # If workflow doesn't return anything, wait for all nodes to complete.
        # Skip if hooks were active — we already resolved per-node above.
        if self.returns is None:
            already_observed = bool(
                hooks
                and (hooks.on_node_success or hooks.on_node_failure or hooks.unwrap_result)
            )
            if already_observed:
                # Per-node completion/failure was already observed above (hooks).
                publish_terminal_receipts()
                return None
            if is_ray_executor and status_refs:
                # Fetch only the tiny status refs: this surfaces any task
                # failure without materializing node payloads on the driver.
                pending_status = [
                    status_refs[fid] for fid in execution_order if fid in status_refs
                ]
                if pending_status:
                    executor.get(pending_status)
                publish_terminal_receipts()
                return None
            # Local / non-status fallback: values are already materialized for
            # LocalExecutor; resolving them just surfaces awaitables/exceptions.
            all_refs = []
            for fid in execution_order:
                ref = result_refs.get(fid)
                if ref is None:
                    continue  # Node was skipped (cancellation)
                if isinstance(ref, (tuple, list)):
                    all_refs.extend(ref)
                else:
                    all_refs.append(ref)
            if all_refs:
                executor.get(all_refs)  # Wait for completion
            publish_terminal_receipts()
            return None

        # Fetch only what the workflow explicitly returns
        # Handle different return types
        publish_terminal_receipts()
        if isinstance(self.returns, NodeFuture):
            # Single NodeFuture returned
            return _fetch_node_result(
                self.returns,
                result_refs,
                self.nodes,
                executor,
                scheduled_node_ids if rerun_spec is not None else None,
            )
        elif isinstance(self.returns, tuple):
            # Tuple of NodeFutures returned
            return tuple(
                _fetch_node_result(
                    nf,
                    result_refs,
                    self.nodes,
                    executor,
                    scheduled_node_ids if rerun_spec is not None else None,
                )
                if isinstance(nf, NodeFuture)
                else nf
                for nf in self.returns
            )
        else:
            # Return value as-is (not a NodeFuture)
            return self.returns

    def __repr__(self) -> str:
        edge_count = sum(len(v) for v in self.graph.values())
        return f"Workflow(name={self.name!r}, " f"nodes={len(self.nodes)}, edges={edge_count})"


# Backward-compatible vocabulary aliases.
Pipeline = Workflow


def workflow(
    fn: Callable[[], None] | None = None,
    *,
    cron: str | None = None,
    webhook: Webhook | bool | None = None,
    input: type | None = None,
    context: type | None = None,
    ctx: type | None = None,
    agent_defaults: dict[str, Any] | None = None,
) -> Callable[[], Workflow] | Callable[[Callable[[], None]], Callable[[], Workflow]]:
    """
    Decorator for workflow definitions.

    The decorated function builds a DAG using >> and & operators.
    Returns a Workflow object that can be executed with .run().

    Can be used bare or with parameters:
        @ava.workflow
        def my_workflow():
            load() >> step() >> export()

        @ava.workflow(cron="0 * * * *")
        def hourly_workflow():
            fetch() >> process() >> save()

        @ava.workflow(input=MyInput, context=MyContext)
        def parameterized_workflow():
            process()

    Args:
        cron: Optional cron expression for scheduled execution.
        input: Optional BaseInput subclass validated at run start.
        context: Optional BaseContext subclass validated at run start.
        ctx: Alias for context.

    Returns:
        Function that returns a Workflow object when called
    """

    if context is not None and ctx is not None and context is not ctx:
        raise ValueError("Use either context= or ctx=, not both")
    context_type = context or ctx
    webhook = _normalize_webhook(webhook)
    if agent_defaults is not None:
        from .agent.config import validate_runtime_kwargs

        agent_defaults = validate_runtime_kwargs(
            agent_defaults, owner="ava.workflow(agent_defaults=...)"
        )

    def decorator(fn: Callable[[], None]) -> Callable[[], Workflow]:
        @wraps(fn)
        def wrapper() -> Workflow:
            ctx = WorkflowContext()
            token = _workflow_context.set(ctx)

            try:
                result = fn()

                return Workflow(
                    graph=ctx.graph,
                    nodes=ctx.node_instances.copy(),
                    node_slugs=ctx.node_slugs.copy(),
                    name=fn.__name__,
                    returns=result,
                    cron=cron,
                    webhook=webhook,
                    input_type=input,
                    context_type=context_type,
                    agent_defaults=agent_defaults,
                )
            finally:
                _workflow_context.reset(token)

        # Discovery relies on an explicit marker instead of probing arbitrary callables.
        # Keep this attribute stable: it is part of the operator/discovery boundary.
        wrapper.__avalanche_workflow__ = True  # type: ignore[attr-defined]
        return wrapper

    # Support both @workflow and @workflow(cron="...")
    if fn is not None:
        return decorator(fn)
    return decorator


pipeline = workflow


def _normalize_webhook(value: Webhook | bool | None) -> Webhook | None:
    if value is True:
        return Webhook()
    if value is False or value is None:
        return None
    if not isinstance(value, Webhook):
        raise TypeError("webhook must be ava.Webhook, True, False, or None")
    return value
