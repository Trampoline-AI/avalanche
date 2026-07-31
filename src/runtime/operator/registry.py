"""Current-only workflow catalog with isolated source discovery."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import replace
from types import MappingProxyType
from typing import Callable

from avalanche.dag import Workflow

from .discovery import ConfiguredRoot, configure_roots, discover, load_builder
from .models import (
    CatalogView,
    ScanTargetInfo,
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


def agent_metadata_for_workflow(workflow: Workflow, node_ids: list[str]) -> dict[str, str]:
    """Serialize stable agent declaration metadata for catalog and run projections."""
    metadata_by_node: dict[str, str] = {}
    for node_id in node_ids:
        spec = getattr(workflow.nodes[node_id].node.fn, "__agent_step__", None)
        if spec is None:
            continue
        try:
            metadata = spec.declaration_metadata(workflow.agent_defaults)
            metadata_by_node[node_id] = json.dumps(
                metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        except Exception as exc:
            metadata_by_node[node_id] = json.dumps(
                {"error": str(exc) or type(exc).__name__},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
    return metadata_by_node


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
    agent_metadata_json = agent_metadata_for_workflow(workflow, node_ids)
    agent_node_ids = list(agent_metadata_json)
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
        agent_node_ids=agent_node_ids,
        agent_metadata_json=agent_metadata_json,
        cron=workflow.cron,
        webhook_path=workflow.webhook.path if workflow.webhook else None,
        webhook_enabled=workflow.webhook is not None,
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
        agent_node_ids=list(descriptor.agent_node_ids),
        agent_metadata_json=dict(descriptor.agent_metadata_json),
        cron=descriptor.cron,
        webhook_path=descriptor.webhook_path,
        webhook_enabled=descriptor.webhook_enabled,
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

    def scan(
        self,
        paths: list[str],
        *,
        validate: Callable[[tuple[WorkflowDescriptor, ...]], object] | None = None,
    ) -> CatalogView:
        """Configure roots and atomically install one complete valid catalog."""
        roots = configure_roots(paths)
        with self._lock:
            self._scan_paths = tuple(paths)
            self._roots = roots
        return self._scan_roots(roots, validate=validate)

    def rescan(
        self,
        *,
        validate: Callable[[tuple[WorkflowDescriptor, ...]], object] | None = None,
    ) -> CatalogView:
        """Refresh configured roots while preserving the last valid catalog."""
        with self._lock:
            roots = self._roots
        if not roots:
            return self.view
        return self._scan_roots(roots, validate=validate)

    def _scan_roots(
        self,
        roots: tuple[ConfiguredRoot, ...],
        *,
        validate: Callable[[tuple[WorkflowDescriptor, ...]], object] | None,
    ) -> CatalogView:
        descriptors, diagnostics = discover(roots, timeout=self._discovery_timeout)
        candidate_diagnostics = list(diagnostics)
        try:
            if validate is not None:
                validate(descriptors)
        except ValueError as exc:
            candidate_diagnostics.append(
                WorkflowDiscoveryDiagnostic(
                    path=", ".join(str(root.target) for root in roots),
                    kind="invalid_catalog",
                    message=str(exc),
                )
            )

        by_id: dict[str, WorkflowDescriptor] = {}
        for descriptor in descriptors:
            if descriptor.workflow_id in by_id:
                candidate_diagnostics.append(
                    WorkflowDiscoveryDiagnostic(
                        path=descriptor.locator.relative_file,
                        kind="invalid_catalog",
                        message=f"Duplicate canonical workflow ID: {descriptor.workflow_id}",
                    )
                )
                break
            by_id[descriptor.workflow_id] = descriptor

        scan_targets = tuple(
            ScanTargetInfo(
                alias=root.alias,
                target_path=str(root.target),
                kind="directory" if root.target == root.path else "file",
            )
            for root in roots
        )
        diagnostics_tuple = tuple(candidate_diagnostics)
        candidate_failed = any(item.kind != "skipped" for item in diagnostics_tuple)
        if candidate_failed:
            with self._lock:
                self._view = replace(
                    self._view,
                    scan_targets=scan_targets,
                    diagnostics=diagnostics_tuple,
                )
                return self._view

        by_id = dict(sorted(by_id.items()))
        short_names: defaultdict[str, list[str]] = defaultdict(list)
        for descriptor in descriptors:
            short_names[descriptor.display_name].append(descriptor.workflow_id)
            short_names[descriptor.locator.builder_symbol].append(descriptor.workflow_id)
        frozen_short_names = {
            name: tuple(sorted(set(candidate_ids)))
            for name, candidate_ids in short_names.items()
        }
        with self._lock:
            view = CatalogView(
                revision=self._view.revision + 1,
                by_id=MappingProxyType(by_id),
                short_names=MappingProxyType(dict(sorted(frozen_short_names.items()))),
                scan_targets=scan_targets,
                diagnostics=diagnostics_tuple,
            )
            self._view = view
            return view

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

    def list_workflows(self, view: CatalogView | None = None) -> list[WorkflowInfo]:
        catalog = view if view is not None else self.view
        scanned = [descriptor_to_info(item) for item in catalog.by_id.values()]
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
