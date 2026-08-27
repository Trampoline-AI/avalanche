"""Persistent, source-validated workflow discovery metadata."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from avalanche import __version__ as avalanche_version

from .discovery import ConfiguredRoot, FileDiscoveryResult
from .models import WorkflowDescriptor, WorkflowDiscoveryDiagnostic, WorkflowLocator
from .source import iter_source_paths

logger = logging.getLogger(__name__)

_CACHE_SCHEMA_VERSION = 3


class _CacheModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _CachedRoot(_CacheModel):
    alias: str
    path: str
    target: str

    @classmethod
    def from_domain(cls, root: ConfiguredRoot) -> _CachedRoot:
        return cls(
            alias=root.alias,
            path=str(root.path.resolve()),
            target=str(root.target.resolve()),
        )


class _CachedLocator(_CacheModel):
    root_alias: str
    relative_file: str
    builder_symbol: str

    @classmethod
    def from_domain(cls, locator: WorkflowLocator) -> _CachedLocator:
        return cls(
            root_alias=locator.root_alias,
            relative_file=locator.relative_file,
            builder_symbol=locator.builder_symbol,
        )

    def to_domain(self) -> WorkflowLocator:
        return WorkflowLocator(
            root_alias=self.root_alias,
            relative_file=self.relative_file,
            builder_symbol=self.builder_symbol,
        )


class _CachedDescriptor(_CacheModel):
    workflow_id: str
    display_name: str
    locator: _CachedLocator
    node_ids: tuple[str, ...]
    graph: tuple[tuple[str, tuple[str, ...]], ...]
    node_types: tuple[tuple[str, str], ...]
    display_names: tuple[tuple[str, str], ...]
    agent_node_ids: tuple[str, ...]
    agent_metadata_json: tuple[tuple[str, str], ...]
    standard_step_docstring_lines: tuple[tuple[str, str], ...]
    node_source_code: tuple[tuple[str, str], ...]
    cron: str | None
    webhook_path: str | None
    webhook_enabled: bool

    @classmethod
    def from_domain(cls, descriptor: WorkflowDescriptor) -> _CachedDescriptor:
        return cls(
            workflow_id=descriptor.workflow_id,
            display_name=descriptor.display_name,
            locator=_CachedLocator.from_domain(descriptor.locator),
            node_ids=descriptor.node_ids,
            graph=descriptor.graph,
            node_types=descriptor.node_types,
            display_names=descriptor.display_names,
            agent_node_ids=descriptor.agent_node_ids,
            agent_metadata_json=descriptor.agent_metadata_json,
            standard_step_docstring_lines=descriptor.standard_step_docstring_lines,
            node_source_code=descriptor.node_source_code,
            cron=descriptor.cron,
            webhook_path=descriptor.webhook_path,
            webhook_enabled=descriptor.webhook_enabled,
        )

    def to_domain(self) -> WorkflowDescriptor:
        return WorkflowDescriptor(
            workflow_id=self.workflow_id,
            display_name=self.display_name,
            locator=self.locator.to_domain(),
            node_ids=self.node_ids,
            graph=self.graph,
            node_types=self.node_types,
            display_names=self.display_names,
            agent_node_ids=self.agent_node_ids,
            agent_metadata_json=self.agent_metadata_json,
            standard_step_docstring_lines=self.standard_step_docstring_lines,
            node_source_code=self.node_source_code,
            cron=self.cron,
            webhook_path=self.webhook_path,
            webhook_enabled=self.webhook_enabled,
        )


class _CachedDiagnostic(_CacheModel):
    path: str
    kind: Literal[
        "skipped", "import_error", "build_error", "invalid_schedule", "invalid_catalog"
    ]
    message: str

    @classmethod
    def from_domain(cls, diagnostic: WorkflowDiscoveryDiagnostic) -> _CachedDiagnostic:
        return cls(path=diagnostic.path, kind=diagnostic.kind, message=diagnostic.message)

    def to_domain(self) -> WorkflowDiscoveryDiagnostic:
        return WorkflowDiscoveryDiagnostic(path=self.path, kind=self.kind, message=self.message)


class _CachedFileResult(_CacheModel):
    root_alias: str
    source_path: str
    descriptors: tuple[_CachedDescriptor, ...]
    diagnostics: tuple[_CachedDiagnostic, ...]
    dependencies: tuple[str, ...]

    @classmethod
    def from_domain(cls, result: FileDiscoveryResult) -> _CachedFileResult:
        return cls(
            root_alias=result.root_alias,
            source_path=str(result.source_path.resolve()),
            descriptors=tuple(
                _CachedDescriptor.from_domain(item) for item in result.descriptors
            ),
            diagnostics=tuple(
                _CachedDiagnostic.from_domain(item) for item in result.diagnostics
            ),
            dependencies=tuple(str(path.resolve()) for path in result.dependencies),
        )

    def to_domain(self) -> FileDiscoveryResult:
        return FileDiscoveryResult(
            root_alias=self.root_alias,
            source_path=Path(self.source_path),
            descriptors=tuple(item.to_domain() for item in self.descriptors),
            diagnostics=tuple(item.to_domain() for item in self.diagnostics),
            dependencies=tuple(Path(path) for path in self.dependencies),
        )


class _SourceStamp(_CacheModel):
    path: str
    modified_ns: int
    size: int


class _CacheDocument(_CacheModel):
    schema_version: Literal[3]
    environment: str
    roots: tuple[_CachedRoot, ...]
    watch_roots: tuple[str, ...]
    source_stamps: tuple[_SourceStamp, ...]
    files: tuple[_CachedFileResult, ...]


class DiscoveryCache:
    """Read and atomically update one configured-root discovery cache."""

    def __init__(
        self,
        roots: tuple[ConfiguredRoot, ...],
        *,
        directory: Path | None = None,
    ) -> None:
        self._roots = tuple(_CachedRoot.from_domain(root) for root in roots)
        cache_directory = directory or Path.cwd() / ".avalanche" / "cache" / "operator"
        root_identity = json.dumps(
            [root.model_dump(mode="json") for root in self._roots],
            separators=(",", ":"),
            sort_keys=True,
        )
        cache_name = hashlib.sha256(root_identity.encode()).hexdigest()[:20]
        self.path = cache_directory / f"{cache_name}.json"
        self._environment = _environment_fingerprint()

    def load(self) -> tuple[FileDiscoveryResult, ...] | None:
        try:
            document = _CacheDocument.model_validate_json(self.path.read_text())
            if document.schema_version != _CACHE_SCHEMA_VERSION:
                return None
            if document.environment != self._environment or document.roots != self._roots:
                return None
            watch_roots = tuple(Path(path) for path in document.watch_roots)
            if (
                _source_stamps(watch_roots, excluded_root=self.path.parent)
                != document.source_stamps
            ):
                return None
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, ValidationError) as exc:
            logger.warning(
                "Ignoring unreadable workflow discovery cache %s: %s", self.path, exc
            )
            return None
        return tuple(item.to_domain() for item in document.files)

    def store(
        self,
        files: tuple[FileDiscoveryResult, ...],
        watch_roots: tuple[Path, ...],
    ) -> None:
        document = _CacheDocument(
            schema_version=_CACHE_SCHEMA_VERSION,
            environment=self._environment,
            roots=self._roots,
            watch_roots=tuple(str(path.resolve()) for path in watch_roots),
            source_stamps=_source_stamps(
                watch_roots,
                excluded_root=self.path.parent,
            ),
            files=tuple(_CachedFileResult.from_domain(item) for item in files),
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(document.model_dump_json())
            temporary.replace(self.path)
        except OSError as exc:
            logger.warning("Could not update workflow discovery cache %s: %s", self.path, exc)


def _source_stamps(
    source_roots: tuple[Path, ...],
    *,
    excluded_root: Path,
) -> tuple[_SourceStamp, ...]:
    stamps: list[_SourceStamp] = []
    for path in iter_source_paths(source_roots):
        if path.resolve().is_relative_to(excluded_root.resolve()):
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        stamps.append(
            _SourceStamp(
                path=str(path.resolve()),
                modified_ns=stat.st_mtime_ns,
                size=stat.st_size,
            )
        )
    return tuple(stamps)


def _environment_fingerprint() -> str:
    distributions: list[tuple[str, str]] = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if isinstance(name, str):
            distributions.append((name.lower(), distribution.version))
    identity = {
        "avalanche": avalanche_version,
        "distributions": sorted(distributions),
        "executable": str(Path(sys.executable).resolve()),
        "python": sys.version,
    }
    encoded = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()
