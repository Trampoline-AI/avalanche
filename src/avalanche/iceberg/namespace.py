"""
IcebergNamespace - Container for Iceberg tables.

Manages collections of Iceberg tables within a catalog namespace and provides
lifecycle operations (create, drop, push).
"""

import logging
from typing import Iterable, Optional, Tuple, Union

from pyiceberg.catalog import Catalog, load_catalog
from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError
from pyiceberg.table import (
    ALWAYS_TRUE,
    EMPTY_DICT,
    BooleanExpression,
    DataScan,
    FileScanTask,
    Properties,
    Table,
)

from ..storage import Namespace, NamespaceConfig, TableGroup
from ..utils import urljoin
from .table import IcebergTable

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ============================================================================
# IcebergAppendScan - Incremental Scanning
# ============================================================================


class IcebergAppendScan(DataScan):
    """
    Scan that returns only data added since a start snapshot.

    This enables incremental processing by filtering out files that existed
    in a previous snapshot, returning only newly added files.

    Used by Stream to implement incremental data flow.
    """

    start_snapshot_id: int | None = None

    @classmethod
    def from_table(
        cls,
        table: "Table | IcebergTable",
        row_filter: Union[str, BooleanExpression] = ALWAYS_TRUE,
        selected_fields: Tuple[str, ...] = ("*",),
        case_sensitive: bool = True,
        start_snapshot_id: Optional[int] = None,
        snapshot_id: Optional[int] = None,
        options: Properties = EMPTY_DICT,
        limit: Optional[int] = None,
    ) -> "IcebergAppendScan":
        """
        Create an IcebergAppendScan from a table.

        Args:
            table: IcebergTable or PyIceberg Table instance
        """
        # Extract underlying table if it's an IcebergTable (proxy will handle attribute access)
        # Otherwise use table directly (it's already a PyIceberg Table)
        # The proxy makes this transparent - we can access .metadata, .io, .history() on both

        instance = cls(
            table_metadata=table.metadata,
            io=table.io,
            row_filter=row_filter,
            selected_fields=selected_fields,
            case_sensitive=case_sensitive,
            snapshot_id=int(snapshot_id) if snapshot_id else None,
            options=options,
            limit=limit,
        )

        start_snapshot_id = int(start_snapshot_id) if start_snapshot_id else None
        instance.start_snapshot_id = start_snapshot_id

        if start_snapshot_id is not None:
            start_snapshot = next(
                (s for s in table.history() if s.snapshot_id == start_snapshot_id),
                None,
            )
            assert start_snapshot is not None, f"Start snapshot not found: {start_snapshot_id}"

        return instance

    def plan_files(self) -> Iterable[FileScanTask]:
        """
        Plan which files to scan.

        Filters out files that existed in the start snapshot, returning only
        files added since then.
        """
        current_plan = super().plan_files()

        if self.start_snapshot_id is None:
            return current_plan

        # Get files from the start snapshot
        try:
            orig_snapshot_id = self.snapshot_id
            self.snapshot_id = self.start_snapshot_id
            prev_plan = super().plan_files()

            return [task for task in current_plan if task not in prev_plan]

        finally:
            # Restore the snapshot id
            self.snapshot_id = orig_snapshot_id


# ============================================================================
# IcebergTableGroup - For organizing tables
# ============================================================================


class IcebergTableGroup(TableGroup):
    """
    Group of related tables within a namespace.

    Simple container for organizing tables.
    """

    def __init__(self, **tables: IcebergTable):
        """
        Initialize table group.

        Args:
            **tables: Named tables in this group
        """
        super().__init__(**tables)


# ============================================================================
# IcebergNsConfig - Namespace configuration
# ============================================================================


class IcebergNsConfig(NamespaceConfig):
    """
    Configuration for IcebergNamespace.

    Example:
        class MyNamespace(IcebergNs):
            ns_config = IcebergNsConfig(
                name="my_data",
                base_location="s3://bucket/data"
            )
    """

    def __init__(
        self,
        name: str,
        base_location: str,
        properties: dict[str, str] | None = None,
    ):
        super().__init__(name=name, base_location=base_location, properties=properties)


# ============================================================================
# IcebergNamespace - Container for tables
# ============================================================================


class IcebergNamespace(Namespace):
    """
    Container for Iceberg tables in a catalog namespace.

    Manages:
    - Catalog connection
    - Table definitions and lifecycle
    - Namespace operations (create, drop)

    Usage:
        class MyNamespace(IcebergNs):
            ns_config = IcebergNsConfig(
                name="my_data",
                base_location="s3://bucket/data"
            )

            documents: IcebergTable = IcebergTable(schema=DocumentSchema)
            chunks: IcebergTable = IcebergTable(schema=ChunkSchema)

        # Create instance
        ns = MyNamespace(
            catalog="local",
            catalog_props={"type": "sql", "uri": "sqlite:///catalog.db"}
        )

        # Push to catalog
        ns.push()

        # Use in tasks
        @ava.source
        def my_source(*, docs=ns.documents):
            docs.append(df)
    """

    # Class-level config (required)
    ns_config: IcebergNsConfig | None = None

    def __init__(
        self,
        *,
        catalog: str | Catalog | None = None,
        load_catalog_props: dict | None = None,
    ):
        """
        Initialize namespace.

        Args:
            catalog: Catalog name or instance
            load_catalog_props: Properties for loading catalog

        Raises:
            ValueError: If ns_config is not defined on the class
        """
        load_catalog_props = load_catalog_props or {}

        # Require ns_config
        if not hasattr(self.__class__, "ns_config") or self.__class__.ns_config is None:
            raise ValueError(
                f"{self.__class__.__name__} must define ns_config. "
                "Example: ns_config = IcebergNsConfig(name='...', base_location='...')"
            )

        super().__init__()

        # Load catalog if provided
        if catalog is not None:
            if isinstance(catalog, str):
                logger.info(f"Loading catalog {catalog} with props {load_catalog_props}")
                self.catalog = load_catalog(catalog, **load_catalog_props)
            elif isinstance(catalog, Catalog):
                if load_catalog_props:
                    raise ValueError(
                        "load_catalog_props should not be provided "
                        "when catalog is a Catalog instance"
                    )
                self.catalog = catalog
            else:
                raise ValueError("Invalid catalog type")
        else:
            # Try to load catalog by namespace name
            self.catalog = load_catalog(self.name)

    def _get_all_tables(self) -> list[tuple[str, IcebergTable]]:
        """Discover all IcebergTable instances from class attributes."""
        return super()._get_all_tables()

    def push(self) -> None:
        """
        Create/update namespace and tables in catalog.

        This is like 'prisma db push' - synchronizes declared schema with actual state.
        """
        if (self.name,) not in self.catalog.list_namespaces():
            self.catalog.create_namespace(self.name)

        for name, table in self._get_all_tables():
            self._push_table(table)

    def _push_table(self, table: IcebergTable) -> None:
        """Push a single table to the catalog."""
        existing_tables = self.catalog.list_tables(self.name)
        logger.info(f"Existing tables: {existing_tables}")

        if (self.name, table._table_name) not in existing_tables:
            # Compute location
            location = urljoin(self.location, table._table_name)
            logger.warning(f"Creating table {table.identifier} at {location}")

            # Create table
            table._table = self.catalog.create_table(
                identifier=table.identifier,
                schema=table.schema,
                location=location,
            )
        else:
            logger.warning(f"Table {table.identifier} already exists. Skipping creation.")

    def drop(self, *, drop_tables: bool = False) -> None:
        """
        Drop namespace from catalog.

        Args:
            drop_tables: If True, drop all tables before dropping namespace
        """
        if drop_tables:
            for name, table in self._get_all_tables():
                self._drop_table(table)

        try:
            self.catalog.drop_namespace(self.name)
            logger.warning(f"Dropped namespace {self.name}")
        except NoSuchNamespaceError:
            logger.warning(
                f"Cannot drop namespace: {self.name} does not exist (NoSuchNamespaceError)."
            )

    def _drop_table(self, table: IcebergTable) -> None:
        """Drop a single table from the catalog."""
        try:
            self.catalog.drop_table(table.identifier)
            table._table = None
            logger.warning(f"Dropped table {table.identifier}")
        except NoSuchTableError:
            table._table = None
            logger.warning(
                f"Cannot drop table: {table.identifier} does not exist (NoSuchTableError)."
            )

    def __repr__(self) -> str:
        return f"IcebergNamespace(name={self.name!r})"


# Alias for documentation compatibility
IcebergNs = IcebergNamespace
