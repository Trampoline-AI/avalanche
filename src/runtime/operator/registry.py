"""Current-only workflow catalog with isolated source discovery."""

from __future__ import annotations

import threading
from collections import defaultdict
from types import MappingProxyType
from typing import Callable

from avalanche.dag import Workflow

from .discovery import ConfiguredRoot, configure_roots, discover, load_builder
from .models import (
    CatalogView,
    WorkflowDescriptor,
    WorkflowDiscoveryDiagnostic,
    WorkflowInfo,
    display_name_from_id,
)


class UnknownWorkflow(KeyError):  # noqa: N818 - domain exception name is intentional
    pass


class AmbiguousWorkflow(KeyError):  # noqa: N818 - domain exception name is intentional
    def __init__(self, selector: str, candidate_ids: tuple[str, ...]) -> None:
        self.selector = selector
        self.candidate_ids = candidate_ids
        super().__init__(
            f"Ambiguous workflow {selector!r}; candidates: {', '.join(candidate_ids)}"
        )


def workflow_to_info(
    workflow: Workflow,
    file_path: str,
    *,
    workflow_id: str = "",
    builder_symbol: str = "",
    root_alias: str = "",
) -> WorkflowInfo:
    """Convert a Workflow object to the public flat compatibility model."""
    node_ids = workflow._topological_sort()
    node_types = {nid: workflow.nodes[nid].node.node_type.value for nid in node_ids}
    display_names = {nid: display_name_from_id(nid) for nid in node_ids}
    node_slugs = {nid: workflow.node_slugs[nid] for nid in node_ids}
    return WorkflowInfo(
        name=workflow.name,
        display_name=workflow.name,
        workflow_id=workflow_id,
        builder_symbol=builder_symbol,
        root_alias=root_alias,
        relative_file=file_path,
        file_path=file_path,
        node_ids=node_ids,
        graph=dict(workflow.graph),
        node_types=node_types,
        display_names=display_names,
        node_slugs=node_slugs,
        cron=workflow.cron,
    )


def descriptor_to_info(descriptor: WorkflowDescriptor) -> WorkflowInfo:
    """Build a mutable public projection without mutating catalog metadata."""
    return WorkflowInfo(
        name=descriptor.display_name,
        display_name=descriptor.display_name,
        workflow_id=descriptor.workflow_id,
        builder_symbol=descriptor.locator.builder_symbol,
        root_alias=descriptor.locator.root_alias,
        relative_file=descriptor.locator.relative_file,
        file_path=descriptor.locator.relative_file,
        node_ids=list(descriptor.node_ids),
        graph={key: list(value) for key, value in descriptor.graph},
        node_types=dict(descriptor.node_types),
        display_names=dict(descriptor.display_names),
        node_slugs=dict(descriptor.node_slugs),
        cron=descriptor.cron,
    )


class WorkflowRegistry:
    """Atomic descriptor catalog plus a separate manual compatibility registry."""

    def __init__(self, *, discovery_timeout: float = 15.0) -> None:
        self._lock = threading.Lock()
        self._view = CatalogView()
        self._scan_paths: tuple[str, ...] = ()
        self._roots: tuple[ConfiguredRoot, ...] = ()
        self._manual: dict[str, tuple[Callable[[], Workflow], WorkflowInfo]] = {}
        self._discovery_timeout = discovery_timeout

    @property
    def view(self) -> CatalogView:
        with self._lock:
            return self._view

    @property
    def configured_roots(self) -> tuple[ConfiguredRoot, ...]:
        """Return the normalized workflow roots used by the current catalog."""
        with self._lock:
            return self._roots

    def scan(self, paths: list[str]) -> CatalogView:
        """Build a complete catalog off-lock, then atomically install it."""
        roots = configure_roots(paths)
        descriptors, diagnostics = discover(roots, timeout=self._discovery_timeout)

        by_id: dict[str, WorkflowDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.workflow_id in by_id:
                raise ValueError(
                    f"Duplicate canonical workflow ID: {descriptor.workflow_id}"
                )
            by_id[descriptor.workflow_id] = descriptor
        short_names: defaultdict[str, list[str]] = defaultdict(list)
        for descriptor in descriptors:
            short_names[descriptor.display_name].append(descriptor.workflow_id)
            short_names[descriptor.locator.builder_symbol].append(descriptor.workflow_id)
        frozen_short_names = {
            name: tuple(sorted(set(candidate_ids)))
            for name, candidate_ids in short_names.items()
        }
        view = CatalogView(
            by_id=MappingProxyType(dict(sorted(by_id.items()))),
            short_names=MappingProxyType(dict(sorted(frozen_short_names.items()))),
            diagnostics=diagnostics,
        )
        with self._lock:
            self._scan_paths = tuple(paths)
            self._roots = roots
            self._view = view
        return view

    def rescan(self) -> CatalogView:
        """Refresh configured roots without retaining a last-good descriptor."""
        with self._lock:
            paths = list(self._scan_paths)
        if not paths:
            return self.view
        return self.scan(paths)

    def resolve(self, selector: str) -> WorkflowDescriptor:
        descriptor, _ = self.resolve_source(selector)
        return descriptor

    def resolve_source(self, selector: str) -> tuple[WorkflowDescriptor, ConfiguredRoot]:
        """Resolve a descriptor and its source root from one catalog lock."""
        with self._lock:
            view = self._view
            exact = view.by_id.get(selector)
            candidates = view.short_names.get(selector, ()) if exact is None else ()
            if exact is None and len(candidates) == 1:
                exact = view.by_id[candidates[0]]
            if exact is None and len(candidates) > 1:
                raise AmbiguousWorkflow(selector, tuple(sorted(candidates)))
            if exact is None:
                raise UnknownWorkflow(f"Unknown workflow: {selector}")
            root = next(
                (item for item in self._roots if item.alias == exact.locator.root_alias),
                None,
            )
            if root is None:
                raise UnknownWorkflow(f"Unknown workflow root: {exact.locator.root_alias}")
            return exact, root

    def descriptors(self) -> tuple[WorkflowDescriptor, ...]:
        return tuple(self.view.by_id.values())

    def register(self, builder: Callable[[], Workflow], file_path: str = "<manual>") -> None:
        """Register a retained builder only in the explicit static compatibility path."""
        workflow = builder()
        info = workflow_to_info(workflow, file_path, workflow_id=workflow.name)
        with self._lock:
            self._manual[workflow.name] = (builder, info)

    def list_workflows(self) -> list[WorkflowInfo]:
        scanned = [descriptor_to_info(item) for item in self.descriptors()]
        with self._lock:
            manual = [entry[1] for entry in self._manual.values()]
        return scanned + manual

    def list_diagnostics(self) -> list[WorkflowDiscoveryDiagnostic]:
        return list(self.view.diagnostics)

    def get_builder(self, selector: str) -> Callable[[], Workflow]:
        """Fresh-load scanned source for the static compatibility path."""
        with self._lock:
            manual = self._manual.get(selector)
        if manual is not None:
            return manual[0]

        descriptor = self.resolve(selector)
        with self._lock:
            roots = {root.alias: root for root in self._roots}
        root = roots.get(descriptor.locator.root_alias)
        if root is None:
            raise UnknownWorkflow(f"Unknown workflow root: {descriptor.locator.root_alias}")
        return load_builder(root, descriptor.locator)
