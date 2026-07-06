"""Tests for IcebergTable."""

from tempfile import TemporaryDirectory

import dataframely as dy
import pytest
from pyiceberg.schema import Schema as IcebergSchema
from pyiceberg.types import NestedField, StringType

from avalanche.iceberg import IcebergNs, IcebergNsConfig, IcebergTable
from avalanche.lineage import ROW_LINEAGE_COLUMNS


class TestTableSchema(dy.Schema):
    """Test schema for table tests."""

    id = dy.String(nullable=False)
    name = dy.String(nullable=False)
    value = dy.Int64(nullable=True)


class TestIcebergTableInit:
    """Test IcebergTable initialization."""

    def test_init_with_dataframely_schema(self):
        """Test initialization with DataFramely schema."""
        table = IcebergTable(schema=TestTableSchema)

        assert table.schema is not None
        assert isinstance(table.schema, IcebergSchema)
        assert table._table_name == ""
        assert table._ns is None

        # Verify table is not accessible before creation
        with pytest.raises(AttributeError, match="has not been created yet"):
            _ = table.scan()

    def test_init_with_pyiceberg_schema(self):
        """Test initialization with PyIceberg schema."""
        pyiceberg_schema = IcebergSchema(
            NestedField(1, "id", StringType(), required=True),
            NestedField(2, "name", StringType(), required=True),
        )

        table = IcebergTable(schema=pyiceberg_schema)

        assert isinstance(table.schema, IcebergSchema)
        assert table.schema_fields == ("id", "name", *ROW_LINEAGE_COLUMNS)

    def test_pyiceberg_schema_row_lineage_can_be_disabled(self):
        """Test PyIceberg schema identity is preserved when row lineage is disabled."""
        pyiceberg_schema = IcebergSchema(
            NestedField(1, "id", StringType(), required=True),
            NestedField(2, "name", StringType(), required=True),
        )

        table = IcebergTable(schema=pyiceberg_schema, row_lineage=False)

        assert table.schema is pyiceberg_schema
        assert table.schema_fields == ("id", "name")

    def test_init_with_invalid_schema(self):
        """Test initialization with invalid schema type."""
        with pytest.raises(TypeError, match="Schema must be either"):
            IcebergTable(schema="not a schema")


class TestIcebergTableProperties:
    """Test IcebergTable properties."""

    @pytest.fixture
    def namespace(self):
        """Create a test namespace with tables."""
        with TemporaryDirectory() as tmpdir:

            class TestNs(IcebergNs):
                ns_config = IcebergNsConfig(
                    name="test-ns",
                    base_location=tmpdir,
                )
                test_table: IcebergTable = IcebergTable(schema=TestTableSchema)

            ns = TestNs(
                catalog="test-catalog",
                load_catalog_props=dict(type="sql", uri="sqlite:///:memory:"),
            )
            ns.push()
            yield ns

    def test_identifier_before_push(self):
        """Test identifier before namespace is set."""
        table = IcebergTable(schema=TestTableSchema)
        assert table.identifier == ""

    def test_identifier_after_push(self, namespace):
        """Test identifier after namespace push."""
        table = namespace.test_table
        assert table.identifier == "test-ns.test_table"

    def test_location_before_push(self):
        """Test location before namespace is set."""
        table = IcebergTable(schema=TestTableSchema)
        assert table.location == ""

    def test_location_after_push(self, namespace):
        """Test location after namespace push."""
        table = namespace.test_table
        assert "test-ns/test_table" in table.location

    def test_name_set_by_namespace(self, namespace):
        """Test that table name is set by namespace."""
        table = namespace.test_table

        # Internal table name is set
        assert table._table_name == "test_table"

        # PyIceberg's name() method returns the full identifier (catalog, namespace, table)
        identifier = table.name()
        assert identifier[-2:] == ("test-ns", "test_table")

    def test_proxied_methods_before_push(self):
        """Test accessing proxied methods before push raises error."""
        with TemporaryDirectory() as tmpdir:

            class TestNs(IcebergNs):
                ns_config = IcebergNsConfig(
                    name="test-ns",
                    base_location=tmpdir,
                )
                test_table: IcebergTable = IcebergTable(schema=TestTableSchema)

            ns = TestNs(
                catalog="test-catalog",
                load_catalog_props=dict(type="sql", uri="sqlite:///:memory:"),
            )

            # Don't push - table not created yet
            with pytest.raises(AttributeError, match="has not been created yet"):
                _ = ns.test_table.scan()

    def test_proxied_methods_after_push(self, namespace):
        """Test that proxied methods work after push."""
        table = namespace.test_table

        # These should all work via proxying
        assert hasattr(table, "scan")
        assert hasattr(table, "metadata")
        assert hasattr(table, "history")

        # Actually call a proxied method
        history = table.history()
        assert isinstance(history, list)


class TestIcebergTableDataOperations:
    """Test IcebergTable data operations via proxying."""

    @pytest.fixture
    def namespace(self):
        """Create a test namespace with tables."""
        with TemporaryDirectory() as tmpdir:

            class TestNs(IcebergNs):
                ns_config = IcebergNsConfig(
                    name="test-ns",
                    base_location=tmpdir,
                )
                test_table: IcebergTable = IcebergTable(schema=TestTableSchema)

            ns = TestNs(
                catalog="test-catalog",
                load_catalog_props=dict(type="sql", uri="sqlite:///:memory:"),
            )
            ns.push()
            yield ns

    def test_append_via_proxy(self, namespace):
        """Test that append method is accessible via proxying."""
        table = namespace.test_table

        # Verify append method exists and is callable
        assert hasattr(table, "append")
        assert callable(table.append)

        # The actual append functionality is PyIceberg's responsibility
        # We're just testing that the proxy works

    def test_scan_via_proxy(self, namespace):
        """Test that scan is proxied to PyIceberg."""
        table = namespace.test_table

        # Scan should work even on empty table
        scan = table.scan()
        assert scan is not None

        result = scan.to_arrow()
        assert len(result) == 0


class TestIcebergTableProxying:
    """Test transparent proxying to underlying PyIceberg table."""

    @pytest.fixture
    def namespace(self):
        """Create a test namespace with tables."""
        with TemporaryDirectory() as tmpdir:

            class TestNs(IcebergNs):
                ns_config = IcebergNsConfig(
                    name="test-ns",
                    base_location=tmpdir,
                )
                test_table: IcebergTable = IcebergTable(schema=TestTableSchema)

            ns = TestNs(
                catalog="test-catalog",
                load_catalog_props=dict(type="sql", uri="sqlite:///:memory:"),
            )
            ns.push()
            yield ns

    def test_proxy_metadata_access(self, namespace):
        """Test proxying metadata attribute."""
        table = namespace.test_table

        # Access metadata through proxy
        metadata = table.metadata
        assert metadata is not None
        assert hasattr(metadata, "schema")

    def test_proxy_history_method(self, namespace):
        """Test proxying history() method."""
        table = namespace.test_table

        # Access history through proxy
        history = table.history()
        assert isinstance(history, list)

    def test_proxy_snapshots_method(self, namespace):
        """Test proxying snapshots() method."""
        table = namespace.test_table

        # Access snapshots through proxy
        snapshots = table.snapshots()
        assert snapshots is not None

    def test_proxy_before_push_raises_error(self):
        """Test that proxying before push gives helpful error."""
        with TemporaryDirectory() as tmpdir:

            class TestNs(IcebergNs):
                ns_config = IcebergNsConfig(
                    name="test-ns",
                    base_location=tmpdir,
                )
                test_table: IcebergTable = IcebergTable(schema=TestTableSchema)

            ns = TestNs(
                catalog="test-catalog",
                load_catalog_props=dict(type="sql", uri="sqlite:///:memory:"),
            )

            # Don't push
            with pytest.raises(AttributeError, match="has not been created yet"):
                _ = ns.test_table.history()

    def test_private_attributes_not_proxied(self, namespace):
        """Test that private attributes aren't proxied."""
        table = namespace.test_table

        with pytest.raises(AttributeError):
            _ = table._nonexistent_private_attr


class TestTableAppendScan:
    """Test IcebergTable.append_scan() convenience method."""

    @pytest.fixture
    def namespace(self):
        """Create a test namespace with tables."""
        with TemporaryDirectory() as tmpdir:

            class TestNs(IcebergNs):
                ns_config = IcebergNsConfig(
                    name="test-ns",
                    base_location=tmpdir,
                )
                test_table: IcebergTable = IcebergTable(schema=TestTableSchema)

            ns = TestNs(
                catalog="test-catalog",
                load_catalog_props=dict(type="sql", uri="sqlite:///:memory:"),
            )
            ns.push()
            yield ns

    def test_append_scan_convenience_method(self, namespace):
        """Test that append_scan() convenience method works."""
        from avalanche.iceberg import IcebergAppendScan

        table = namespace.test_table

        # Use convenience method
        scan = table.append_scan()

        assert scan is not None
        assert isinstance(scan, IcebergAppendScan)

    def test_append_scan_with_start_snapshot(self, namespace):
        """Test append_scan() with start_snapshot_id parameter."""
        from avalanche.iceberg import IcebergAppendScan

        table = namespace.test_table

        # Get current snapshot if available
        if table.current_snapshot():
            current_snapshot_id = table.current_snapshot().snapshot_id

            # Create scan with start_snapshot_id
            scan = table.append_scan(start_snapshot_id=current_snapshot_id)

            assert isinstance(scan, IcebergAppendScan)
            assert scan.start_snapshot_id == current_snapshot_id

    def test_append_scan_equivalent_to_from_table(self, namespace):
        """Test that table.append_scan() is equivalent to IcebergAppendScan.from_table()."""
        from avalanche.iceberg import IcebergAppendScan

        table = namespace.test_table

        # Both methods should create equivalent scans
        scan1 = table.append_scan()
        scan2 = IcebergAppendScan.from_table(table)

        # They should have the same table metadata
        assert scan1.table_metadata == scan2.table_metadata


class TestIcebergTableRepr:
    """Test IcebergTable string representation."""

    def test_repr_before_namespace(self):
        """Test repr before namespace is set."""
        table = IcebergTable(schema=TestTableSchema)
        repr_str = repr(table)

        assert "IcebergTable" in repr_str
        assert "table_name=''" in repr_str

    def test_repr_after_namespace(self):
        """Test repr after namespace is set."""
        with TemporaryDirectory() as tmpdir:

            class TestNs(IcebergNs):
                ns_config = IcebergNsConfig(
                    name="test-ns",
                    base_location=tmpdir,
                )
                test_table: IcebergTable = IcebergTable(schema=TestTableSchema)

            ns = TestNs(
                catalog="test-catalog",
                load_catalog_props=dict(type="sql", uri="sqlite:///:memory:"),
            )
            ns.push()

            repr_str = repr(ns.test_table)
            assert "IcebergTable" in repr_str
            assert "test_table" in repr_str
            assert "test-ns.test_table" in repr_str
