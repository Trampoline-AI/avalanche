"""Durable, run-lineaged artifact storage contracts and local backend."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import sqlite3
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, BinaryIO, Literal
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from pydantic import BaseModel, ConfigDict, field_validator

ArtifactKind = Literal["input", "output"]
_CHUNK_SIZE = 1024 * 1024


class ArtifactRef(BaseModel):
    """Durable artifact metadata passed across workflow and executor boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    uri: str
    checksum: str | None
    size: int | None
    media_type: str | None
    kind: ArtifactKind
    run_id: str
    node_id: str | None
    role: str
    origin: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        invalid = (
            not value
            or value in {".", ".."}
            or any(char in value for char in ("/", "\\", "\0"))
        )
        if invalid:
            raise ValueError("artifact name must be a non-empty file name")
        return value

    @field_validator("uri", "run_id", "role", "origin")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("artifact metadata values must be non-empty")
        return value

    @field_validator("checksum")
    @classmethod
    def _validate_checksum(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("artifact checksum must be a 64-character SHA-256 digest")
        return normalized

    @field_validator("size")
    @classmethod
    def _validate_size(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("artifact size must be non-negative")
        return value

    def open(self, mode: str = "rb", **kwargs: Any) -> BinaryIO:
        """Open the durable object through its URI."""
        parsed = urlparse(self.uri)
        if parsed.scheme == "file":
            path = Path(url2pathname(unquote(parsed.path)))
            return path.open(mode, **kwargs)
        try:
            import fsspec
        except ModuleNotFoundError as exc:  # pragma: no cover - core dependency today
            raise ModuleNotFoundError(
                f"Artifact URI access for {parsed.scheme!r} requires fsspec",
                name="fsspec",
            ) from exc
        return fsspec.open(self.uri, mode=mode, **kwargs).open()

    def read_bytes(self, **kwargs: Any) -> bytes:
        """Read and validate the complete artifact payload."""
        with self.open("rb", **kwargs) as file:
            content = file.read()
        if self.size is not None and len(content) != self.size:
            raise ValueError("artifact size does not match metadata")
        if self.checksum is not None and hashlib.sha256(content).hexdigest() != self.checksum:
            raise ValueError("artifact checksum does not match content")
        return content


class ArtifactManifest(BaseModel):
    """The complete input/output artifact lineage for one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    artifacts: tuple[ArtifactRef, ...] = ()

    @property
    def inputs(self) -> tuple[ArtifactRef, ...]:
        return tuple(ref for ref in self.artifacts if ref.kind == "input")

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return tuple(ref for ref in self.artifacts if ref.kind == "output")


class DuplicateArtifactError(FileExistsError):
    """Raised when a run already contains an artifact with the same kind and name."""


class ArtifactStore(ABC):
    """Backend contract for durable staging, registration, and publication."""

    @abstractmethod
    def stage_input(
        self,
        source: Any,
        *,
        run_id: str,
        role: str,
        name: str | None = None,
        media_type: str | None = None,
    ) -> ArtifactRef:
        """Copy a submitted input into durable storage."""

    @abstractmethod
    def register(
        self,
        uri: str,
        *,
        name: str,
        checksum: str | None,
        size: int | None,
        media_type: str | None,
        kind: ArtifactKind,
        run_id: str,
        node_id: str | None,
        role: str,
        origin: str | None = None,
    ) -> ArtifactRef:
        """Register an existing durable object without copying it."""

    @abstractmethod
    def publish(
        self,
        source: str | Path,
        *,
        run_id: str,
        node_id: str,
        role: str,
        name: str | None = None,
        media_type: str | None = None,
    ) -> ArtifactRef:
        """Atomically publish a generated local file."""

    @abstractmethod
    def manifest(self, run_id: str) -> ArtifactManifest:
        """Return the single manifest containing a run's inputs and outputs."""


class LocalArtifactStore(ArtifactStore):
    """Content-addressed local filesystem backend with an atomic SQLite manifest.

    Artifact names are unique per ``(run_id, kind)``. Publishing or staging a
    duplicate raises :class:`DuplicateArtifactError`; existing content and
    manifest rows are never overwritten.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def stage_input(
        self,
        source: Any,
        *,
        run_id: str,
        role: str,
        name: str | None = None,
        media_type: str | None = None,
    ) -> ArtifactRef:
        from .runtime import File

        if isinstance(source, File):
            artifact_name = name or source.name or role.rsplit(".", 1)[-1]
            resolved_media_type = (
                media_type or source.content_type or _guess_media_type(artifact_name)
            )
            ref = self._store_bytes(
                source.content,
                name=artifact_name,
                run_id=run_id,
                role=role,
                media_type=resolved_media_type,
                kind="input",
                node_id=None,
                origin=f"upload://{artifact_name}",
            )
            if source.sha256 is not None and ref.checksum != source.sha256:
                raise ValueError("staged input checksum does not match File metadata")
            return ref

        path = Path(source)
        artifact_name = name or path.name
        return self._store_path(
            path,
            name=artifact_name,
            run_id=run_id,
            role=role,
            media_type=media_type or _guess_media_type(artifact_name),
            kind="input",
            node_id=None,
            origin=path.resolve().as_uri(),
        )

    def register(
        self,
        uri: str,
        *,
        name: str,
        checksum: str | None,
        size: int | None,
        media_type: str | None,
        kind: ArtifactKind,
        run_id: str,
        node_id: str | None,
        role: str,
        origin: str | None = None,
    ) -> ArtifactRef:
        parsed = urlparse(uri)
        if not parsed.scheme:
            raise ValueError("registered artifact URI must be absolute")
        if kind == "output" and not node_id:
            raise ValueError("published output artifacts require a node_id")
        ref = ArtifactRef(
            name=name,
            uri=uri,
            checksum=checksum,
            size=size,
            media_type=media_type or _guess_media_type(name),
            kind=kind,
            run_id=run_id,
            node_id=node_id,
            role=role,
            origin=origin or uri,
        )
        self._insert_manifest_ref(ref)
        return ref

    def publish(
        self,
        source: str | Path,
        *,
        run_id: str,
        node_id: str,
        role: str,
        name: str | None = None,
        media_type: str | None = None,
    ) -> ArtifactRef:
        path = Path(source)
        artifact_name = name or path.name
        return self._store_path(
            path,
            name=artifact_name,
            run_id=run_id,
            role=role,
            media_type=media_type or _guess_media_type(artifact_name),
            kind="output",
            node_id=node_id,
            origin=path.resolve().as_uri(),
        )

    def manifest(self, run_id: str) -> ArtifactManifest:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT
                    name, uri, checksum, size, media_type, kind,
                    run_id, node_id, role, origin
                FROM artifacts
                WHERE run_id = ?
                ORDER BY CASE kind WHEN 'input' THEN 0 ELSE 1 END, name, COALESCE(node_id, '')
                """,
                (run_id,),
            ).fetchall()
        return ArtifactManifest(
            run_id=run_id,
            artifacts=tuple(
                ArtifactRef(
                    name=row[0],
                    uri=row[1],
                    checksum=row[2],
                    size=row[3],
                    media_type=row[4],
                    kind=row[5],
                    run_id=row[6],
                    node_id=row[7],
                    role=row[8],
                    origin=row[9],
                )
                for row in rows
            ),
        )

    def _store_bytes(
        self,
        content: bytes,
        *,
        name: str,
        run_id: str,
        role: str,
        media_type: str | None,
        kind: ArtifactKind,
        node_id: str | None,
        origin: str,
    ) -> ArtifactRef:
        def write_content(target: BinaryIO) -> tuple[str, int]:
            digest = hashlib.sha256()
            view = memoryview(content)
            for offset in range(0, len(view), _CHUNK_SIZE):
                chunk = view[offset : offset + _CHUNK_SIZE]
                target.write(chunk)
                digest.update(chunk)
            return digest.hexdigest(), len(content)

        return self._store(write_content, name, run_id, role, media_type, kind, node_id, origin)

    def _store_path(
        self,
        path: Path,
        *,
        name: str,
        run_id: str,
        role: str,
        media_type: str | None,
        kind: ArtifactKind,
        node_id: str | None,
        origin: str,
    ) -> ArtifactRef:
        if not path.is_file():
            raise FileNotFoundError(f"artifact source is not a file: {path}")

        def copy_content(target: BinaryIO) -> tuple[str, int]:
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as source:
                while chunk := source.read(_CHUNK_SIZE):
                    target.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            return digest.hexdigest(), size

        return self._store(copy_content, name, run_id, role, media_type, kind, node_id, origin)

    def _store(
        self,
        writer: Any,
        name: str,
        run_id: str,
        role: str,
        media_type: str | None,
        kind: ArtifactKind,
        node_id: str | None,
        origin: str,
    ) -> ArtifactRef:
        root = self._resolved_root()
        temporary_root = root / ".tmp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix="artifact-", dir=temporary_root)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                checksum, size = writer(target)
                target.flush()
                os.fsync(target.fileno())
            blob = root / "blobs" / checksum[:2] / checksum
            blob.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(temporary_path, blob)
            except FileExistsError:
                pass
            ref = ArtifactRef(
                name=name,
                uri=blob.resolve().as_uri(),
                checksum=checksum,
                size=size,
                media_type=media_type,
                kind=kind,
                run_id=run_id,
                node_id=node_id,
                role=role,
                origin=origin,
            )
            self._insert_manifest_ref(ref)
            return ref
        finally:
            temporary_path.unlink(missing_ok=True)

    def _insert_manifest_ref(self, ref: ArtifactRef) -> None:
        try:
            with closing(self._connect()) as connection:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO artifacts
                            (
                                name, uri, checksum, size, media_type,
                                kind, run_id, node_id, role, origin
                            )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            ref.name,
                            ref.uri,
                            ref.checksum,
                            ref.size,
                            ref.media_type,
                            ref.kind,
                            ref.run_id,
                            ref.node_id,
                            ref.role,
                            ref.origin,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise DuplicateArtifactError(
                f"artifact {ref.name!r} already exists for run {ref.run_id!r} as {ref.kind}"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        root = self._resolved_root()
        root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(root / "manifest.sqlite3", timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                name TEXT NOT NULL,
                uri TEXT NOT NULL,
                checksum TEXT,
                size INTEGER,
                media_type TEXT,
                kind TEXT NOT NULL CHECK (kind IN ('input', 'output')),
                run_id TEXT NOT NULL,
                node_id TEXT,
                role TEXT NOT NULL,
                origin TEXT NOT NULL,
                PRIMARY KEY (run_id, kind, name)
            )
            """
        )
        connection.commit()
        return connection

    def _resolved_root(self) -> Path:
        return self.root


def stage_artifact_inputs(value: Any, store: ArtifactStore, *, run_id: str) -> Any:
    """Recursively replace submitted file transports with durable input references."""
    from .runtime import File, S3File

    def stage(item: Any, path: tuple[str, ...]) -> Any:
        role = ".".join(path) or "input"
        if isinstance(item, File):
            return store.stage_input(item, run_id=run_id, role=role)
        if isinstance(item, Path):
            return store.stage_input(item, run_id=run_id, role=role)
        if isinstance(item, S3File):
            parsed = urlparse(item.uri)
            name = Path(unquote(parsed.path)).name or (path[-1] if path else "input")
            return store.register(
                item.uri,
                name=name,
                checksum=item.sha256,
                size=item.size_bytes,
                media_type=item.content_type,
                kind="input",
                run_id=run_id,
                node_id=None,
                role=role,
                origin=item.uri,
            )
        if isinstance(item, ArtifactRef):
            if item.run_id == run_id and item.kind == "input":
                return item
            return store.register(
                item.uri,
                name=item.name,
                checksum=item.checksum,
                size=item.size,
                media_type=item.media_type,
                kind="input",
                run_id=run_id,
                node_id=None,
                role=role,
                origin=item.uri,
            )
        if isinstance(item, BaseModel):
            return {
                field_name: stage(getattr(item, field_name), (*path, field_name))
                for field_name in type(item).model_fields
            }
        if isinstance(item, Mapping):
            return {key: stage(child, (*path, str(key))) for key, child in item.items()}
        if isinstance(item, list):
            return [stage(child, (*path, str(index))) for index, child in enumerate(item)]
        if isinstance(item, tuple):
            return tuple(stage(child, (*path, str(index))) for index, child in enumerate(item))
        return item

    return stage(value, ())


def _guess_media_type(name: str) -> str | None:
    return mimetypes.guess_type(name)[0]
