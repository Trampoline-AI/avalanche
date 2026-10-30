"""Run input files staged on the operator host before a run or schedule uses them."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from avalanche.file import File

MAX_STAGED_INPUT_FILE_BYTES = 32 * 1024 * 1024
STAGED_INPUT_FILE_TTL_SECONDS = 24 * 60 * 60
_MAX_NAME_BYTES = 255
_MAX_MEDIA_TYPE_CHARS = 255
_TOKEN_CHARS = frozenset("0123456789abcdef")


class StagedInputFileError(ValueError):
    """A staged input file request or reference the operator cannot accept."""


class InputFileStorageError(RuntimeError):
    """The operator could not store a staged input file, or its staging folder is closed."""


@dataclass(frozen=True)
class StagedInputFile:
    """One staged file, named by the object key StartRun and the schedule refer to."""

    object_key: str
    name: str
    media_type: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class InputFileFields:
    """The input fields a workflow declares, by whether each holds one file or a list."""

    declared: frozenset[str]
    lists: frozenset[str]


@dataclass
class _Entry:
    file: StagedInputFile
    path: Path
    staged_at: float
    # Requests that use the file; a used file stays until the operator closes.
    claims: int = 0


def _is_plain_name(name: str) -> bool:
    return not (
        not name
        or name in {".", ".."}
        or any(character in name for character in "/\\\x00")
        or len(name.encode("utf-8")) > _MAX_NAME_BYTES
    )


class InputFileStaging:
    """A private folder of uploaded input files, addressed only by keys it issued.

    Each file lives in its own folder, ``<token>/<name>``, so the ``ava.File`` a run
    receives keeps the uploaded name and grants access to that one folder. Files no
    run or schedule used expire after ``ttl_seconds``; the rest stay until ``close``.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        ttl_seconds: float = STAGED_INPUT_FILE_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._root = (
            Path(tempfile.mkdtemp(prefix="avalanche-input-files-")) if root is None else root
        ).resolve(strict=True)
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._closed = False

    @property
    def root(self) -> Path:
        return self._root

    def stage(self, name: str, media_type: str, content: bytes) -> StagedInputFile:
        """Save one upload and return the key that refers to it."""
        if not _is_plain_name(name):
            raise StagedInputFileError(
                f"Input file name {name!r} must be a plain file name of at most "
                f"{_MAX_NAME_BYTES} bytes"
            )
        if len(media_type) > _MAX_MEDIA_TYPE_CHARS:
            raise StagedInputFileError(
                f"Input file media type exceeds {_MAX_MEDIA_TYPE_CHARS} characters"
            )
        if len(content) > MAX_STAGED_INPUT_FILE_BYTES:
            raise StagedInputFileError(
                f"Input file exceeds the {MAX_STAGED_INPUT_FILE_BYTES} byte limit"
            )
        with self._lock:
            if self._closed:
                raise InputFileStorageError("Input file staging is closed")
        self.prune()
        token = uuid4().hex
        folder = self._root / token
        path = folder / name
        try:
            folder.mkdir(mode=0o700)
            path.write_bytes(content)
        except OSError as exc:
            failure = InputFileStorageError(f"Could not store input file {name!r}: {exc}")
            try:
                shutil.rmtree(folder)
            except FileNotFoundError:
                pass
            except OSError as cleanup:
                failure.add_note(f"Removing the partial upload also failed: {cleanup}")
            raise failure from exc
        staged = StagedInputFile(
            object_key=f"{token}/{name}",
            name=name,
            media_type=media_type,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )
        with self._lock:
            self._entries[staged.object_key] = _Entry(
                file=staged, path=path, staged_at=self._clock()
            )
        return staged

    @contextmanager
    def claims(self) -> Iterator[InputFileClaims]:
        """Claim files for one run or schedule request, releasing them if it fails.

        A released file expires like any unused one, so a refused request does not
        keep its files until the operator closes.
        """
        claims = InputFileClaims(self)
        try:
            yield claims
        except BaseException:
            claims.release()
            raise

    def claim(self, object_key: str, *, sha256: str, size_bytes: int) -> File:
        """Count one more use of a staged file and build its ``ava.File``.

        Prefer ``claims``, which releases the use again when its request fails.
        """
        self._validate_key(object_key)
        with self._lock:
            entry = self._entries.get(object_key)
            if entry is None:
                raise StagedInputFileError(
                    f"Input file {object_key!r} was not staged by this operator or has expired"
                )
            if (sha256.lower(), size_bytes) != (entry.file.sha256, entry.file.size_bytes):
                raise StagedInputFileError(
                    f"Input file {object_key!r} does not match its staged sha256 and size"
                )
        try:
            # An empty media type is how uploads say the type is unknown.
            file = File.from_path(entry.path, content_type=entry.file.media_type or None)
        except OSError as exc:
            failure = InputFileStorageError(f"Could not read input file {object_key!r}: {exc}")
            raise failure from exc
        with self._lock:
            # Count the use only once the file is built, so a failure leaves no use behind.
            if self._entries.get(object_key) is not entry:
                raise InputFileStorageError(f"Input file {object_key!r} was removed")
            entry.claims += 1
        return file

    def release(self, object_keys: Iterable[str]) -> None:
        """Undo one use per key, so a file no request uses expires like an unused one."""
        with self._lock:
            for key in object_keys:
                entry = self._entries.get(key)
                # Closing the staging folder already dropped every entry.
                if entry is not None:
                    entry.claims -= 1

    def prune(self) -> None:
        """Remove staged files that expired before any run or schedule used them."""
        cutoff = self._clock() - self._ttl_seconds
        with self._lock:
            expired = [
                key
                for key, entry in self._entries.items()
                if entry.claims == 0 and entry.staged_at <= cutoff
            ]
            folders = [self._entries.pop(key).path.parent for key in expired]
        for folder in folders:
            shutil.rmtree(folder)

    def close(self) -> None:
        """Remove the staging folder and every file in it."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._entries.clear()
        shutil.rmtree(self._root)

    def _validate_key(self, object_key: str) -> None:
        token, separator, name = object_key.partition("/")
        if (
            not separator
            or len(token) != 32
            or not set(token) <= _TOKEN_CHARS
            or not _is_plain_name(name)
        ):
            raise StagedInputFileError(f"Malformed input file key {object_key!r}")
        path = (self._root / token / name).resolve()
        if path.parent.parent != self._root:
            raise StagedInputFileError(
                f"Input file key {object_key!r} points outside the staging folder"
            )


class InputFileClaims:
    """The files one run or schedule request refers to; see ``InputFileStaging.claims``."""

    def __init__(self, staging: InputFileStaging) -> None:
        self._staging = staging
        # One entry per use this request counted, so a release undoes only those.
        self._keys: list[str] = []

    def file(self, object_key: str, *, sha256: str, size_bytes: int) -> File:
        """Build the ``ava.File`` for a staged key; its digest and size must match."""
        file = self._staging.claim(object_key, sha256=sha256, size_bytes=size_bytes)
        self._keys.append(object_key)
        return file

    def release(self) -> None:
        """Undo every use this request counted."""
        keys, self._keys = self._keys, []
        self._staging.release(keys)

    def inline(self, name: str, media_type: str, content: bytes, sha256: str) -> File:
        """Stage an inline body, so it is path-backed like any other input file."""
        staged = self._staging.stage(name, media_type, content)
        # Hex digits compare case-insensitively, as inline bodies always have.
        if sha256 and sha256.lower() != staged.sha256:
            raise StagedInputFileError(f"Input file {name!r} does not match its sha256")
        return self.file(staged.object_key, sha256=staged.sha256, size_bytes=staged.size_bytes)
