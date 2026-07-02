"""
Progress tracking for streaming with per-snapshot state management.

ProgressStore manages:
- Per-snapshot state (pending, started, done, failed, quarantined)
- Cursor position (highest contiguous done snapshot)
- Lease management for concurrent processing
- Automatic retry with quarantine
- CAS operations for atomic updates

Storage (all under `avalanche.stream.<key>.*`):
- Snapshot state: `avalanche.stream.<key>.<snapshot_id>` (JSON, <4KB)
- Cursor: `avalanche.stream.<key>.cursor` (snapshot_id as string)
"""

import json
import re
import time
import uuid
from typing import TYPE_CHECKING, Literal, overload

from pyiceberg.exceptions import CommitFailedException

from .types import SnapshotMetadata, SnapshotState

# Regex for valid keys: snake_case
# (starts with lowercase letter, contains only a-z, 0-9, _)
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

if TYPE_CHECKING:
    from pyiceberg.table import Table

    from .iceberg import IcebergTable

# Default configuration
DEFAULT_LEASE_TTL_SECONDS = 600  # 10 minutes
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_CAS_RETRIES = 3  # Number of CAS retry attempts
DEFAULT_MAX_DONE_HISTORY = 10  # Keep last N done snapshots behind cursor


class ProgressStore:
    """
    Property-backed storage for cursor and per-snapshot state.

    Manages progress tracking for streaming workflows using table metadata properties.
    Supports concurrent processing with leases and automatic retry with quarantine.

    Can be used as a context manager for automatic claim/mark lifecycle:

    Example (single snapshot, context manager):
        with ProgressStore(table, key="docs_to_chunks") as snapshot_id:
            if snapshot_id:
                # Process data
                data = read_snapshot(snapshot_id)
                process(data)
                # Automatically marks done on success, failed on exception
                # Automatically advances cursor

    Example (batch, context manager - claim all pending):
        with ProgressStore(table, key="docs_to_chunks", claim_n=-1) as snapshot_ids:
            for sid in snapshot_ids:
                process_snapshot(sid)
                # All automatically marked done on success

    Example (batch, context manager - claim up to 5):
        with ProgressStore(table, key="docs_to_chunks", claim_n=5) as snapshot_ids:
            for sid in snapshot_ids:
                process_snapshot(sid)

    Example (manual):
        store = ProgressStore(table, key="docs_to_chunks")
        snapshot_id = store.claim_next_pending()
        if snapshot_id:
            try:
                data = read_snapshot(snapshot_id)
                process(data)
                store.mark_done(snapshot_id)
                store.advance_cursor()
            except Exception as e:
                store.mark_failed(snapshot_id, error=str(e))

    Example (manual batch):
        store = ProgressStore(table, key="docs_to_chunks")
        snapshot_ids = store.claim_next_pending(n=-1)  # Claim all
        try:
            for sid in snapshot_ids:
                process_snapshot(sid)
            store.mark_done(snapshot_ids)
            store.advance_cursor()
        except Exception as e:
            store.mark_failed(snapshot_ids, error=str(e))

    Methods:
        claim(): Claim a specific snapshot for processing
        claim_next_pending(): Claim pending snapshot(s) for processing
        mark_done(): Mark snapshot(s) as successfully processed
        mark_failed(): Mark snapshot(s) as failed
        advance_cursor(): Update cursor to highest contiguous done snapshot
        list_pending(): List snapshots that need processing
        get_cursor(): Get current cursor position
    """

    def __init__(
        self,
        table: "IcebergTable | Table",
        *,
        key: str,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_done_history: int = DEFAULT_MAX_DONE_HISTORY,
        worker_id: str | None = None,
        claim_n: int = 1,
    ):
        """
        Initialize ProgressStore.

        Args:
            table: Storage table to track progress for
            key: Unique key for this stream (e.g., "docs_to_chunks")
            lease_ttl_seconds: How long a lease is valid before expiring
            max_attempts: Maximum attempts before quarantining a snapshot
            max_done_history: Keep only last N done snapshots behind cursor
            worker_id: ID of this worker (auto-generated if None)
            claim_n: Number of snapshots to claim when used as context manager.
                     1 (default): Claim single snapshot, returns int | None
                     >1: Claim up to n snapshots, returns list[int]
                     -1: Claim all pending snapshots, returns list[int]
        """
        if not _KEY_PATTERN.match(key):
            raise ValueError(
                f"Invalid key: {key!r}. "
                "Must be snake_case (lowercase letter followed by letters/numbers/underscores)"
            )

        self.table = table
        self.key = key
        self.lease_ttl_seconds = lease_ttl_seconds
        self.max_attempts = max_attempts
        self.max_done_history = max_done_history
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.claim_n = claim_n

    def _snapshot_property_key(self, snapshot_id: int) -> str:
        """Get property key for a snapshot."""
        return f"avalanche.stream.{self.key}.{snapshot_id}"

    def _cursor_property_key(self) -> str:
        """Get property key for the cursor."""
        return f"avalanche.stream.{self.key}.cursor"

    def _get_snapshot_metadata(self, snapshot_id: int) -> SnapshotMetadata | None:
        """Get metadata for a snapshot, or None if not tracked."""
        key = self._snapshot_property_key(snapshot_id)
        value = self.table.properties.get(key)
        if not value:
            return None
        return SnapshotMetadata.from_dict(json.loads(value))

    def _is_claimable(self, meta: SnapshotMetadata, now: int) -> bool:
        """Check if a snapshot with given metadata is claimable."""
        return meta.state in (SnapshotState.PENDING, SnapshotState.FAILED) or (
            meta.state == SnapshotState.STARTED and (meta.lease_expires_at or 0) <= now
        )

    def _prune_history(self, cursor_idx: int, snapshots: list[int]) -> list[str]:
        """
        Get property keys to prune for DONE snapshots behind cursor.

        Returns keys exceeding max_done_history limit.
        """
        if self.max_done_history <= 0:
            return []

        done_snapshots = []
        for sid in snapshots[: cursor_idx + 1]:
            meta = self._get_snapshot_metadata(sid)
            if meta and meta.state == SnapshotState.DONE:
                done_snapshots.append(sid)

        if len(done_snapshots) > self.max_done_history:
            to_remove = done_snapshots[: -self.max_done_history]
            return [self._snapshot_property_key(sid) for sid in to_remove]

        return []

    def get_cursor(self) -> int | None:
        """
        Get current cursor position.

        Returns:
            Snapshot ID of cursor, or None if not set
        """
        cursor_key = self._cursor_property_key()
        value = self.table.properties.get(cursor_key)
        return int(value) if value else None

    def list_pending(self) -> list[int]:
        """
        List snapshots that need processing.

        Returns snapshots that are:
        - Not yet tracked (pending by default)
        - In 'pending' state
        - In 'started' state with expired lease
        - In 'failed' state with attempts < max_attempts

        Returns:
            List of snapshot IDs that need processing, in chronological order
        """
        cursor = self.get_cursor()
        current_time = int(time.time())
        pending_snapshots = []

        # Get all snapshots in chronological order (by timestamp)
        all_snapshots = list(self.table.history())
        # Sort by timestamp_ms to get chronological order (oldest first)
        all_snapshots_sorted = sorted(all_snapshots, key=lambda s: s.timestamp_ms)
        snapshots = [s.snapshot_id for s in all_snapshots_sorted]

        # Find cursor position
        start_idx = 0
        if cursor:
            try:
                start_idx = snapshots.index(cursor) + 1
            except ValueError:
                # Cursor not in history, start from beginning
                start_idx = 0

        # Check snapshots that come after cursor
        for snapshot_id in snapshots[start_idx:]:
            metadata = self._get_snapshot_metadata(snapshot_id)

            # Not tracked yet - treat as pending
            if metadata is None:
                pending_snapshots.append(snapshot_id)
                continue

            # Check state
            if metadata.state == SnapshotState.PENDING:
                pending_snapshots.append(snapshot_id)
            elif metadata.state == SnapshotState.STARTED:
                # Check if lease expired
                if metadata.lease_expires_at and metadata.lease_expires_at < current_time:
                    pending_snapshots.append(snapshot_id)
            elif metadata.state == SnapshotState.FAILED:
                # Retry if not exceeded max attempts
                if metadata.attempt < self.max_attempts:
                    pending_snapshots.append(snapshot_id)
            # Skip DONE and QUARANTINED

        # Already in chronological order from the sorted history iteration
        return pending_snapshots

    def claim(self, snapshot_id: int) -> int:
        """
        Claim a specific snapshot for processing with CAS semantics.

        Atomically sets state to STARTED with a lease using CAS to prevent
        double-claiming by concurrent workers.

        Args:
            snapshot_id: Specific snapshot ID to claim

        Returns:
            The claimed snapshot ID (same as input)

        Raises:
            RuntimeError: If snapshot cannot be claimed (already claimed or done)

        Note:
            Uses transactional CAS with retries on commit conflicts.
        """
        last_error: Exception | None = None

        for _ in range(DEFAULT_CAS_RETRIES):
            try:
                with self.table.transaction() as tx:
                    now = int(time.time())
                    meta = self._get_snapshot_metadata(snapshot_id)

                    if meta and not self._is_claimable(meta, now):
                        raise RuntimeError(
                            f"Snapshot {snapshot_id} cannot be claimed - "
                            f"current state: {meta.state.value}"
                        )

                    started = SnapshotMetadata(
                        state=SnapshotState.STARTED,
                        lease_expires_at=now + self.lease_ttl_seconds,
                        attempt=(meta.attempt if meta else 0) + 1,
                        worker_id=self.worker_id,
                        last_error=meta.last_error if meta else None,
                    )
                    key = self._snapshot_property_key(snapshot_id)
                    tx.set_properties(**{key: json.dumps(started.to_dict())})

                return snapshot_id

            except CommitFailedException as e:
                last_error = e
                self.table.refresh()

        raise RuntimeError(
            f"Snapshot {snapshot_id} could not be claimed after {DEFAULT_CAS_RETRIES} retries"
        ) from last_error

    @overload
    def claim_next_pending(self) -> int | None: ...

    @overload
    def claim_next_pending(self, n: Literal[1]) -> int | None: ...

    @overload
    def claim_next_pending(self, n: int) -> list[int]: ...

    def claim_next_pending(self, n: int = 1) -> int | list[int] | None:
        """
        Claim pending snapshots with CAS semantics.

        Atomically sets state to STARTED with a lease using CAS to prevent
        double-claiming by concurrent workers.

        Args:
            n: Number of snapshots to claim.
               - 1 (default): Claim single snapshot, returns int | None
               - >1: Claim up to n snapshots, returns list[int]
               - -1: Claim all pending snapshots, returns list[int]

        Returns:
            - When n=1: Snapshot ID that was claimed, or None if no pending
            - When n>1 or -1: List of claimed snapshot IDs (may be empty)

        Note:
            Uses transactional CAS with retries. Moves to next candidate on
            persistent conflicts or state changes.
        """
        for _ in range(DEFAULT_CAS_RETRIES):
            candidates = self.list_pending()
            if not candidates:
                return None if n == 1 else []

            to_claim = candidates if n == -1 else candidates[:n]

            try:
                with self.table.transaction() as tx:
                    now = int(time.time())
                    claimed: list[int] = []
                    props: dict[str, str] = {}

                    for sid in to_claim:
                        meta = self._get_snapshot_metadata(sid)
                        started = SnapshotMetadata(
                            state=SnapshotState.STARTED,
                            lease_expires_at=now + self.lease_ttl_seconds,
                            attempt=(meta.attempt if meta else 0) + 1,
                            worker_id=self.worker_id,
                            last_error=meta.last_error if meta else None,
                        )
                        props[self._snapshot_property_key(sid)] = json.dumps(started.to_dict())
                        claimed.append(sid)

                    if props:
                        tx.set_properties(**props)

                return (claimed[0] if claimed else None) if n == 1 else claimed

            except CommitFailedException:
                self.table.refresh()

        return None if n == 1 else []

    def mark_done(self, snapshot_ids: int | list[int]) -> None:
        """
        Mark snapshot(s) as successfully processed.

        Args:
            snapshot_ids: Single snapshot ID or list of snapshot IDs

        Note:
            Uses transaction for atomicity. Re-reads current metadata within
            transaction to preserve attempt count and worker_id.
        """
        ids = [snapshot_ids] if isinstance(snapshot_ids, int) else snapshot_ids
        if not ids:
            return

        for _ in range(DEFAULT_CAS_RETRIES):
            try:
                with self.table.transaction() as tx:
                    props = {}
                    for snapshot_id in ids:
                        meta = self._get_snapshot_metadata(snapshot_id)

                        # CAS: only mark done if we still own the lease
                        if not meta or meta.worker_id != self.worker_id:
                            continue

                        done = SnapshotMetadata(
                            state=SnapshotState.DONE,
                            attempt=meta.attempt,
                            worker_id=meta.worker_id,
                            lease_expires_at=None,
                            last_error=None,
                        )
                        key = self._snapshot_property_key(snapshot_id)
                        props[key] = json.dumps(done.to_dict())

                    if props:
                        tx.set_properties(**props)
                return
            except CommitFailedException:
                self.table.refresh()

    def mark_failed(self, snapshot_ids: int | list[int], error: str | None = None) -> None:
        """
        Mark snapshot(s) as failed.

        If attempts >= max_attempts, quarantines the snapshot.

        Args:
            snapshot_ids: Single snapshot ID or list of snapshot IDs
            error: Error message describing the failure

        Note:
            Uses transaction for atomicity with retries. Re-reads current metadata
            to check attempt count for quarantining.
        """
        ids = [snapshot_ids] if isinstance(snapshot_ids, int) else snapshot_ids
        if not ids:
            return

        for _ in range(DEFAULT_CAS_RETRIES):
            try:
                with self.table.transaction() as tx:
                    props = {}
                    for snapshot_id in ids:
                        meta = self._get_snapshot_metadata(snapshot_id)

                        # CAS: only mark failed if we still own the lease
                        if not meta or meta.worker_id != self.worker_id:
                            continue

                        new_state = (
                            SnapshotState.QUARANTINED
                            if meta.attempt >= self.max_attempts
                            else SnapshotState.FAILED
                        )
                        failed = SnapshotMetadata(
                            state=new_state,
                            attempt=meta.attempt,
                            worker_id=meta.worker_id,
                            lease_expires_at=None,
                            last_error=error,
                        )
                        key = self._snapshot_property_key(snapshot_id)
                        props[key] = json.dumps(failed.to_dict())

                    if props:
                        tx.set_properties(**props)
                return
            except CommitFailedException:
                self.table.refresh()

    def advance_cursor(self) -> int | None:
        """
        Advance cursor to highest contiguous done snapshot.

        Scans from current cursor forward, finding the longest sequence
        of done snapshots. Updates cursor to the end of that sequence.

        Returns:
            New cursor position, or None if no advance was made

        Example:
            Snapshots: [1, 2, 3, 4, 5]
            States: [done, done, done, started, done]
            Cursor advances to 3 (highest contiguous done)

        Note:
            Uses transactional CAS - re-reads cursor within transaction to ensure
            cursor only moves forward, never backwards.
        """
        for _ in range(DEFAULT_CAS_RETRIES):
            all_snapshots = list(self.table.history())
            all_snapshots_sorted = sorted(all_snapshots, key=lambda s: s.timestamp_ms)
            snapshots = [s.snapshot_id for s in all_snapshots_sorted]

            if not snapshots:
                return None

            try:
                with self.table.transaction() as tx:
                    cursor_key = self._cursor_property_key()
                    cur_val = self.table.properties.get(cursor_key)
                    cursor = int(cur_val) if cur_val else None

                    cur_idx = (
                        snapshots.index(cursor)
                        if (cursor is not None and cursor in snapshots)
                        else -1
                    )

                    new_cursor = cursor
                    new_cursor_idx = cur_idx
                    terminal_states = (SnapshotState.DONE, SnapshotState.QUARANTINED)
                    for i, snapshot_id in enumerate(snapshots[cur_idx + 1 :]):
                        metadata = self._get_snapshot_metadata(snapshot_id)
                        if metadata and metadata.state in terminal_states:
                            new_cursor = snapshot_id
                            new_cursor_idx = cur_idx + 1 + i
                        else:
                            break

                    if new_cursor is not None and new_cursor != cursor:
                        tx.set_properties(**{cursor_key: str(new_cursor)})
                        keys_to_prune = self._prune_history(new_cursor_idx, snapshots)
                        if keys_to_prune:
                            tx.remove_properties(*keys_to_prune)
                    else:
                        return None

                return new_cursor

            except CommitFailedException:
                self.table.refresh()

        return None

    def reset(self) -> None:
        """
        Reset all progress tracking (for testing/debugging).

        Removes all snapshot metadata and cursor.
        """
        for _ in range(DEFAULT_CAS_RETRIES):
            self.table.refresh()
            cursor_key = self._cursor_property_key()
            properties_to_remove = []

            for prop_key in self.table.properties.keys():
                if prop_key == cursor_key or prop_key.startswith(
                    f"avalanche.stream.{self.key}."
                ):
                    properties_to_remove.append(prop_key)

            if not properties_to_remove:
                return  # Nothing to reset

            try:
                with self.table.transaction() as tx:
                    tx.remove_properties(*properties_to_remove)
                return
            except CommitFailedException:
                pass  # Retry with fresh state

    def __enter__(self) -> int | list[int] | None:
        """
        Enter context manager - claims pending snapshot(s) for processing.

        The number of snapshots claimed is controlled by the `claim_n` parameter
        passed to the constructor.

        Returns:
            - When claim_n=1: Snapshot ID that was claimed, or None if nothing to process
            - When claim_n>1 or -1: List of claimed snapshot IDs (may be empty)

        Example (single snapshot):
            with ProgressStore(table, key="my_stream") as snapshot_id:
                if snapshot_id:
                    process_snapshot(snapshot_id)
                    # Automatically marks done on success

        Example (batch):
            with ProgressStore(table, key="my_stream", claim_n=-1) as snapshot_ids:
                for sid in snapshot_ids:
                    process_snapshot(sid)
                    # All automatically marked done on success
        """
        self._claimed_snapshot_ids = self.claim_next_pending(n=self.claim_n)
        return self._claimed_snapshot_ids

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Exit context manager - marks snapshot(s) as done/failed and advances cursor.

        Args:
            exc_type: Exception type if an exception occurred
            exc_val: Exception value if an exception occurred
            exc_tb: Exception traceback if an exception occurred

        Returns:
            False (exceptions are not suppressed)
        """
        claimed = self._claimed_snapshot_ids

        ids = [claimed] if isinstance(claimed, int) else (claimed or [])

        if not ids:
            # Nothing was claimed, nothing to mark
            return False

        if exc_type is None:
            # Success - mark done and advance cursor
            self.mark_done(ids)
            self.advance_cursor()
        else:
            # Failure - mark failed with error message
            error_msg = f"{exc_type.__name__}: {exc_val}"
            self.mark_failed(ids, error=error_msg)

        # Clean up
        self._claimed_snapshot_ids = None

        # Don't suppress the exception
        return False
