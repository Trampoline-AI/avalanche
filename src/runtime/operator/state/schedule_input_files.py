"""The folder that keeps the uploaded files schedule inputs use, next to the database.

A schedule's input refers to its files as ``ava.File`` values naming a host-local
workspace. Each file is copied out of the operator's temporary upload staging into
its own ``<folder>/<token>/<name>`` here, with its own workspace reference, so the
input keeps working after a restart. The reference registry lives in the system
temporary folder, so :meth:`ScheduleInputFileFolder.restore` registers each kept
file again when the operator starts. Files stay until the schedule input that
uses them is replaced.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from avalanche.file import File
from avalanche.references import local_workspace, restore_local_workspace

from ..input_files import (
    InputFileStaging,
    InputFileStorageError,
    StagedInputFileError,
    is_plain_file_name,
)
from .errors import StateStoreError
from .store import OperatorStateStore

SCHEDULE_INPUT_FOLDER_NAME = "schedule-inputs"

_logger = logging.getLogger(__name__)


class ScheduleInputFile(BaseModel):
    """One file a schedule input uses: ``<folder>/<name>`` under the schedule input folder."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    folder: str = Field(pattern=r"^[0-9a-f]{32}$")
    name: str
    workspace_reference: str = Field(pattern=r"^local-ws:v1:[0-9a-f]{32}$")

    @field_validator("name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        if not is_plain_file_name(value):
            raise ValueError(f"Schedule input file name {value!r} is not a plain file name")
        return value


class ScheduleInputFiles(BaseModel):
    """The JSON form of the files one schedule input uses."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    files: tuple[ScheduleInputFile, ...]


class ScheduleInputFileFolder:
    """Own the schedule input folder: copy files in, register them, remove replaced ones."""

    def __init__(self, root: Path, *, temporary: bool) -> None:
        self._root = root.resolve(strict=True)
        self._temporary = temporary

    @classmethod
    def for_store(cls, store: OperatorStateStore) -> ScheduleInputFileFolder:
        """The folder next to the store's database, or a throwaway one for ``:memory:``."""
        if store.path is None:
            root = Path(tempfile.mkdtemp(prefix="avalanche-schedule-inputs-"))
            return cls(root, temporary=True)
        root = store.path.parent / SCHEDULE_INPUT_FOLDER_NAME
        try:
            root.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise StateStoreError(
                f"Could not create the schedule input folder {root}: {exc}"
            ) from exc
        return cls(root, temporary=False)

    @property
    def root(self) -> Path:
        return self._root

    @contextmanager
    def claims(self, staging: InputFileStaging) -> Iterator[ScheduleInputFileClaims]:
        """Copy one schedule request's files, removing them unless the schedule keeps them."""
        claims = ScheduleInputFileClaims(self, staging)
        try:
            yield claims
        finally:
            claims.discard_unkept()

    def restore(self, workflow_id: str, input_file: ScheduleInputFile) -> None:
        """Register a kept file's workspace again, as when the operator starts."""
        folder = self._root / input_file.folder
        if not (folder / input_file.name).is_file():
            raise StateStoreError(
                f"Schedule input file {folder / input_file.name} of workflow {workflow_id} "
                "is missing. Restore it, or pass a different --state-db."
            )
        try:
            restore_local_workspace(input_file.workspace_reference, folder)
        except (OSError, ValueError) as exc:
            raise StateStoreError(
                f"Could not register schedule input file {folder / input_file.name} of "
                f"workflow {workflow_id}: {exc}"
            ) from exc

    def remove(self, input_files: Iterable[ScheduleInputFile]) -> None:
        """Remove files no stored schedule input uses any more.

        Their input is already replaced, so this is housekeeping: a folder that
        cannot be removed is logged and left behind rather than failing the change.
        """
        for input_file in input_files:
            folder = self._root / input_file.folder
            try:
                shutil.rmtree(folder)
            except FileNotFoundError:
                pass
            except OSError:
                _logger.warning(
                    "Could not remove schedule input file %s", folder, exc_info=True
                )

    def close(self) -> None:
        """Remove a throwaway folder; a folder next to a database file stays."""
        if self._temporary:
            shutil.rmtree(self._root)

    def new_folder(self) -> tuple[str, Path]:
        """Create an empty ``<token>`` folder for one file and return both."""
        token = uuid4().hex
        folder = self._root / token
        try:
            folder.mkdir(mode=0o700)
        except OSError as exc:
            raise InputFileStorageError(
                f"Could not create schedule input folder {folder}: {exc}"
            ) from exc
        return token, folder


class ScheduleInputFileClaims:
    """The files one schedule request copies; see ``ScheduleInputFileFolder.claims``."""

    def __init__(self, folder: ScheduleInputFileFolder, staging: InputFileStaging) -> None:
        self._folder = folder
        self._staging = staging
        self._files: list[ScheduleInputFile] = []
        self._kept = False

    @property
    def files(self) -> tuple[ScheduleInputFile, ...]:
        return tuple(self._files)

    def file(self, object_key: str, *, sha256: str, size_bytes: int) -> File:
        """Copy a staged file into the schedule input folder and build its ``ava.File``."""
        token, folder = self._folder.new_folder()
        try:
            staged = self._staging.copy(
                object_key, sha256=sha256, size_bytes=size_bytes, folder=folder
            )
            try:
                workspace = local_workspace(folder)
                # An empty media type is how uploads say the type is unknown.
                file = File.from_path(
                    folder / staged.name,
                    content_type=staged.media_type or None,
                    workspace=workspace,
                )
            except OSError as exc:
                failure = InputFileStorageError(
                    f"Could not store schedule input file {object_key!r}: {exc}"
                )
                raise failure from exc
        except BaseException as failure:
            try:
                shutil.rmtree(folder)
            except OSError as cleanup:
                failure.add_note(
                    f"Removing the partial schedule input file also failed: {cleanup}"
                )
            raise
        self._files.append(
            ScheduleInputFile(
                folder=token, name=staged.name, workspace_reference=file.ref.workspace
            )
        )
        return file

    def inline(self, name: str, media_type: str, content: bytes, sha256: str) -> File:
        """Stage an inline body like any upload, then copy it for the schedule."""
        staged = self._staging.stage(name, media_type, content)
        # Hex digits compare case-insensitively, as inline bodies always have.
        if sha256 and sha256.lower() != staged.sha256:
            raise StagedInputFileError(f"Input file {name!r} does not match its sha256")
        return self.file(staged.object_key, sha256=staged.sha256, size_bytes=staged.size_bytes)

    def keep(self) -> None:
        """The schedule's stored input now uses these files; never remove them here."""
        self._kept = True

    def discard_unkept(self) -> None:
        if not self._kept:
            self._folder.remove(self._files)
            self._files.clear()
