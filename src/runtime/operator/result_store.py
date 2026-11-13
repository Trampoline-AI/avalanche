"""Private, atomic local storage for operator-managed workflow results.

Accepted result bundles live under one result-storage root next to the operator
state database (``results/`` beside ``state.db``), indexed by the
``run_results`` table, so they survive operator restarts. A ``:memory:``
database gets a private temporary root that is removed when the store closes.

Layout of the root, which is owner-only (``0700``)::

    .avalanche-result-store.lock   owner marker, locked while a store is open
    pending/bundle_<hex>/          bundles being written by run workers
    bundle_<hex>/                  accepted bundles, one per indexed result

A worker publishes into a ``pending`` bundle through a descriptor it received
from the operator. On acceptance the operator validates that bundle, deletes
it, and writes the validated bytes into a new bundle the worker never had a
descriptor for, which it then moves into the root. Nothing is ever deleted by
time; :meth:`ResultStore.delete_run_result` removes one run's result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing.reduction
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .results import (
    MAX_ATTACHMENT_MEDIA_TYPE_LENGTH,
    MAX_ATTACHMENT_NAME_LENGTH,
    MAX_RESULT_ATTACHMENT_BYTES,
    MAX_RESULT_ATTACHMENTS,
    MAX_RESULT_ATTACHMENTS_BYTES,
    MAX_RESULT_TOTAL_BYTES,
    MAX_RESULT_VALUE_JSON_BYTES,
    EncodedWorkflowResult,
    ResultFileAttachment,
    strict_json_loads,
    validate_workflow_result_document,
)
from .state import OperatorStateStore
from .state.results import RunResultRow, RunResultTable, StoredWorkflowResult

_MANIFEST_VERSION = 1
_BUNDLE_NAME = re.compile(r"bundle_[0-9a-f]{32}")
_STORAGE_NAME = re.compile(r"attachment_[0-9]{8}\.bin")
_ATTACHMENT_ID = re.compile(r"file_(0|[1-9][0-9]{0,3})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_READ_CHUNK_SIZE = 1024 * 1024
MAX_RESULT_MANIFEST_BYTES = 1024 * 1024
RESULTS_DIRECTORY = "results"
PENDING_DIRECTORY = "pending"
_OWNER_MARKER = ".avalanche-result-store.lock"
_OWNER_MARKER_BYTES = b"avalanche-result-store-v1\n"
_SECURE_ANCHORED_ERROR = (
    "Secure workflow result publication is unavailable: this platform does not "
    "support directory-anchored file operations"
)
_SECURE_STORE_ERROR = (
    "Secure workflow result storage is unavailable: this platform does not "
    "support directory-anchored file operations and file locks"
)
logger = logging.getLogger(__name__)

__all__ = [
    "MAX_RESULT_MANIFEST_BYTES",
    "PENDING_DIRECTORY",
    "RESULTS_DIRECTORY",
    "CancellationSignal",
    "PendingResultBundle",
    "ResultBundleMissingError",
    "ResultIndexReport",
    "ResultPublicationCancelledError",
    "ResultStore",
    "ResultStoreLockedError",
    "StoredWorkflowResult",
    "detach_transferred_bundle_descriptor",
    "duplicate_bundle_descriptor_for_spawn",
    "publish_workflow_result",
    "report_result_index",
    "require_worker_descriptor_transfer",
    "result_root_for",
]


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class _NeverCancelled:
    @staticmethod
    def is_set() -> bool:
        return False


class ResultPublicationCancelledError(RuntimeError):
    """Raised when cancellation interrupts result publication."""


class ResultBundleMissingError(RuntimeError):
    """An indexed result's bundle directory is no longer in the result root."""


class ResultStoreLockedError(RuntimeError):
    """Another open result store already owns this result-storage root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        super().__init__(
            f"Result storage {root} is already in use by another operator. Each "
            "operator state database needs its own folder; pass a --state-db in "
            "another folder."
        )


@dataclass(frozen=True)
class PendingResultBundle:
    """A private per-run directory prepared by the operator parent."""

    name: str
    path: str
    device: int
    inode: int
    descriptor: int


@dataclass(frozen=True)
class ResultIndexReport:
    """Where the result index and the bundles on disk disagree."""

    missing_bundles: tuple[RunResultRow, ...]
    """Indexed results whose bundle directory is gone; loading them fails."""
    unindexed_bundles: tuple[str, ...]
    """Accepted bundle directories that no index row names; never served."""
    unexpected_entries: tuple[str, ...]
    """Root entries that are neither bundles nor the store's own files."""

    @property
    def consistent(self) -> bool:
        return not (self.missing_bundles or self.unindexed_bundles or self.unexpected_entries)


@dataclass(frozen=True)
class _StoredAttachment:
    attachment_id: str
    storage_name: str
    name: str | None
    media_type: str | None
    sha256: str
    size: int


@dataclass(frozen=True)
class _Manifest:
    value_sha256: str
    value_size: int
    files: tuple[_StoredAttachment, ...]


@dataclass
class _OpenedBundle:
    descriptor: int

    def close(self) -> None:
        os.close(self.descriptor)


def result_root_for(state_store: OperatorStateStore) -> Path | None:
    """The result-storage root beside a database file, or ``None`` for ``:memory:``."""
    path = state_store.path
    return None if path is None else path.parent / RESULTS_DIRECTORY


class ResultStore:
    """Own the result bundles of one operator state database.

    Lock order: callers may hold the operator lock; the store takes its own lock
    and then, for index reads and writes, the state store's lock. It never calls
    back into the operator.
    """

    def __init__(self, state_store: OperatorStateStore) -> None:
        if not _supports_secure_store():
            raise RuntimeError(_SECURE_STORE_ERROR)
        self._state_store = state_store
        stable_root = result_root_for(state_store)
        self._temporary = stable_root is None
        if stable_root is None:
            self._root = Path(tempfile.mkdtemp(prefix="operator-results-"))
        else:
            self._root = stable_root
            _make_private_directory(self._root)
        self._root_fd: int | None = None
        self._owner_fd: int | None = None
        self._pending_fd: int | None = None
        try:
            self._root_fd = os.open(self._root, _directory_flags())
            _validate_result_root_metadata(os.fstat(self._root_fd), self._root)
            self._owner_fd = _open_and_lock_owner_marker(self._root_fd, self._root)
            self._pending_fd = _open_private_subdirectory(self._root_fd, PENDING_DIRECTORY)
            self.recovered_pending_bundles = _remove_bundles_in(self._pending_fd)
            if self.recovered_pending_bundles:
                os.fsync(self._pending_fd)
        except BaseException as failure:
            _close_all(self._take_root_descriptors(), failure)
            if self._temporary:
                try:
                    shutil.rmtree(self._root)
                except OSError as exc:
                    failure.add_note(f"Result storage cleanup also failed: {exc}")
            raise
        self._lock = threading.RLock()
        # Open pending bundles by their unique name, never by a reusable fd number.
        self._pending_bundles: dict[str, int] = {}
        self._closed = False

    @property
    def root(self) -> Path:
        return self._root

    def prepare(self) -> PendingResultBundle:
        with self._lock:
            pending_fd = self._require_open(self._pending_fd)
            name = f"bundle_{uuid4().hex}"
            os.mkdir(name, mode=0o700, dir_fd=pending_fd)
            bundle = _open_bundle_at(pending_fd, name)
            metadata = os.fstat(bundle.descriptor)
            self._pending_bundles[name] = bundle.descriptor
            return PendingResultBundle(
                name=name,
                path=str(self._root / PENDING_DIRECTORY / name),
                device=metadata.st_dev,
                inode=metadata.st_ino,
                descriptor=bundle.descriptor,
            )

    def accept(
        self,
        pending: PendingResultBundle,
        manifest_sha256: str,
        cancel_signal: CancellationSignal | None = None,
    ) -> StoredWorkflowResult:
        """Validate a worker's bundle and store its bytes as a new accepted bundle.

        The accepted bundle is not indexed yet: the operator records it with
        :class:`RunResultTable` once the run has ended in success, or discards it.
        """
        return self.accept_with_value(pending, manifest_sha256, cancel_signal)[0]

    def accept_with_value(
        self,
        pending: PendingResultBundle,
        manifest_sha256: str,
        cancel_signal: CancellationSignal | None = None,
    ) -> tuple[StoredWorkflowResult, str]:
        """Accept a worker's bundle and also return its validated value JSON.

        The value is the one just validated and written, so a caller that needs
        it does not have to read the accepted bundle back.
        """
        _validate_sha256(manifest_sha256, "manifest sha256")
        with self._lock:
            self._ensure_open()
            if self._pending_bundles.get(pending.name) != pending.descriptor:
                raise ValueError("Result bundle is not pending in this result store")
            pending_bundle = self._open_retained_bundle(
                pending.descriptor,
                expected_identity=(pending.device, pending.inode),
            )
            try:
                payload = _read_bundle(pending_bundle, manifest_sha256, cancel_signal)
            finally:
                pending_bundle.close()
            self._discard_pending(pending)
            return self._write_accepted_bundle(payload, cancel_signal), payload.value_json

    def load(
        self,
        stored: StoredWorkflowResult,
        cancel_signal: CancellationSignal | None = None,
    ) -> EncodedWorkflowResult:
        """Read and fully re-validate one accepted bundle against its index entry."""
        with self._lock:
            root_fd = self._require_open(self._root_fd)
            try:
                bundle = _open_bundle_at(root_fd, stored.bundle_name)
            except FileNotFoundError as exc:
                raise ResultBundleMissingError(
                    f"Result bundle {stored.bundle_name} is missing from {self._root}"
                ) from exc
            try:
                return _read_bundle(bundle, stored.manifest_sha256, cancel_signal)
            finally:
                bundle.close()

    def discard(self, bundle: PendingResultBundle | StoredWorkflowResult) -> None:
        """Remove a pending bundle, or an accepted bundle that was never indexed."""
        with self._lock:
            if self._closed:
                return
            if isinstance(bundle, StoredWorkflowResult):
                _remove_bundle_at(self._require_open(self._root_fd), bundle.bundle_name)
                return
            if self._pending_bundles.get(bundle.name) != bundle.descriptor:
                # Already accepted or discarded; its fd number may belong to another run.
                return
            self._discard_pending(bundle)

    def delete_run_result(self, run_id: str) -> RunResultRow | None:
        """Delete one run's index row and then its bundle; return the deleted row.

        Returns ``None`` when the run has no stored result. The row is committed
        as deleted before the bundle is removed, so a failure in between leaves
        an unindexed bundle (reported by :meth:`check_index`), never an index row
        that serves a half-deleted bundle. A row whose bundle is already missing
        is deleted all the same.
        """
        with self._lock:
            root_fd = self._require_open(self._root_fd)
            with self._state_store.transaction() as connection:
                row = RunResultTable.delete(connection, run_id)
            if row is None:
                return None
            try:
                _remove_bundle_at(root_fd, row.bundle_name)
            except FileNotFoundError:
                pass
            return row

    def remove_unindexed_bundles(self) -> tuple[str, ...]:
        """Remove every accepted bundle no index row names; return their names.

        These are left by a crash between a bundle's acceptance and its index
        commit, or by a delete interrupted after its row was committed. They are
        never served, so only an explicit clear removes them.
        """
        with self._lock:
            root_fd = self._require_open(self._root_fd)
            names = self.check_index().unindexed_bundles
            for name in names:
                _remove_bundle_at(root_fd, name)
            return names

    def check_index(self) -> ResultIndexReport:
        """Compare the ``run_results`` index with the bundles in the root."""
        with self._lock:
            root_fd = self._require_open(self._root_fd)
            with self._state_store.transaction() as connection:
                rows = RunResultTable.list_all(connection)
            bundles: set[str] = set()
            unexpected: list[str] = []
            with os.scandir(root_fd) as entries:
                for entry in entries:
                    if entry.name in (_OWNER_MARKER, PENDING_DIRECTORY):
                        continue
                    if _BUNDLE_NAME.fullmatch(entry.name) and entry.is_dir(
                        follow_symlinks=False
                    ):
                        bundles.add(entry.name)
                    else:
                        unexpected.append(entry.name)
            indexed = {row.bundle_name for row in rows}
            return ResultIndexReport(
                missing_bundles=tuple(row for row in rows if row.bundle_name not in bundles),
                unindexed_bundles=tuple(sorted(bundles - indexed)),
                unexpected_entries=tuple(sorted(unexpected)),
            )

    def close(self) -> None:
        """Release the root; a temporary root is removed with everything in it."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._temporary:
                    self._remove_temporary_root()
            except BaseException as failure:
                _close_all(self._take_all_descriptors(), failure)
                raise
            _close_all(self._take_all_descriptors(), None)

    def _write_accepted_bundle(
        self,
        payload: EncodedWorkflowResult,
        cancel_signal: CancellationSignal | None,
    ) -> StoredWorkflowResult:
        root_fd = self._require_open(self._root_fd)
        pending_fd = self._require_open(self._pending_fd)
        name = f"bundle_{uuid4().hex}"
        os.mkdir(name, mode=0o700, dir_fd=pending_fd)
        try:
            bundle = _open_bundle_at(pending_fd, name)
            try:
                metadata = os.fstat(bundle.descriptor)
                manifest_sha256 = publish_workflow_result(
                    payload,
                    bundle.descriptor,
                    (metadata.st_dev, metadata.st_ino),
                    cancel_signal or _NeverCancelled(),
                )
            finally:
                bundle.close()
            os.rename(name, name, src_dir_fd=pending_fd, dst_dir_fd=root_fd)
        except BaseException as failure:
            try:
                _remove_bundle_at(pending_fd, name)
            except Exception as exc:
                failure.add_note(f"Removing the unaccepted result bundle also failed: {exc}")
            raise
        os.fsync(pending_fd)
        os.fsync(root_fd)
        return StoredWorkflowResult(
            bundle_name=name,
            manifest_sha256=manifest_sha256,
            byte_size=_encoded_result_byte_size(payload),
            published_at=time.time(),
        )

    def _discard_pending(self, bundle: PendingResultBundle) -> None:
        if type(bundle.name) is not str or _BUNDLE_NAME.fullmatch(bundle.name) is None:
            raise ValueError("Result bundle descriptor is malformed")
        try:
            pending_fd = self._require_open(self._pending_fd)
            retained = self._open_retained_bundle(
                bundle.descriptor, (bundle.device, bundle.inode)
            )
            try:
                _empty_bundle(retained.descriptor)
            finally:
                retained.close()
            _remove_bundle_at(pending_fd, bundle.name, (bundle.device, bundle.inode))
        finally:
            os.close(self._pending_bundles.pop(bundle.name))

    def _remove_temporary_root(self) -> None:
        root_fd = self._require_descriptor(self._root_fd)
        pending_fd = self._require_descriptor(self._pending_fd)
        _remove_bundles_in(pending_fd)
        os.rmdir(PENDING_DIRECTORY, dir_fd=root_fd)
        _remove_bundles_in(root_fd, keep=frozenset({_OWNER_MARKER}))
        os.unlink(_OWNER_MARKER, dir_fd=root_fd)
        parent_fd = os.open(self._root.parent, _directory_flags())
        try:
            current = os.stat(self._root.name, dir_fd=parent_fd, follow_symlinks=False)
            opened = os.fstat(root_fd)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("Result-storage root identity changed")
            os.rmdir(self._root.name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    def _take_root_descriptors(self) -> tuple[int, ...]:
        descriptors = tuple(
            descriptor
            for descriptor in (self._pending_fd, self._owner_fd, self._root_fd)
            if descriptor is not None
        )
        self._pending_fd = self._owner_fd = self._root_fd = None
        return descriptors

    def _take_all_descriptors(self) -> tuple[int, ...]:
        bundle_descriptors = tuple(self._pending_bundles.values())
        self._pending_bundles.clear()
        return (*bundle_descriptors, *self._take_root_descriptors())

    @staticmethod
    def _open_retained_bundle(
        descriptor: int,
        expected_identity: tuple[int, int] | None = None,
    ) -> _OpenedBundle:
        duplicate = os.dup(descriptor)
        try:
            _validate_bundle_metadata(os.fstat(duplicate), expected_identity)
        except BaseException:
            os.close(duplicate)
            raise
        return _OpenedBundle(descriptor=duplicate)

    def _require_open(self, descriptor: int | None) -> int:
        self._ensure_open()
        return self._require_descriptor(descriptor)

    @staticmethod
    def _require_descriptor(descriptor: int | None) -> int:
        if descriptor is None:
            raise RuntimeError("Result-storage root descriptor is unavailable")
        return descriptor

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Result store is closed")


def _close_all(descriptors: tuple[int, ...], primary: BaseException | None) -> None:
    """Close every descriptor, even after one fails, without hiding a primary failure.

    A close failure is attached to ``primary`` when there is one. Otherwise the
    first close failure is raised once all descriptors were tried, with any later
    ones attached to it.
    """
    first: OSError | None = None
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError as exc:
            note = f"Closing result storage descriptor {descriptor} also failed: {exc}"
            if primary is not None:
                primary.add_note(note)
            elif first is None:
                first = exc
            else:
                first.add_note(note)
    if first is not None:
        raise first


def _read_bundle(
    bundle: _OpenedBundle,
    manifest_sha256: str,
    cancel_signal: CancellationSignal | None,
) -> EncodedWorkflowResult:
    manifest_bytes = _read_regular_file(
        bundle,
        "manifest.json",
        cancel_signal,
        maximum_bytes=MAX_RESULT_MANIFEST_BYTES,
    )
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
        raise ValueError("Result manifest sha256 does not match")
    return _materialize_bundle(bundle, _decode_manifest(manifest_bytes), cancel_signal)


def _materialize_bundle(
    bundle: _OpenedBundle,
    manifest: _Manifest,
    cancel_signal: CancellationSignal | None,
) -> EncodedWorkflowResult:
    expected_names = {"manifest.json", "value.json"}
    value_bytes = _read_regular_file(
        bundle,
        "value.json",
        cancel_signal,
        maximum_bytes=manifest.value_size,
    )
    try:
        value_json = value_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Result value JSON is not UTF-8") from exc
    _validate_file_bytes(
        value_bytes,
        manifest.value_size,
        manifest.value_sha256,
        "result value",
    )

    attachment_ids: set[str] = set()
    storage_names: set[str] = set()
    files: list[ResultFileAttachment] = []
    for item in manifest.files:
        if item.attachment_id in attachment_ids:
            raise ValueError(f"Duplicate result file attachment {item.attachment_id!r}")
        if item.storage_name in storage_names:
            raise ValueError("Duplicate result attachment storage name")
        attachment_ids.add(item.attachment_id)
        storage_names.add(item.storage_name)
        expected_names.add(item.storage_name)
        files.append(
            ResultFileAttachment(
                attachment_id=item.attachment_id,
                content=_load_attachment(bundle, item, cancel_signal),
                name=item.name,
                media_type=item.media_type,
                sha256=item.sha256,
            )
        )

    actual_names = set(_bounded_bundle_names(bundle.descriptor))
    if actual_names != expected_names:
        raise ValueError("Result bundle contains missing or unexpected files")
    validate_workflow_result_document(value_json, attachment_ids)
    _check_optional_cancelled(cancel_signal)
    return EncodedWorkflowResult(value_json=value_json, files=tuple(files))


def _open_bundle_at(
    parent_fd: int,
    bundle_name: str,
    expected_identity: tuple[int, int] | None = None,
) -> _OpenedBundle:
    if type(bundle_name) is not str or _BUNDLE_NAME.fullmatch(bundle_name) is None:
        raise ValueError("Result bundle descriptor is malformed")
    descriptor = os.open(bundle_name, _directory_flags(), dir_fd=parent_fd)
    try:
        _validate_bundle_metadata(os.fstat(descriptor), expected_identity)
    except BaseException:
        os.close(descriptor)
        raise
    return _OpenedBundle(descriptor=descriptor)


def _empty_bundle(bundle_fd: int) -> None:
    os.fchmod(bundle_fd, 0o700)
    for entry_name in _bounded_bundle_names(bundle_fd):
        if _is_directory_at(bundle_fd, entry_name):
            raise ValueError("Result bundle unexpectedly contains a directory")
        os.unlink(entry_name, dir_fd=bundle_fd)
    os.fsync(bundle_fd)


def _remove_bundle_at(
    parent_fd: int,
    bundle_name: str,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    """Empty and remove one bundle directory, never following a link."""
    bundle = _open_bundle_at(parent_fd, bundle_name, expected_identity)
    try:
        _empty_bundle(bundle.descriptor)
    finally:
        bundle.close()
    os.rmdir(bundle_name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _remove_bundles_in(directory_fd: int, *, keep: frozenset[str] = frozenset()) -> int:
    """Remove every bundle in a directory that may hold only bundles and ``keep``."""
    names = [entry.name for entry in os.scandir(directory_fd) if entry.name not in keep]
    for name in names:
        if _BUNDLE_NAME.fullmatch(name) is None or not _is_directory_at(directory_fd, name):
            raise ValueError(f"Result storage contains an unexpected entry {name!r}")
        _remove_bundle_at(directory_fd, name)
    return len(names)


def _make_private_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(path, mode=0o700)
    except FileExistsError:
        return
    os.chmod(path, 0o700)


def _open_private_subdirectory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        _validate_bundle_metadata(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def publish_workflow_result(
    encoded: EncodedWorkflowResult,
    bundle_descriptor: int,
    bundle_identity: tuple[int, int],
    cancel_signal: CancellationSignal,
) -> str:
    """Atomically publish an encoded result and return its manifest digest."""
    _validate_worker_bundle_descriptor(bundle_descriptor, bundle_identity)
    _check_cancelled(cancel_signal)
    value_bytes = encoded.value_json.encode("utf-8")
    _validate_publication_metadata(encoded, value_bytes)
    value_sha256 = _write_private_file(
        bundle_descriptor,
        "value.json",
        value_bytes,
        cancel_signal,
    )

    files: list[dict[str, Any]] = []
    for index, item in enumerate(encoded.files):
        _check_cancelled(cancel_signal)
        storage_name = f"attachment_{index:08d}.bin"
        digest = _write_private_file(
            bundle_descriptor,
            storage_name,
            item.content,
            cancel_signal,
        )
        if digest != item.sha256:
            raise ValueError(
                f"File attachment {item.attachment_id!r} sha256 changed during publication"
            )
        files.append(
            {
                "attachment_id": item.attachment_id,
                "storage_name": storage_name,
                "name": item.name,
                "media_type": item.media_type,
                "sha256": digest,
                "size": len(item.content),
            }
        )

    manifest_bytes = json.dumps(
        {
            "version": _MANIFEST_VERSION,
            "value": {
                "sha256": value_sha256,
                "size": len(value_bytes),
            },
            "files": files,
        },
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(manifest_bytes) > MAX_RESULT_MANIFEST_BYTES:
        raise ValueError(f"Result manifest exceeds {MAX_RESULT_MANIFEST_BYTES} bytes")
    _check_cancelled(cancel_signal)
    manifest_sha256 = _write_private_file(
        bundle_descriptor,
        "manifest.json",
        manifest_bytes,
        cancel_signal,
    )
    os.fsync(bundle_descriptor)
    _check_cancelled(cancel_signal)
    return manifest_sha256


def _write_private_file(
    directory_descriptor: int,
    final_name: str,
    content: bytes,
    cancel_signal: CancellationSignal,
) -> str:
    temporary_name = f".tmp-{uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(
        temporary_name,
        flags,
        0o600,
        dir_fd=directory_descriptor,
    )
    digest = hashlib.sha256()
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        for offset in range(0, len(view), _READ_CHUNK_SIZE):
            _check_cancelled(cancel_signal)
            chunk = view[offset : offset + _READ_CHUNK_SIZE]
            written = 0
            while written < len(chunk):
                written += os.write(descriptor, chunk[written:])
            digest.update(chunk)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        _unlink_at(directory_descriptor, temporary_name)
        raise
    else:
        os.close(descriptor)
    try:
        os.link(
            temporary_name,
            final_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except BaseException:
        _unlink_at(directory_descriptor, temporary_name)
        raise
    os.unlink(temporary_name, dir_fd=directory_descriptor)
    return digest.hexdigest()


def _decode_manifest(payload: bytes) -> _Manifest:
    if len(payload) > MAX_RESULT_MANIFEST_BYTES:
        raise ValueError(f"Result manifest exceeds {MAX_RESULT_MANIFEST_BYTES} bytes")
    try:
        document = strict_json_loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid result manifest JSON") from exc
    if type(document) is not dict or set(document) != {"version", "value", "files"}:
        raise ValueError("Malformed result manifest")
    if type(document["version"]) is not int or document["version"] != _MANIFEST_VERSION:
        raise ValueError("Unsupported result manifest version")
    value = document["value"]
    if type(value) is not dict or set(value) != {"sha256", "size"}:
        raise ValueError("Malformed result value descriptor")
    value_sha256 = _required_sha256(value, "sha256")
    value_size = _required_size(value, "size")
    if value_size > MAX_RESULT_VALUE_JSON_BYTES:
        raise ValueError(f"Result value JSON exceeds {MAX_RESULT_VALUE_JSON_BYTES} bytes")
    raw_files = document["files"]
    if type(raw_files) is not list:
        raise ValueError("Result manifest files must be a list")
    if len(raw_files) > MAX_RESULT_ATTACHMENTS:
        raise ValueError(f"Result manifest exceeds {MAX_RESULT_ATTACHMENTS} file attachments")
    files = tuple(_decode_attachment(item) for item in raw_files)
    total_attachment_bytes = sum(item.size for item in files)
    if total_attachment_bytes > MAX_RESULT_ATTACHMENTS_BYTES:
        raise ValueError(
            "Result manifest file attachments exceed " f"{MAX_RESULT_ATTACHMENTS_BYTES} bytes"
        )
    if value_size + total_attachment_bytes > MAX_RESULT_TOTAL_BYTES:
        raise ValueError(f"Result manifest exceeds {MAX_RESULT_TOTAL_BYTES} result bytes")
    return _Manifest(value_sha256=value_sha256, value_size=value_size, files=files)


def _decode_attachment(value: Any) -> _StoredAttachment:
    expected = {
        "attachment_id",
        "storage_name",
        "name",
        "media_type",
        "sha256",
        "size",
    }
    if type(value) is not dict or set(value) != expected:
        raise ValueError("Malformed stored result attachment")
    attachment_id = value["attachment_id"]
    if type(attachment_id) is not str or _ATTACHMENT_ID.fullmatch(attachment_id) is None:
        raise ValueError("Stored result attachment ID is malformed")
    if int(attachment_id[5:]) >= MAX_RESULT_ATTACHMENTS:
        raise ValueError("Stored result attachment ID is malformed")
    storage_name = value["storage_name"]
    if type(storage_name) is not str or _STORAGE_NAME.fullmatch(storage_name) is None:
        raise ValueError("Stored result attachment filename is malformed")
    name = value["name"]
    media_type = value["media_type"]
    if name is not None and type(name) is not str:
        raise ValueError("Stored result attachment name must be a string or null")
    if name is not None and len(name) > MAX_ATTACHMENT_NAME_LENGTH:
        raise ValueError(
            f"Stored result attachment name exceeds {MAX_ATTACHMENT_NAME_LENGTH} characters"
        )
    if media_type is not None and type(media_type) is not str:
        raise ValueError("Stored result attachment media type must be a string or null")
    if media_type is not None and len(media_type) > MAX_ATTACHMENT_MEDIA_TYPE_LENGTH:
        raise ValueError(
            "Stored result attachment media type exceeds "
            f"{MAX_ATTACHMENT_MEDIA_TYPE_LENGTH} characters"
        )
    size = _required_size(value, "size")
    if size > MAX_RESULT_ATTACHMENT_BYTES:
        raise ValueError(
            f"Stored result attachment exceeds {MAX_RESULT_ATTACHMENT_BYTES} bytes"
        )
    return _StoredAttachment(
        attachment_id=attachment_id,
        storage_name=storage_name,
        name=name,
        media_type=media_type,
        sha256=_required_sha256(value, "sha256"),
        size=size,
    )


def _required_sha256(value: dict[str, Any], field: str) -> str:
    digest = value[field]
    _validate_sha256(digest, field)
    return digest


def _validate_sha256(value: Any, field: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase hexadecimal SHA-256")


def _required_size(value: dict[str, Any], field: str) -> int:
    size = value[field]
    if type(size) is not int or size < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return size


def _read_regular_file(
    bundle: _OpenedBundle,
    name: str,
    cancel_signal: CancellationSignal | None = None,
    *,
    maximum_bytes: int,
) -> bytes:
    if type(maximum_bytes) is not int or maximum_bytes < 0:
        raise ValueError("Regular-file read maximum must be a non-negative integer")
    descriptor = _open_regular_file(bundle, name)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            _check_optional_cancelled(cancel_signal)
            read_size = min(_READ_CHUNK_SIZE, maximum_bytes - total + 1)
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise ValueError(f"Stored {name} exceeds {maximum_bytes} bytes")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _hash_regular_file(
    bundle: _OpenedBundle,
    name: str,
    cancel_signal: CancellationSignal | None = None,
    *,
    maximum_bytes: int,
) -> tuple[str, int]:
    if type(maximum_bytes) is not int or maximum_bytes < 0:
        raise ValueError("Regular-file hash maximum must be a non-negative integer")
    descriptor = _open_regular_file(bundle, name)
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            _check_optional_cancelled(cancel_signal)
            chunk = os.read(
                descriptor,
                min(_READ_CHUNK_SIZE, maximum_bytes - size + 1),
            )
            if not chunk:
                break
            size += len(chunk)
            if size > maximum_bytes:
                raise ValueError(f"Stored {name} exceeds {maximum_bytes} bytes")
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _open_regular_file(bundle: _OpenedBundle, name: str) -> int:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=bundle.descriptor,
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        os.close(descriptor)
        raise ValueError(f"Stored result file {name!r} is not a private regular file")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        os.close(descriptor)
        raise ValueError(f"Stored result file {name!r} permissions are too broad")
    return descriptor


def _bounded_bundle_names(directory_descriptor: int) -> list[str]:
    maximum_entries = MAX_RESULT_ATTACHMENTS + 2
    entries: list[str] = []
    with os.scandir(directory_descriptor) as iterator:
        for entry in iterator:
            entries.append(entry.name)
            if len(entries) > maximum_entries:
                raise ValueError(f"Result bundle exceeds {maximum_entries} stored files")
    return entries


def _is_directory_at(directory_descriptor: int, name: str) -> bool:
    metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    return stat.S_ISDIR(metadata.st_mode)


def _load_attachment(
    bundle: _OpenedBundle,
    item: _StoredAttachment,
    cancel_signal: CancellationSignal | None = None,
) -> bytes:
    content = _read_regular_file(
        bundle,
        item.storage_name,
        cancel_signal,
        maximum_bytes=item.size,
    )
    _validate_file_bytes(content, item.size, item.sha256, item.attachment_id)
    return content


def _validate_file_bytes(content: bytes, size: int, digest: str, label: str) -> None:
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError(f"Stored {label} metadata does not match")


def _encoded_result_byte_size(payload: EncodedWorkflowResult) -> int:
    return len(payload.value_json.encode("utf-8")) + sum(
        len(item.content) for item in payload.files
    )


def _validate_bundle_metadata(
    metadata: os.stat_result,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Result bundle is not a directory")
    if (
        expected_identity is not None
        and (
            metadata.st_dev,
            metadata.st_ino,
        )
        != expected_identity
    ):
        raise ValueError("Result bundle identity changed")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("Result bundle permissions are too broad")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("Result bundle is not owned by the current user")


def _check_cancelled(cancel_signal: CancellationSignal) -> None:
    if cancel_signal.is_set():
        raise ResultPublicationCancelledError("Result publication was cancelled")


def _check_optional_cancelled(
    cancel_signal: CancellationSignal | None,
) -> None:
    if cancel_signal is not None:
        _check_cancelled(cancel_signal)


def _supports_secure_worker_publication() -> bool:
    return (
        all(function in os.supports_dir_fd for function in (os.open, os.unlink, os.link))
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "fchmod")
    )


def duplicate_bundle_descriptor_for_spawn(bundle: PendingResultBundle) -> Any:
    """Return a spawn-transfer wrapper for only the parent's retained bundle fd."""
    require_worker_descriptor_transfer()
    duplicate = multiprocessing.reduction.DupFd
    _validate_worker_bundle_descriptor(
        bundle.descriptor,
        (bundle.device, bundle.inode),
    )
    return duplicate(bundle.descriptor)


def require_worker_descriptor_transfer() -> None:
    """Fail before preparing a bundle when secure spawn transfer is unavailable."""
    if not _supports_secure_worker_publication():
        raise RuntimeError(_SECURE_ANCHORED_ERROR)
    duplicate = getattr(multiprocessing.reduction, "DupFd", None)
    if not callable(duplicate):
        raise RuntimeError(
            "Secure workflow result publication is unavailable: this platform "
            "does not support multiprocessing descriptor transfer"
        )


def detach_transferred_bundle_descriptor(
    transferred_descriptor: Any,
    expected_identity: tuple[int, int],
) -> int:
    """Detach and validate one child-owned directory descriptor."""
    detach = getattr(transferred_descriptor, "detach", None)
    if not callable(detach):
        raise RuntimeError("Result bundle descriptor transfer is malformed")
    descriptor = detach()
    try:
        _validate_worker_bundle_descriptor(descriptor, expected_identity)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_worker_bundle_descriptor(
    descriptor: int,
    expected_identity: tuple[int, int],
) -> None:
    if not _supports_secure_worker_publication():
        raise RuntimeError(_SECURE_ANCHORED_ERROR)
    if type(descriptor) is not int or descriptor < 0:
        raise ValueError("Result bundle descriptor is malformed")
    if (
        type(expected_identity) is not tuple
        or len(expected_identity) != 2
        or any(type(value) is not int or value < 0 for value in expected_identity)
    ):
        raise ValueError("Result bundle identity is malformed")
    _validate_bundle_metadata(os.fstat(descriptor), expected_identity)


def _unlink_at(directory_descriptor: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_descriptor)
    except FileNotFoundError:
        pass


def _validate_publication_metadata(
    encoded: EncodedWorkflowResult,
    value_bytes: bytes,
) -> None:
    if type(encoded) is not EncodedWorkflowResult:
        raise ValueError("Malformed encoded workflow result")
    if len(value_bytes) > MAX_RESULT_VALUE_JSON_BYTES:
        raise ValueError(f"Workflow result JSON exceeds {MAX_RESULT_VALUE_JSON_BYTES} bytes")
    if type(encoded.files) is not tuple:
        raise ValueError("Workflow result attachments must be a tuple")
    if len(encoded.files) > MAX_RESULT_ATTACHMENTS:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_ATTACHMENTS} file attachments")
    attachment_ids: set[str] = set()
    total_attachment_bytes = 0
    for item in encoded.files:
        if type(item) is not ResultFileAttachment:
            raise ValueError("Malformed result file attachment")
        if (
            type(item.attachment_id) is not str
            or _ATTACHMENT_ID.fullmatch(item.attachment_id) is None
            or int(item.attachment_id[5:]) >= MAX_RESULT_ATTACHMENTS
        ):
            raise ValueError("Result file attachment ID is malformed")
        if item.attachment_id in attachment_ids:
            raise ValueError(f"Duplicate result file attachment {item.attachment_id!r}")
        attachment_ids.add(item.attachment_id)
        if type(item.content) is not bytes:
            raise ValueError("Result file attachment content must be bytes")
        if len(item.content) > MAX_RESULT_ATTACHMENT_BYTES:
            raise ValueError(
                f"Result file attachment exceeds {MAX_RESULT_ATTACHMENT_BYTES} bytes"
            )
        total_attachment_bytes += len(item.content)
        if total_attachment_bytes > MAX_RESULT_ATTACHMENTS_BYTES:
            raise ValueError(
                "Workflow result file attachments exceed "
                f"{MAX_RESULT_ATTACHMENTS_BYTES} bytes"
            )
        if item.name is not None and (
            type(item.name) is not str or len(item.name) > MAX_ATTACHMENT_NAME_LENGTH
        ):
            raise ValueError("Result file attachment name is malformed or too long")
        if item.media_type is not None and (
            type(item.media_type) is not str
            or len(item.media_type) > MAX_ATTACHMENT_MEDIA_TYPE_LENGTH
        ):
            raise ValueError("Result file attachment media type is malformed or too long")
        _validate_sha256(item.sha256, "attachment sha256")
    if len(value_bytes) + total_attachment_bytes > MAX_RESULT_TOTAL_BYTES:
        raise ValueError(f"Workflow result exceeds {MAX_RESULT_TOTAL_BYTES} bytes")
    validate_workflow_result_document(encoded.value_json, attachment_ids)


def _directory_flags() -> int:
    """Open a directory read-only, never through a symlink; supported platforms only."""
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _supports_secure_store() -> bool:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return False
    return all(
        function in os.supports_dir_fd
        for function in (os.open, os.mkdir, os.unlink, os.rmdir, os.link, os.rename, os.stat)
    ) and (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"))


def _open_and_lock_owner_marker(root_fd: int, root: Path) -> int:
    """Open (creating once) and exclusively lock the root's ownership marker."""
    import fcntl

    flags = os.O_RDWR | os.O_NOFOLLOW
    descriptor = os.open(_OWNER_MARKER, flags | os.O_CREAT, 0o600, dir_fd=root_fd)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ResultStoreLockedError(root) from exc
        if os.fstat(descriptor).st_size == 0:
            # Created now, or by a store that stopped before writing it.
            os.write(descriptor, _OWNER_MARKER_BYTES)
            os.fsync(descriptor)
            os.fsync(root_fd)
        _validate_owner_marker(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_result_root_metadata(metadata: os.stat_result, root: Path) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"Result-storage root {root} is not a directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError(
            f"Result-storage root {root} must be private to its owner (mode 0700), "
            f"found {stat.S_IMODE(metadata.st_mode):04o}"
        )
    if metadata.st_uid != os.getuid():
        raise ValueError(f"Result-storage root {root} is owned by another user")


def _validate_owner_marker(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("Result-storage ownership marker is not a private file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("Result-storage ownership marker permissions are not private")
    if metadata.st_uid != os.getuid():
        raise ValueError("Result-storage ownership marker has a different owner")
    if metadata.st_size != len(_OWNER_MARKER_BYTES):
        raise ValueError("Result-storage ownership marker is malformed")
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.read(descriptor, len(_OWNER_MARKER_BYTES) + 1) != _OWNER_MARKER_BYTES:
        raise ValueError("Result-storage ownership marker is malformed")


def report_result_index(store: ResultStore) -> ResultIndexReport:
    """Warn about every disagreement between the result index and the bundles on disk.

    The operator still starts: a missing bundle fails only that run's result, and
    an unindexed bundle is never served. Nothing is deleted or repaired here.
    """
    if store.recovered_pending_bundles:
        logger.warning(
            "Removed %d unfinished result bundle(s) left in %s by runs that never completed",
            store.recovered_pending_bundles,
            store.root / PENDING_DIRECTORY,
        )
    report = store.check_index()
    for row in report.missing_bundles:
        logger.warning(
            "Stored result for run %s is unavailable: its bundle %s is missing from %s",
            row.run_id,
            row.bundle_name,
            store.root,
        )
    if report.unindexed_bundles:
        logger.warning(
            "Result storage %s holds %d bundle(s) no stored result refers to; they are "
            "never served: %s",
            store.root,
            len(report.unindexed_bundles),
            ", ".join(report.unindexed_bundles),
        )
    if report.unexpected_entries:
        logger.warning(
            "Result storage %s contains unexpected entries: %s",
            store.root,
            ", ".join(report.unexpected_entries),
        )
    return report
