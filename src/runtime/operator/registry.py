"""Current-only workflow catalog with isolated source discovery."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Callable

from avalanche.dag import Workflow

from .discovery import (
    DEFAULT_DISCOVERY_TIMEOUT,
    ConfiguredRoot,
    FileDiscoveryResult,
    candidate_files,
    configure_roots,
    discover_files,
    load_builder,
    validate_discovery_timeout,
)
from .discovery_cache import DiscoveryCache
from .models import (
    CatalogView,
    ScanTargetInfo,
    WorkflowDescriptor,
    WorkflowDiscoveryDiagnostic,
    WorkflowInfo,
    display_name_from_id,
)
from .source import resolve_watch_roots
from .workflow_metadata import standard_step_docstring_lines_for_workflow

logger = logging.getLogger(__name__)


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
    """Serialize stable agent declaration metadata for current-catalog projections."""
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


def agent_field_schemas_for_workflow(workflow: Workflow, node_ids: list[str]) -> dict[str, str]:
    """Serialize only agent invocation field schemas for immutable run topology."""
    schemas_by_node: dict[str, str] = {}
    for node_id in node_ids:
        spec = getattr(workflow.nodes[node_id].node.fn, "__agent_step__", None)
        if spec is None:
            continue
        schemas_by_node[node_id] = json.dumps(
            spec.field_schema_metadata(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return schemas_by_node


def agent_instruction_lines_for_workflow(
    workflow: Workflow, node_ids: list[str]
) -> dict[str, str]:
    """Serialize stable agent signature instruction summaries for a run topology."""
    lines_by_node: dict[str, str] = {}
    for node_id in node_ids:
        spec = getattr(workflow.nodes[node_id].node.fn, "__agent_step__", None)
        if spec is None:
            continue
        instruction_line = spec.signature_instruction_line()
        if instruction_line:
            lines_by_node[node_id] = instruction_line
    return lines_by_node


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
    standard_step_docstring_lines = standard_step_docstring_lines_for_workflow(
        workflow, node_ids
    )
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
        standard_step_docstring_lines=standard_step_docstring_lines,
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
        standard_step_docstring_lines=dict(descriptor.standard_step_docstring_lines),
        cron=descriptor.cron,
        webhook_path=descriptor.webhook_path,
        webhook_enabled=descriptor.webhook_enabled,
    )


class WorkflowRegistry:
    """Atomic descriptor catalog plus a separate manual compatibility registry."""

    def __init__(
        self,
        *,
        discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
        cache_dir: Path | None = None,
    ) -> None:
        validate_discovery_timeout(discovery_timeout)
        self._lock = threading.Lock()
        self._view = CatalogView()
        self._scan_paths: tuple[str, ...] = ()
        self._roots: tuple[ConfiguredRoot, ...] = ()
        self._manual: dict[str, tuple[Callable[[], Workflow], WorkflowInfo]] = {}
        self._discovery_timeout = discovery_timeout
        self._cache_dir = cache_dir
        self._cache: DiscoveryCache | None = None
        self._discovery_files: dict[tuple[str, Path], FileDiscoveryResult] = {}
        self._file_rollback: (
            tuple[CatalogView, dict[tuple[str, Path], FileDiscoveryResult]] | None
        ) = None

    @property
    def view(self) -> CatalogView:
        with self._lock:
            return self._view

    def restore_view(self, rejected: CatalogView, previous: CatalogView) -> None:
        """Restore the last valid view after downstream reconciliation rejects a candidate."""
        with self._lock:
            if self._view is not rejected:
                raise RuntimeError("Workflow catalog changed before candidate rollback")
            self._view = previous
            if self._file_rollback is not None and self._file_rollback[0] is rejected:
                self._discovery_files = self._file_rollback[1]
            self._file_rollback = None
            files = tuple(self._discovery_files.values())
            roots = self._roots
            cache = self._cache
        if cache is not None:
            self._store_cache(cache, roots, files)

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
        started = time.perf_counter()
        roots = configure_roots(paths)
        cache = DiscoveryCache(roots, directory=self._cache_dir)
        logger.info("Workflow discovery cache selected: path=%s", cache.path)
        with self._lock:
            self._scan_paths = tuple(paths)
            self._roots = roots
            self._cache = cache
        cached_files = cache.load()
        if cached_files is not None:
            view, accepted = self._install_files(roots, cached_files, (), validate=validate)
            if accepted:
                logger.info("Workflow discovery cache hit: path=%s", cache.path)
                self._commit_files(view, cached_files)
                self._log_scan_completed("cache", started, (), ())
                return view
        logger.info("Workflow discovery cache miss: path=%s", cache.path)
        return self._scan_roots(roots, validate=validate)

    def rescan(
        self,
        changed_files: tuple[str, ...] = (),
        *,
        validate: Callable[[tuple[WorkflowDescriptor, ...]], object] | None = None,
    ) -> CatalogView:
        """Refresh affected configured sources while preserving the last valid catalog."""
        with self._lock:
            roots = self._roots
        if not roots:
            return self.view
        if not changed_files:
            return self._scan_roots(roots, validate=validate)
        if any(Path(path).suffix != ".py" for path in changed_files):
            return self._scan_roots(roots, validate=validate)
        return self._rescan_changed(roots, changed_files, validate=validate)

    def _rescan_changed(
        self,
        roots: tuple[ConfiguredRoot, ...],
        changed_files: tuple[str, ...],
        *,
        validate: Callable[[tuple[WorkflowDescriptor, ...]], object] | None,
    ) -> CatalogView:
        started = time.perf_counter()
        changed_paths = {Path(path).resolve() for path in changed_files}
        current_candidates = set(candidate_files(roots))
        with self._lock:
            known_files = dict(self._discovery_files)
            cache = self._cache
        known_keys = set(known_files)
        added = current_candidates - known_keys
        removed = known_keys - current_candidates
        affected = {
            key
            for key in current_candidates
            if key[1] in changed_paths
            or (
                key in known_files
                and any(
                    path.resolve() in changed_paths for path in known_files[key].dependencies
                )
            )
        }
        affected.update(added)

        target_keys = tuple(sorted(affected, key=lambda item: (item[0], str(item[1]))))
        target_paths = tuple(str(path) for _, path in target_keys)
        logger.info(
            "Workflow discovery scan started: mode=targeted files=%s",
            target_paths,
        )
        previous_workflow_ids = {
            descriptor.workflow_id
            for key in target_keys
            if key in known_files
            for descriptor in known_files[key].descriptors
        }
        if not affected and not removed:
            if cache is not None:
                self._store_cache(cache, roots, tuple(known_files.values()))
            self._log_scan_completed("targeted", started, target_paths, ())
            return self.view

        if affected:
            discovered, diagnostics = discover_files(
                roots,
                targets=target_keys,
                timeout=self._discovery_timeout,
            )
        else:
            discovered, diagnostics = (), ()
        discovered_by_key = {
            (item.root_alias, item.source_path.resolve()): item for item in discovered
        }
        missing = affected - set(discovered_by_key)
        if missing:
            diagnostics += tuple(
                WorkflowDiscoveryDiagnostic(
                    path=str(path),
                    kind="import_error",
                    message="Targeted discovery did not return this candidate file.",
                )
                for _, path in sorted(missing, key=lambda item: (item[0], str(item[1])))
            )
        merged = {
            key: result
            for key, result in known_files.items()
            if key not in affected and key not in removed
        }
        merged.update(discovered_by_key)
        files = tuple(
            merged[key] for key in sorted(merged, key=lambda item: (item[0], str(item[1])))
        )
        view, accepted = self._install_files(roots, files, diagnostics, validate=validate)
        if accepted:
            self._commit_files(view, files)
        rescanned_workflow_ids = previous_workflow_ids | {
            descriptor.workflow_id
            for file_result in discovered
            for descriptor in file_result.descriptors
        }
        self._log_scan_completed(
            "targeted",
            started,
            target_paths,
            tuple(sorted(rescanned_workflow_ids)),
        )
        return view

    def _scan_roots(
        self,
        roots: tuple[ConfiguredRoot, ...],
        *,
        validate: Callable[[tuple[WorkflowDescriptor, ...]], object] | None,
    ) -> CatalogView:
        started = time.perf_counter()
        targets = candidate_files(roots)
        target_paths = tuple(str(path) for _, path in targets)
        logger.info(
            "Workflow discovery scan started: mode=full files=%s",
            target_paths,
        )
        with self._lock:
            previous_workflow_ids = {
                descriptor.workflow_id
                for file_result in self._discovery_files.values()
                for descriptor in file_result.descriptors
            }
        files, diagnostics = discover_files(roots, timeout=self._discovery_timeout)
        view, accepted = self._install_files(roots, files, diagnostics, validate=validate)
        if accepted:
            self._commit_files(view, files)
        rescanned_workflow_ids = previous_workflow_ids | {
            descriptor.workflow_id
            for file_result in files
            for descriptor in file_result.descriptors
        }
        self._log_scan_completed(
            "full",
            started,
            target_paths,
            tuple(sorted(rescanned_workflow_ids)),
        )
        return view

    @staticmethod
    def _log_scan_completed(
        mode: str,
        started: float,
        rescanned_files: tuple[str, ...],
        rescanned_workflows: tuple[str, ...],
    ) -> None:
        logger.info(
            "Workflow discovery scan completed: mode=%s duration_seconds=%.3f "
            "rescanned_files=%s rescanned_workflows=%s",
            mode,
            time.perf_counter() - started,
            rescanned_files,
            rescanned_workflows,
        )

    def _install_files(
        self,
        roots: tuple[ConfiguredRoot, ...],
        files: tuple[FileDiscoveryResult, ...],
        diagnostics: tuple[WorkflowDiscoveryDiagnostic, ...],
        *,
        validate: Callable[[tuple[WorkflowDescriptor, ...]], object] | None,
    ) -> tuple[CatalogView, bool]:
        descriptors = tuple(
            descriptor for file_result in files for descriptor in file_result.descriptors
        )
        candidate_diagnostics = [
            diagnostic for file_result in files for diagnostic in file_result.diagnostics
        ]
        candidate_diagnostics.extend(diagnostics)
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
                current = self._view
                if (
                    current.scan_targets == scan_targets
                    and current.diagnostics == diagnostics_tuple
                ):
                    return current, False
                self._view = replace(
                    current,
                    scan_targets=scan_targets,
                    diagnostics=diagnostics_tuple,
                )
                return self._view, False

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
            current = self._view
            if (
                current.by_id == by_id
                and current.short_names == frozen_short_names
                and current.scan_targets == scan_targets
                and current.diagnostics == diagnostics_tuple
            ):
                return current, True
            view = CatalogView(
                revision=current.revision + 1,
                by_id=MappingProxyType(by_id),
                short_names=MappingProxyType(dict(sorted(frozen_short_names.items()))),
                scan_targets=scan_targets,
                diagnostics=diagnostics_tuple,
            )
            self._view = view
            return view, True

    def _commit_files(
        self,
        view: CatalogView,
        files: tuple[FileDiscoveryResult, ...],
    ) -> None:
        file_map = {(item.root_alias, item.source_path.resolve()): item for item in files}
        with self._lock:
            previous_files = self._discovery_files
            if view is not self._view:
                raise RuntimeError("Workflow catalog changed before discovery files committed")
            self._discovery_files = file_map
            self._file_rollback = (view, previous_files)
            cache = self._cache
            roots = self._roots
        if cache is not None:
            self._store_cache(cache, roots, files)

    @staticmethod
    def _store_cache(
        cache: DiscoveryCache,
        roots: tuple[ConfiguredRoot, ...],
        files: tuple[FileDiscoveryResult, ...],
    ) -> None:
        locators = tuple(
            descriptor.locator
            for file_result in files
            for descriptor in file_result.descriptors
        )
        cache.store(files, resolve_watch_roots(roots, locators))

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
