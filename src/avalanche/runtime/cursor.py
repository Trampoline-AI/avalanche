"""
Cursor for tracking state between workflow runs.
"""

import re
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:

    class CursorTable(Protocol):
        @property
        def properties(self) -> dict[str, str]: ...

        def transaction(self) -> Any: ...

# Regex for valid cursor keys: snake_case
# (starts with lowercase letter, contains only a-z, 0-9, _)
_CURSOR_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class _CursorTransaction:
    """
    Transaction context manager for Cursor operations.

    Manages the transaction lifecycle and enables cursor.set() within context.
    """

    def __init__(self, cursor: "Cursor"):
        self.cursor = cursor
        self._tx = None

    def __enter__(self):
        """Begin transaction and mark cursor as active."""
        self._tx = self.cursor.table.transaction()
        self._tx.__enter__()
        # Mark cursor as in transaction
        self.cursor._active_tx = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Commit transaction and cleanup."""
        try:
            return self._tx.__exit__(exc_type, exc_val, exc_tb)
        finally:
            # Clean up cursor state
            if hasattr(self.cursor, "_active_tx"):
                delattr(self.cursor, "_active_tx")

    def append(self, data: Any) -> Any:
        """Append rows through the cursor transaction using table write semantics."""
        table = self.cursor.table
        arrow_data = data
        if getattr(table, "row_lineage", False):
            from avalanche.lineage import add_row_lineage_to_data
            from avalanche.runtime import get_current_run_context

            arrow_data = add_row_lineage_to_data(
                arrow_data,
                context=get_current_run_context(),
            )

        cast_to_schema = getattr(table, "_cast_to_table_schema", None)
        if cast_to_schema is not None:
            arrow_data = cast_to_schema(arrow_data)
        else:
            schema = getattr(table, "schema", None)
            if schema is not None:
                arrow_data = arrow_data.cast(schema)

        assert self._tx is not None
        return self._tx.append(arrow_data)

    def set_properties(self, **kwargs: Any) -> None:
        """Set metadata properties on the underlying transaction."""
        assert self._tx is not None
        self._tx.set_properties(**kwargs)

    def __getattr__(self, name: str) -> Any:
        assert self._tx is not None
        return getattr(self._tx, name)


class Cursor:
    """
    Cursor for tracking state between workflow runs.

    Cursors store arbitrary state (snapshot IDs, timestamps, last processed ID, etc.)
    as properties on the table. They provide simple get/set interface for checkpoint
    management.

    The cursor state is stored in the table's metadata properties under the key:
    `avalanche.cursor.{key}`

    Example:
        @ava.source
        def load_incremental(
            *,
            cursor=ava.Cursor(ns().documents, key="last_id"),
            docs=ns().documents,
        ):
            with cursor.transaction() as tx:
                last_id = cursor.get() or 0
                new_data = fetch_data(after_id=last_id)
                tx.append(new_data.to_arrow())
                cursor.set(new_data["id"].max())

    Attributes:
        table: Table to track state for
        key: Unique key to identify this cursor (enables tracking multiple
            checkpoints per table, e.g., 'last_processed_id', 'checkpoint_alpha')
    """

    table: "CursorTable"
    """Table to track state for."""

    key: str
    """Unique key name (enables multiple independent cursors per table)."""

    def __init__(self, table: "CursorTable", *, key: str):
        """
        Initialize a cursor.

        Args:
            table: Storage table to track cursor for
            key: Cursor key name (required, snake_case: lowercase letter
                followed by letters/numbers/underscores)

        Raises:
            ValueError: If key is invalid (not snake_case)

        Example:
            cursor = Cursor(ns.documents, key="last_processed_id")
        """
        if not _CURSOR_KEY_PATTERN.match(key):
            raise ValueError(
                f"Invalid cursor key: {key!r}. "
                "Must be snake_case (lowercase letter followed by letters/numbers/underscores)"
            )

        self.table = table
        self.key = key

    @property
    def _property_key(self) -> str:
        """Get the full property key for this cursor."""
        return f"avalanche.cursor.{self.key}"

    def get(self) -> Any | None:
        """
        Get the current cursor value.

        Returns:
            The cursor value (typically snapshot ID or timestamp), or None if not set

        Example:
            last_id = cursor.get() or 0
        """
        return self.table.properties.get(self._property_key)

    def set(self, value: Any) -> None:
        """
        Set the cursor value.

        Must be called within a transaction context.

        Args:
            value: Value to store (typically snapshot ID or timestamp)

        Raises:
            RuntimeError: If not called within transaction context

        Example:
            with cursor.transaction() as tx:
                # ... process data ...
                cursor.set(new_snapshot_id)
        """
        if not hasattr(self, "_active_tx"):
            raise RuntimeError(
                "cursor.set() must be called within cursor.transaction() context"
            )

        # Update the transaction's property updates
        self._active_tx.set_properties(**{self._property_key: str(value)})

    def transaction(self) -> "_CursorTransaction":
        """
        Create a transaction context for cursor operations.

        Returns:
            Transaction context manager that enables cursor.set()

        Example:
            with cursor.transaction() as tx:
                new_data = fetch_new_data()
                tx.append(new_data.to_arrow())
                cursor.set(new_snapshot_id)
        """
        return _CursorTransaction(self)

    # Shorthand alias for transaction()
    tx = transaction

    def __repr__(self) -> str:
        return f"Cursor(table={self.table}, key={self.key!r})"
