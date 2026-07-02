"""Tests for iceberg.py - Iceberg backend integration."""

import os
from tempfile import TemporaryDirectory

import dataframely as dy
import pytest

from avalanche.iceberg import (
    IcebergAppendScan,
    IcebergNamespace,
    IcebergNs,
    IcebergNsConfig,
    IcebergTable,
    IcebergTableGroup,
)


def print_directory_tree(directory, *, level=0):
    """Helper to print directory tree for debugging."""
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        print("    " * level + "|- " + name)
        if os.path.isdir(path):
            print_directory_tree(path, level=level + 1)


class TestSchema(dy.Schema):
    """Test schema defined with DataFramely."""

    id = dy.String(nullable=False)
    name = dy.String(nullable=False)


@pytest.fixture
def tmpdir():
    """Temporary directory for test data."""
    with TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def namespace(tmpdir):
    """Test namespace instance with dynamic tmpdir configuration."""

    class TestNamespace(IcebergNs):
        ns_config = IcebergNsConfig(
            name="test-namespace",
            base_location=tmpdir,
        )
        tables: IcebergTableGroup = IcebergTableGroup(
            test_table=IcebergTable(schema=TestSchema)
        )

    ns = TestNamespace(
        catalog="test-catalog",
        load_catalog_props=dict(
            type="sql",
            uri="sqlite:///:memory:",
        ),
    )
    return ns


# Type hint for test methods
TestNamespace = type("TestNamespace", (IcebergNs,), {})


class TestIcebergNamespace:
    """Test IcebergNamespace lifecycle operations."""

    def test_namespace_instantiation(self, namespace):
        """Test that namespace can be instantiated."""
        assert namespace is not None
        assert namespace.name == "test-namespace"

    def test_push_creates_namespace_and_tables(self, namespace: TestNamespace, tmpdir: str):
        """Test that push() creates namespace and tables in catalog."""
        namespace.push()

        # Verify directory structure
        print(f"tmpdir ({tmpdir})")
        print_directory_tree(tmpdir, level=1)

        # Verify table directory exists
        assert os.path.exists(f"{tmpdir}/test-namespace/test_table")

    def test_push_is_idempotent(self, namespace: TestNamespace, tmpdir: str):
        """Test that push() can be called multiple times without error."""
        namespace.push()
        namespace.push()  # Should not fail

        # Verify directory still exists
        print(f"tmpdir ({tmpdir})")
        print_directory_tree(tmpdir, level=1)

        assert os.path.exists(f"{tmpdir}/test-namespace/test_table")


class TestIcebergTable:
    """Test IcebergTable operations."""

    def test_table_has_namespace_reference(self, namespace: TestNamespace):
        """Test that tables have reference to parent namespace."""
        namespace.push()

        # Access table from namespace
        table = namespace.tables.test_table

        assert table._ns is namespace
        assert table._table_name == "test_table"

        # PyIceberg's name() method returns the full identifier (catalog, namespace, table)
        identifier = table.name()
        assert identifier[-2:] == ("test-namespace", "test_table")

    def test_table_identifier(self, namespace: TestNamespace):
        """Test table identifier format."""
        namespace.push()
        table = namespace.tables.test_table

        assert table.identifier == "test-namespace.test_table"

    def test_table_location(self, namespace: TestNamespace, tmpdir: str):
        """Test table location path."""
        namespace.push()
        table = namespace.tables.test_table

        assert table._table_name in table.location
        assert tmpdir in table.location


class TestNamespaceConfig:
    """Test IcebergNsConfig configuration."""

    def test_ns_config_required(self):
        """Test that ns_config is required."""

        class MissingConfigNamespace(IcebergNamespace):
            """Namespace without ns_config."""

            tables: IcebergTableGroup = IcebergTableGroup(
                test_table=IcebergTable(schema=TestSchema)
            )

        with pytest.raises(ValueError, match="must define ns_config"):
            MissingConfigNamespace(
                catalog="test-catalog",
                load_catalog_props=dict(type="sql", uri="sqlite:///:memory:"),
            )

    def test_ns_config_values_used(self, tmpdir):
        """Test that ns_config values are properly applied."""

        class ConfigNamespace(IcebergNs):
            ns_config = IcebergNsConfig(
                name="config-namespace",
                base_location=str(tmpdir),
                properties={"owner": "test", "env": "dev"},
            )

            tables: IcebergTableGroup = IcebergTableGroup(
                test_table=IcebergTable(schema=TestSchema)
            )

        ns = ConfigNamespace(
            catalog="test-catalog",
            load_catalog_props=dict(type="sql", uri="sqlite:///:memory:"),
        )

        # Config values should be used
        assert ns.name == "config-namespace"
        assert str(tmpdir) in str(ns.base_location)

    def test_ns_config_properties_accessible(self, tmpdir):
        """Test that custom properties in ns_config are accessible."""

        class PropertiesNamespace(IcebergNs):
            ns_config: IcebergNsConfig = IcebergNsConfig(
                name="props-namespace",
                base_location=str(tmpdir),
                properties={"owner": "team-x", "environment": "staging"},
            )
            tables: IcebergTableGroup = IcebergTableGroup(
                test_table=IcebergTable(schema=TestSchema)
            )

        ns = PropertiesNamespace(
            catalog="test-catalog",
            load_catalog_props=dict(type="sql", uri="sqlite:///:memory:"),
        )

        # Properties should be accessible
        assert ns.ns_config.properties["owner"] == "team-x"
        assert ns.ns_config.properties["environment"] == "staging"


class TestAppendScan:
    """Test IcebergAppendScan for incremental processing."""

    def test_append_scan_can_be_created(self, namespace: TestNamespace):
        """Test that IcebergAppendScan can be created from a table."""
        namespace.push()
        table = namespace.tables.test_table

        # Create append scan - works with IcebergTable via proxying
        scan = IcebergAppendScan.from_table(table)

        assert scan is not None

    def test_append_scan_with_start_snapshot(self, namespace: TestNamespace):
        """Test IcebergAppendScan with start_snapshot_id."""
        namespace.push()
        table = namespace.tables.test_table

        # Get current snapshot (if any) - proxied from PyIceberg Table
        if table.current_snapshot():
            current_snapshot_id = table.current_snapshot().snapshot_id

            # Create scan from that snapshot - works with IcebergTable via proxying
            scan = IcebergAppendScan.from_table(table, start_snapshot_id=current_snapshot_id)
            assert scan.start_snapshot_id == current_snapshot_id
