"""Operator state persisted in one local SQLite file.

:class:`OperatorStateStore` owns the only connection. It runs in WAL mode with
an exclusive lock held for the store's lifetime, applies the ordered
:data:`~runtime.operator.state.migrations.MIGRATIONS` when it opens, and refuses
a database written by a newer Avalanche version.

Typed row adapters
------------------

Each table has exactly one adapter module in this package, following
:mod:`runtime.operator.state.metadata`:

* A migration in ``migrations.py`` creates the table as ``STRICT``.
* A frozen, ``strict`` Pydantic row model (or the existing domain model) names
  every column the adapter reads.
* One adapter class with static methods taking the ``sqlite3.Connection`` from
  :meth:`OperatorStateStore.transaction`, so writes to several tables can share
  one transaction::

      with store.transaction() as connection:
          metadata = OperatorMetadataTable.read(connection)

* Reads go through :func:`~runtime.operator.state.rows.row_model`; JSON columns
  are written with ``model_dump_json()`` and read back through
  :func:`~runtime.operator.state.rows.json_column` into their model. A row that
  fails validation raises :class:`StateRowError`; it is never skipped.
* Nothing untyped leaves an adapter: no ``sqlite3.Row``, tuples or dictionaries,
  and no ``getattr`` probing on results.
"""

from .errors import (
    StateRowError,
    StateSchemaMismatchError,
    StateSchemaTooNewError,
    StateStoreError,
    StateStoreLockedError,
)
from .migrations import LATEST_SCHEMA_VERSION, MIGRATIONS, Migration
from .store import DEFAULT_STATE_DB, MEMORY, OperatorStateStore, StateDbPath

__all__ = [
    "DEFAULT_STATE_DB",
    "LATEST_SCHEMA_VERSION",
    "MEMORY",
    "MIGRATIONS",
    "Migration",
    "OperatorStateStore",
    "StateDbPath",
    "StateRowError",
    "StateSchemaMismatchError",
    "StateSchemaTooNewError",
    "StateStoreError",
    "StateStoreLockedError",
]
