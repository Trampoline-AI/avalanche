"""Private, atomic local storage for operator-managed workflow results."""

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

_MANIFEST_VERSION = 1
_BUNDLE_NAME = re.compile(r"bundle_[0-9a-f]{32}")
_ROOT_NAME = re.compile(r"operator-results-[A-Za-z0-9_-]+")
_STORAGE_NAME = re.compile(r"attachment_[0-9]{8}\.bin")
_ATTACHMENT_ID = re.compile(r"file_(0|[1-9][0-9]{0,3})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_READ_CHUNK_SIZE = 1024 * 1024
MAX_RESULT_MANIFEST_BYTES = 1024 * 1024
MAX_RETAINED_RESULTS = 1024
MAX_RETAINED_RESULT_BYTES = 256 * 1024 * 1024
_OWNER_MARKER = ".avalanche-result-store.lock"
_OWNER_MARKER_BYTES = b"avalanche-result-store-v1\n"
_SECURE_ANCHORED_ERROR = (
    "Secure workflow result publication is unavailable: this platform does not "
    "support directory-anchored file operations"
)
_SECURE_STORE_ERROR = (
    "Secure workflow result storage is unavailable: this platform does not "
    "support directory-anchored file operations"
)
logger = logging.getLogger(__name__)


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class ResultPublicationCancelledError(RuntimeError):
    """Raised when cancellation interrupts result publication."""


@dataclass(frozen=True)
class PendingResultBundle:
    """A private per-run directory prepared by the operator parent."""

    name: str
    path: str
    device: int
    inode: int
    descriptor: int


@dataclass(frozen=True)
class StoredWorkflowResult:
    """Opaque handle for immutable result bytes owned by ``ResultStore``."""

    storage_key: str
    manifest_sha256: str
    published_at: float
    byte_size: int


@dataclass(frozen=True)
class _AcceptedWorkflowResult:
    handle: StoredWorkflowResult
    payload: EncodedWorkflowResult


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


class ResultStore:
    """Own private result bundles and validate every storage boundary."""

    def __init__(self, base_directory: str | os.PathLike[str] | None = None) -> None:
        base = None if base_directory is None else os.fspath(base_directory)
        use_dir_fd = all(
            function in os.supports_dir_fd
            for function in (
                os.open,
                os.mkdir,
                os.unlink,
                os.rmdir,
                os.link,
            )
        ) and bool(getattr(os, "O_DIRECTORY", 0) and getattr(os, "O_NOFOLLOW", 0))
        if not use_dir_fd:
            raise RuntimeError(_SECURE_STORE_ERROR)
        if base is not None:
            Path(base).mkdir(parents=True, exist_ok=True)
            _recover_stale_result_roots(Path(base))
        self._root = Path(tempfile.mkdtemp(prefix="operator-results-", dir=base))
        self._root_fd: int | None = None
        self._owner_fd: int | None = None
        try:
            os.chmod(self._root, 0o700)
            self._root_fd = os.open(
                self._root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            self._owner_fd = _create_and_lock_owner_marker(self._root_fd)
        except BaseException:
            if self._root_fd is not None:
                os.close(self._root_fd)
            shutil.rmtree(self._root, ignore_errors=True)
            raise
        self._lock = threading.RLock()
        self._bundle_descriptors: set[int] = set()
        self._accepted_results: dict[str, _AcceptedWorkflowResult] = {}
        self._retained_result_bytes = 0
        self._closed = False

    @property
    def root(self) -> Path:
        return self._root

    def prepare(self) -> PendingResultBundle:
        with self._lock:
            self._ensure_open()
            name = f"bundle_{uuid4().hex}"
            os.mkdir(name, mode=0o700, dir_fd=self._root_fd)
            bundle = self._open_bundle(name)
            metadata = os.fstat(bundle.descriptor)
            self._bundle_descriptors.add(bundle.descriptor)
            return PendingResultBundle(
                name=name,
                path=str(self._root / name),
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
        _validate_sha256(manifest_sha256, "manifest sha256")
        with self._lock:
            self._ensure_open()
            pending_bundle = self._open_retained_bundle(
                pending.descriptor,
                expected_identity=(pending.device, pending.inode),
            )
            try:
                manifest_bytes = _read_regular_file(
                    pending_bundle,
                    "manifest.json",
                    cancel_signal,
                    maximum_bytes=MAX_RESULT_MANIFEST_BYTES,
                )
                if hashlib.sha256(manifest_bytes).hexdigest() != manifest_sha256:
                    raise ValueError("Result manifest sha256 does not match")
                manifest = _decode_manifest(manifest_bytes)
                payload = self._materialize_bundle(
                    pending_bundle,
                    manifest,
                    cancel_signal,
                )
            finally:
                pending_bundle.close()
            byte_size = _encoded_result_byte_size(payload)
            if len(self._accepted_results) >= MAX_RETAINED_RESULTS:
                raise RuntimeError(
                    f"Result store retains at most {MAX_RETAINED_RESULTS} results"
                )
            if self._retained_result_bytes + byte_size > MAX_RETAINED_RESULT_BYTES:
                raise RuntimeError(
                    "Result store retained bytes would exceed " f"{MAX_RETAINED_RESULT_BYTES}"
                )
            storage_key = uuid4().hex
            stored = StoredWorkflowResult(
                storage_key=storage_key,
                manifest_sha256=manifest_sha256,
                published_at=time.monotonic(),
                byte_size=byte_size,
            )
            self._discard_bundle(
                pending.name,
                descriptor=pending.descriptor,
                expected_identity=(pending.device, pending.inode),
            )
            self._accepted_results[storage_key] = _AcceptedWorkflowResult(
                handle=stored,
                payload=payload,
            )
            self._retained_result_bytes += byte_size
            return stored

    def load(
        self,
        stored: StoredWorkflowResult,
        cancel_signal: CancellationSignal | None = None,
    ) -> EncodedWorkflowResult:
        with self._lock:
            self._ensure_open()
            accepted = self._accepted_results.get(stored.storage_key)
            if accepted is None or accepted.handle != stored:
                raise ValueError("Stored result handle is unavailable")
            _check_optional_cancelled(cancel_signal)
            return accepted.payload

    def discard(self, bundle: PendingResultBundle | StoredWorkflowResult) -> None:
        with self._lock:
            if self._closed:
                return
            if isinstance(bundle, StoredWorkflowResult):
                accepted = self._accepted_results.get(bundle.storage_key)
                if accepted is None or accepted.handle != bundle:
                    return
                del self._accepted_results[bundle.storage_key]
                self._retained_result_bytes -= bundle.byte_size
                return
            if bundle.descriptor not in self._bundle_descriptors:
                return
            self._discard_bundle(
                bundle.name,
                descriptor=bundle.descriptor,
                expected_identity=(bundle.device, bundle.inode),
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._accepted_results.clear()
            self._retained_result_bytes = 0
            if self._root_fd is None:
                raise RuntimeError("Result-storage root descriptor is unavailable")
            _remove_open_result_root(
                self._root,
                self._root_fd,
                self._owner_fd,
            )
            for descriptor in self._bundle_descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._bundle_descriptors.clear()
            self._root_fd = None
            self._owner_fd = None

    def _materialize_bundle(
        self,
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

    def _open_bundle(
        self,
        bundle_name: str,
        expected_identity: tuple[int, int] | None = None,
    ) -> _OpenedBundle:
        self._ensure_open()
        if type(bundle_name) is not str or _BUNDLE_NAME.fullmatch(bundle_name) is None:
            raise ValueError("Result bundle descriptor is malformed")
        if self._root_fd is None:
            raise RuntimeError("Result-storage root descriptor is unavailable")
        descriptor = os.open(
            bundle_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=self._root_fd,
        )
        try:
            _validate_bundle_metadata(os.fstat(descriptor), expected_identity)
        except BaseException:
            os.close(descriptor)
            raise
        return _OpenedBundle(descriptor=descriptor)

    def _discard_bundle(
        self,
        bundle_name: str,
        descriptor: int,
        expected_identity: tuple[int, int] | None = None,
    ) -> None:
        if type(bundle_name) is not str or _BUNDLE_NAME.fullmatch(bundle_name) is None:
            raise ValueError("Result bundle descriptor is malformed")
        try:
            self._remove_bundle_directory(
                bundle_name,
                descriptor,
                expected_identity,
            )
        finally:
            if descriptor in self._bundle_descriptors:
                self._bundle_descriptors.remove(descriptor)
                os.close(descriptor)

    def _remove_bundle_directory(
        self,
        bundle_name: str,
        descriptor: int,
        expected_identity: tuple[int, int] | None,
    ) -> None:
        if self._root_fd is None:
            raise RuntimeError("Result-storage root descriptor is unavailable")
        bundle = self._open_retained_bundle(descriptor, expected_identity)
        try:
            os.fchmod(bundle.descriptor, 0o700)
            entries = _bounded_bundle_names(bundle.descriptor)
            for entry_name in entries:
                if _is_directory_at(bundle.descriptor, entry_name):
                    raise ValueError("Result bundle unexpectedly contains a directory")
                os.unlink(entry_name, dir_fd=bundle.descriptor)
            os.fsync(bundle.descriptor)
        finally:
            bundle.close()
        on_root_descriptor = os.open(
            bundle_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=self._root_fd,
        )
        try:
            _validate_bundle_metadata(
                os.fstat(on_root_descriptor),
                expected_identity,
            )
        finally:
            os.close(on_root_descriptor)
        os.rmdir(bundle_name, dir_fd=self._root_fd)
        os.fsync(self._root_fd)

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

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Result store is closed")


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
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
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
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
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
        and bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
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


def _create_and_lock_owner_marker(root_fd: int) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(_OWNER_MARKER, flags, 0o600, dir_fd=root_fd)
    try:
        os.write(descriptor, _OWNER_MARKER_BYTES)
        os.fsync(descriptor)
        _lock_owner_marker(descriptor, blocking=True)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _lock_owner_marker(descriptor: int, *, blocking: bool) -> bool:
    try:
        import fcntl
    except ImportError:
        return False
    operation = fcntl.LOCK_EX
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        fcntl.flock(descriptor, operation)
    except BlockingIOError:
        return False
    return True


def _recover_stale_result_roots(base: Path) -> None:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        return
    base_fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for entry in os.scandir(base_fd):
            if _ROOT_NAME.fullmatch(entry.name) is None:
                continue
            try:
                _recover_stale_result_root(base_fd, entry.name)
            except (OSError, ValueError):
                logger.warning(
                    "Leaving unverified stale result-storage root %s in place",
                    entry.name,
                )
    finally:
        os.close(base_fd)


def _recover_stale_result_root(base_fd: int, root_name: str) -> None:
    root_fd = os.open(
        root_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=base_fd,
    )
    owner_fd: int | None = None
    try:
        _validate_result_root_metadata(os.fstat(root_fd))
        owner_fd = os.open(
            _OWNER_MARKER,
            os.O_RDWR | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        _validate_owner_marker(owner_fd)
        if not _lock_owner_marker(owner_fd, blocking=False):
            return
        _remove_result_root_at(base_fd, root_name, root_fd, owner_fd)
    finally:
        if owner_fd is not None:
            os.close(owner_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _validate_result_root_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Result-storage root is not a directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("Result-storage root permissions are not private")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("Result-storage root has a different owner")


def _validate_owner_marker(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("Result-storage ownership marker is not a private file")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("Result-storage ownership marker permissions are not private")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("Result-storage ownership marker has a different owner")
    if metadata.st_size != len(_OWNER_MARKER_BYTES):
        raise ValueError("Result-storage ownership marker is malformed")
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.read(descriptor, len(_OWNER_MARKER_BYTES) + 1) != _OWNER_MARKER_BYTES:
        raise ValueError("Result-storage ownership marker is malformed")


def _remove_open_result_root(
    root: Path,
    root_fd: int,
    owner_fd: int | None,
) -> None:
    if owner_fd is None:
        raise RuntimeError("Result-storage ownership marker is unavailable")
    parent_fd = os.open(
        root.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        _remove_result_root_at(parent_fd, root.name, root_fd, owner_fd)
    finally:
        os.close(owner_fd)
        os.close(root_fd)
        os.close(parent_fd)


def _remove_result_root_at(
    parent_fd: int,
    root_name: str,
    root_fd: int,
    owner_fd: int,
) -> None:
    opened_identity = os.fstat(root_fd)
    current_fd = os.open(
        root_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        current_identity = os.fstat(current_fd)
        if (current_identity.st_dev, current_identity.st_ino) != (
            opened_identity.st_dev,
            opened_identity.st_ino,
        ):
            raise ValueError("Result-storage root identity changed")
    finally:
        os.close(current_fd)
    for entry in os.scandir(root_fd):
        if entry.name == _OWNER_MARKER:
            continue
        if _BUNDLE_NAME.fullmatch(entry.name) is None or not entry.is_dir(
            follow_symlinks=False
        ):
            raise ValueError("Result-storage root contains an unexpected entry")
        bundle_fd = os.open(
            entry.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            _validate_bundle_metadata(os.fstat(bundle_fd))
            os.fchmod(bundle_fd, 0o700)
            for child in os.scandir(bundle_fd):
                if child.is_dir(follow_symlinks=False):
                    raise ValueError("Result bundle unexpectedly contains a directory")
                os.unlink(child.name, dir_fd=bundle_fd)
        finally:
            os.close(bundle_fd)
        os.rmdir(entry.name, dir_fd=root_fd)
    os.unlink(_OWNER_MARKER, dir_fd=root_fd)
    os.fsync(root_fd)
    os.rmdir(root_name, dir_fd=parent_fd)
