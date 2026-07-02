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
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from functools import update_wrapper, wraps
from typing import TYPE_CHECKING, Any, Callable, DefaultDict, TypeVar

from ulid import ULID

if TYPE_CHECKING:
    from .executor import Executor
    from .operator.hooks import RunHooks

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class WorkflowContext:
    """Thread-local context for workflow construction.

    Each workflow construction gets its own isolated context via contextvars,
    ensuring thread-safety and proper isolation for concurrent/nested workflows.
    """

    graph: DefaultDict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    instance_counter: dict[str, int] = field(default_factory=dict)
    node_instances: dict[str, "NodeFuture"] = field(default_factory=dict)


# Thread-local context variable for workflow construction
_workflow_context: ContextVar[WorkflowContext | None] = ContextVar(
    "_workflow_context", default=None
)


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

    def __init__(self, fn: Callable[..., Any], node_type: NodeType, num_returns: int = 1):
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

        result = NodeFuture(
            node=self,
            future_id=future_id,
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
                ctx.graph[arg.future_id].append(future_id)

        for kwarg_val in kwargs.values():
            if isinstance(kwarg_val, NodeFuture):
                ctx.graph[kwarg_val.future_id].append(future_id)

        return result

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"Node({self.name}, type={self.node_type.value})"


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
        if isinstance(self.chain_end, ParallelTasks):
            ids = []
            for branch in self.chain_end.branches:
                ids.extend(branch.as_dependency_ids())
            return ids
        else:
            return [self.chain_end.future_id]

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
        dependency_ids = self.as_dependency_ids()

        if isinstance(next, NodeFuture):  # (set parent as dependency of next)
            # Graph format: {parent: [children]}
            for d in dependency_ids:
                self.graph_ref[d].append(next.future_id)

            # Track incoming ref (preserves tuple_index for data passing)
            if isinstance(self.chain_end, NodeFuture):
                next._incoming_refs.append(self.chain_end)
            elif isinstance(self.chain_end, ParallelTasks):
                next._incoming_refs.extend(self.chain_end.branches)

            return NodeFuture(
                node=self.chain_start.node,
                future_id=self.chain_start.future_id,
                graph_ref=self.graph_ref,
                chain_start=self.chain_start,
                chain_end=next.chain_end,
            )
        else:  # ParallelTasks (set parent as dependency of each branch in next)
            # Graph format: {parent: [children]}
            for d in dependency_ids:
                for branch in next.branches:
                    self.graph_ref[d].append(branch.future_id)

            # Track incoming ref for each branch (preserves tuple_index for data passing)
            for branch in next.branches:
                if isinstance(self.chain_end, NodeFuture):
                    branch._incoming_refs.append(self.chain_end)
                elif isinstance(self.chain_end, ParallelTasks):
                    branch._incoming_refs.extend(self.chain_end.branches)

            return NodeFuture(
                node=self.chain_start.node,
                future_id=self.chain_start.future_id,
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
        self.branches = branches
        self.graph_ref = graph_ref
        self.chain_start = branches[0].chain_start

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
        for branch in self.branches:
            branch_dep_ids = branch.as_dependency_ids()
            for dep_id in branch_dep_ids:
                if isinstance(next, NodeFuture):
                    self.graph_ref[dep_id].append(next.future_id)
                else:  # ParallelTasks
                    for next_branch in next.branches:
                        self.graph_ref[dep_id].append(next_branch.future_id)

        if isinstance(next, NodeFuture):
            return NodeFuture(
                node=self.chain_start.node,
                future_id=self.chain_start.future_id,
                graph_ref=self.graph_ref,
                chain_start=self.chain_start,
                chain_end=next.chain_end,
            )
        else:  # ParallelTasks
            return NodeFuture(
                node=self.chain_start.node,
                future_id=self.chain_start.future_id,
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
            self.branches.extend(other.branches)
        else:
            self.branches.append(other)
        return self


# Decorators


def source(fn: F = None, *, num_returns: int = 1) -> Node | Callable[[F], Node]:
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
        return Node(f, NodeType.SOURCE, num_returns=num_returns)

    if fn is None:
        return decorator
    return decorator(fn)


def step(fn: F = None, *, num_returns: int = 1) -> Node | Callable[[F], Node]:
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
        return Node(f, NodeType.STEP, num_returns=num_returns)

    if fn is None:
        return decorator
    return decorator(fn)


transform = step


def dest(fn: F = None, *, num_returns: int = 1) -> Node | Callable[[F], Node]:
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
        return Node(f, NodeType.DEST, num_returns=num_returns)

    if fn is None:
        return decorator
    return decorator(fn)


# Workflow execution helpers


def _resolve_to_ref(arg, result_refs):
    """Convert NodeFuture argument to its ref, handling tuple indexing."""
    if isinstance(arg, NodeFuture):
        ref = result_refs[arg.future_id]
        return ref[arg.tuple_index] if arg.tuple_index is not None else ref
    return arg


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
            "docs": Stream(table, key="docs_to_chunks"),
            "chunks": Stream(table, key="chunks_to_embeddings"),
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
    #              data: pl.DataFrame = Stream(table_a, key="default"),
    #              logger=Logger()
    #          ):
    #              ...
    #
    #   - Override: process(data=Stream(table_b, key="custom"))  # Explicit wins over default
    #   - Explicit: process(data=Stream(table, key="key"))       # When no default exists
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
    #              data: pl.DataFrame = Stream(table, key="docs"),
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


def _fetch_node_result(nf: NodeFuture, result_refs, nodes, executor):
    """Fetch the actual result value for a NodeFuture, handling chains and multi-return."""
    # If this is a chain composite, fetch the chain_end instead
    fetch_target = nf.chain_end if nf.chain_end != nf else nf

    # Handle ParallelTasks: return tuple of all branch results
    if isinstance(fetch_target, ParallelTasks):
        results = []
        for branch in fetch_target.branches:
            branch_result = _fetch_node_result(branch, result_refs, nodes, executor)
            results.append(branch_result)
        return tuple(results)

    # Regular NodeFuture handling
    ref = result_refs[fetch_target.future_id]
    node = nodes[fetch_target.future_id]

    # Handle tuple indexing if this is an indexed ref
    if fetch_target.tuple_index is not None:
        # Indexed into a multi-return node
        refs_to_fetch = list(ref)  # ref is tuple/list of refs
        if _all_fetchable_with_executor(refs_to_fetch, executor):
            fetched = executor.get(refs_to_fetch)
            return fetched[fetch_target.tuple_index]
        return refs_to_fetch[fetch_target.tuple_index]
    elif node.node.num_returns > 1:
        # Multi-return node: ref is tuple/list of refs
        refs_to_fetch = list(ref)
        if _all_fetchable_with_executor(refs_to_fetch, executor):
            fetched = executor.get(refs_to_fetch)
            return tuple(fetched)
        return tuple(refs_to_fetch)
    else:
        # Single return node: ref is a single ref
        if _should_fetch_with_executor(ref, executor):
            return executor.get([ref])[0]
        return ref


def _implicit_value_from_upstream(item: Any) -> Any:
    """Convert upstream AppendResult values the same way legacy auto-args did."""
    from .types import AppendResult

    return item.data if isinstance(item, AppendResult) else item


def _collect_implicit_parent_results(
    node_ref: NodeFuture,
    result_refs: dict[str, Any],
    dependencies_map: dict[str, list[str]],
    node_id: str,
) -> list[Any]:
    """Collect flattened parent results for implicit data passing."""
    auto_values: list[Any] = []

    # Use _incoming_refs if available (preserves tuple_index info).
    # Otherwise fall back to dependencies_map.
    if node_ref._incoming_refs:
        for incoming in node_ref._incoming_refs:
            presult = result_refs.get(incoming.future_id)
            if presult is not None:
                if incoming.tuple_index is not None:
                    auto_values.append(presult[incoming.tuple_index])
                else:
                    items = presult if isinstance(presult, (tuple, list)) else [presult]
                    auto_values.extend(items)
    else:
        parent_ids = dependencies_map.get(node_id, [])
        for pid in parent_ids:
            presult = result_refs.get(pid)
            if presult is not None:
                items = presult if isinstance(presult, (tuple, list)) else [presult]
                auto_values.extend(items)

    return auto_values


def _bind_implicit_parent_results(
    fn: Callable[..., Any],
    upstream_values: list[Any],
    injectable_params: dict[str, Any],
    resolved_kwargs: dict[str, Any],
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

    var_pos_index = next(
        (
            index
            for index, param in enumerate(params)
            if param.kind == inspect.Parameter.VAR_POSITIONAL
        ),
        None,
    )
    if var_pos_index is not None and len(upstream_values) > var_pos_index:
        provider_conflicts = [
            param.name
            for index, param in enumerate(params[:var_pos_index])
            if index < len(upstream_values) and param.name in injectable_params
        ]
        if provider_conflicts:
            names = ", ".join(provider_conflicts)
            raise TypeError(
                f"Cannot implicitly bind upstream results to *args for {fn.__name__}: "
                f"injectable parameter(s) would be double-bound by position and keyword: "
                f"{names}"
            )

        kwarg_conflicts = [
            param.name
            for index, param in enumerate(params[:var_pos_index])
            if (
                index < len(upstream_values)
                and param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
                and param.name in resolved_kwargs
            )
        ]
        if kwarg_conflicts:
            names = ", ".join(kwarg_conflicts)
            raise TypeError(
                f"Cannot implicitly bind upstream results to *args for {fn.__name__}: "
                f"explicit keyword argument(s) would be double-bound by position: {names}"
            )

        return [_implicit_value_from_upstream(item) for item in upstream_values], {}

    resolved_args: list[Any] = []
    implicit_kwargs: dict[str, Any] = {}

    for index, item in enumerate(upstream_values):
        if index >= len(params):
            raise TypeError(
                f"Cannot implicitly bind upstream result at position {index} for "
                f"{fn.__name__}: function signature has no parameter at that position"
            )

        param = params[index]
        value = _implicit_value_from_upstream(item)

        if param.kind == inspect.Parameter.POSITIONAL_ONLY:
            if param.name in injectable_params:
                raise TypeError(
                    f"Cannot inject provider parameter {param.name!r} for {fn.__name__}: "
                    "positional-only parameters cannot be injected by keyword"
                )
            resolved_args.append(value)
            continue

        if param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD:
            if param.name in injectable_params:
                continue
            if param.name in resolved_kwargs:
                raise TypeError(
                    f"Cannot implicitly bind upstream result at position {index} for "
                    f"{fn.__name__}: parameter {param.name!r} was also passed explicitly"
                )
            implicit_kwargs[param.name] = value
            continue

        raise TypeError(
            f"Cannot implicitly bind upstream result at position {index} for "
            f"{fn.__name__}: parameter {param.name!r} is {param.kind.description}"
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
        name: str = "workflow",
        returns: Any = None,
        cron: str | None = None,
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
        self.name = name
        self.returns = returns
        self.cron = cron

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

    def run(
        self,
        executor: "Executor | None" = None,
        hooks: "RunHooks | None" = None,
    ) -> Any:
        """
        Execute the workflow.

        Args:
            executor: Execution engine (defaults to RayExecutor or LocalExecutor)
            hooks: Optional callbacks for monitoring node lifecycle

        Returns:
            - If workflow returns NodeFuture: the computed value
            - If workflow returns tuple of NodeFutures: tuple of computed values
            - If workflow returns nothing: None
            - Otherwise: the return value as-is

        Implementation:
            1. Topological sort to determine execution order
            2. Submit all nodes
            3. Only fetch explicitly returned values

        Note:
            Intermediate results stay as refs (zero-copy).
        """
        if executor is None:
            from .executor import get_default_executor

            executor = get_default_executor()

        # Topological sort
        execution_order = self._topological_sort()

        execution_id = str(ULID())
        executor_type = "ray" if type(executor).__name__ == "RayExecutor" else "local"

        result_refs: dict[str, Any] = {}

        # Build reverse dependency map (child -> parents) for execution
        dependencies_map = _reverse_graph(self.graph)

        from .runtime.providers import PROVIDERS

        def submit_node(node_id: str) -> tuple[NodeFuture, Any]:
            node_ref = self.nodes[node_id]

            if hooks and hooks.on_node_start:
                hooks.on_node_start(node_id)

            inspected_params = _inspect_providers(node_ref.node.fn, node_ref.kwargs, PROVIDERS)

            injectable_params = {}
            if inspected_params:
                import inspect

                from .types import ParamContext

                parent_ids = dependencies_map.get(node_id, [])
                parent_results = [result_refs.get(pid) for pid in parent_ids]

                sig = inspect.signature(node_ref.node.fn)
                param_order = list(sig.parameters.keys())

                for param_name, param_value in inspected_params.items():
                    param_position = (
                        param_order.index(param_name) if param_name in param_order else -1
                    )

                    param_context = ParamContext(
                        parent_results=parent_results,
                        param_position=param_position,
                        node_name=node_ref.node.fn.__name__,
                        execution_id=execution_id,
                        executor_type=executor_type,
                    )

                    for provider in PROVIDERS:
                        if provider.can_resolve(param_value):
                            resolved_value = provider.resolve(param_value, param_context)
                            injectable_params[param_name] = (
                                provider,
                                param_value,
                                resolved_value,
                            )
                            break

            # Resolve explicit arguments (args and kwargs use same logic)
            resolved_args = [_resolve_to_ref(arg, result_refs) for arg in node_ref.args]
            resolved_kwargs = {
                k: _resolve_to_ref(v, result_refs)
                for k, v in node_ref.kwargs.items()
                # Exclude injectable params from normal resolution
                if k not in injectable_params
            }

            # Get original function, optionally wrapped by hooks
            actual_fn = node_ref.node.fn
            if hooks and hooks.wrap_fn:
                actual_fn = hooks.wrap_fn(node_id, actual_fn)

            # Implicit data passing: If node was called with no explicit arguments
            # and its function signature can accept positional arguments,
            # automatically pass parent results by signature position.
            # This enables: `a() >> b() >> c()` without manual wiring.
            # Injectable params (Logger, Stream, etc.) are excluded — they're
            # handled separately by the provider system.
            if not resolved_args:
                upstream_values = _collect_implicit_parent_results(
                    node_ref,
                    result_refs,
                    dependencies_map,
                    node_id,
                )
                implicit_args, implicit_kwargs = _bind_implicit_parent_results(
                    node_ref.node.fn,
                    upstream_values,
                    injectable_params,
                    resolved_kwargs,
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
                    provider_id = id(type(provider))
                    if provider_id not in params_by_provider:
                        params_by_provider[provider_id] = (provider, param_value, {})
                    params_by_provider[provider_id][2][param_name] = resolved_value

                # Apply wrappers or collect params for direct injection
                params_to_inject = {}
                for provider, param_value, resolved_params in params_by_provider.values():
                    wrapper = provider.create_wrapper(param_value, actual_fn, resolved_params)
                    if wrapper is not None:
                        actual_fn = wrapper
                    else:
                        params_to_inject.update(resolved_params)

                resolved_kwargs.update(params_to_inject)

            try:
                result = executor.submit(
                    actual_fn,
                    *resolved_args,
                    num_returns=node_ref.node.num_returns,
                    **resolved_kwargs,
                )
            except Exception as exc:
                if hooks and hooks.on_node_failure:
                    hooks.on_node_failure(node_id, exc)
                raise

            return node_ref, result

        def resolve_submitted_result(node_ref: NodeFuture, result: Any) -> Any:
            if node_ref.node.num_returns > 1 and isinstance(result, tuple):
                return executor.get(list(result))
            if _should_fetch_with_executor(result, executor):
                return executor.get([result])[0]
            return result

        observe_ray_hooks = bool(
            hooks
            and type(executor).__name__ == "RayExecutor"
            and getattr(executor, "ray", None) is not None
            and (hooks.on_node_success or hooks.on_node_failure or hooks.unwrap_result)
        )

        if observe_ray_hooks:
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
                    resolved_val = resolve_submitted_result(node_ref, result)
                    if hooks and hooks.unwrap_result:
                        result_refs[node_id] = hooks.unwrap_result(node_id, resolved_val)
                    if hooks and hooks.on_node_success:
                        hooks.on_node_success(node_id)
                    completed_nodes.add(node_id)
                except Exception as exc:
                    if hooks and hooks.on_node_failure:
                        hooks.on_node_failure(node_id, exc)
                    raise

            def track_ray_node(node_id: str, result: Any) -> None:
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
                    if hooks and hooks.cancel_requested and hooks.cancel_requested():
                        cancelled = True
                        break
                    if not dependencies_ready(node_id):
                        continue

                    _node_ref, result = submit_node(node_id)
                    result_refs[node_id] = result
                    track_ray_node(node_id, result)
                    remaining_nodes.remove(node_id)
                    submitted_any = True
                    drain_ray_completions(block=False)

                if cancelled:
                    break
                if submitted_any:
                    continue
                if pending_refs:
                    drain_ray_completions(block=True)
                    if hooks and hooks.cancel_requested:
                        hooks.cancel_requested()
                    continue
                break

            while pending_refs:
                drain_ray_completions(block=True)
                if hooks and hooks.cancel_requested:
                    hooks.cancel_requested()
        else:
            for node_id in execution_order:
                # Check cancellation before starting each node
                if hooks and hooks.cancel_requested and hooks.cancel_requested():
                    break

                node_ref, result = submit_node(node_id)
                # For non-Ray executors, resolve refs immediately so
                # on_node_success fires after actual completion (not just
                # submission). This serializes execution but gives accurate
                # per-node progress — the right tradeoff for the operator.
                if hooks and (hooks.on_node_success or hooks.unwrap_result):
                    # Wait for completion and resolve to actual value.
                    # For multi-return nodes (num_returns > 1), result is
                    # a tuple of refs; for single-return it's one ref/value.
                    resolved_val = resolve_submitted_result(node_ref, result)
                    # Unwrap side-channel data (e.g. logs from Ray workers)
                    if hooks.unwrap_result:
                        result = hooks.unwrap_result(node_id, resolved_val)
                    if hooks.on_node_success:
                        hooks.on_node_success(node_id)
                result_refs[node_id] = result

        # If workflow doesn't return anything, wait for all nodes to complete.
        # Skip if hooks were active — we already resolved per-node above.
        if self.returns is None:
            if not (hooks and (hooks.on_node_success or hooks.unwrap_result)):
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
            return None

        # Fetch only what the workflow explicitly returns
        # Handle different return types
        if isinstance(self.returns, NodeFuture):
            # Single NodeFuture returned
            return _fetch_node_result(self.returns, result_refs, self.nodes, executor)
        elif isinstance(self.returns, tuple):
            # Tuple of NodeFutures returned
            return tuple(
                _fetch_node_result(nf, result_refs, self.nodes, executor)
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

    Args:
        cron: Optional cron expression for scheduled execution.

    Returns:
        Function that returns a Workflow object when called
    """

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
                    name=fn.__name__,
                    returns=result,
                    cron=cron,
                )
            finally:
                _workflow_context.reset(token)

        return wrapper

    # Support both @workflow and @workflow(cron="...")
    if fn is not None:
        return decorator(fn)
    return decorator


pipeline = workflow
