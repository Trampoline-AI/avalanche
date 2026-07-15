"""Tests for iceberg.py - Iceberg backend integration."""

import os
import pickle
import threading
from tempfile import TemporaryDirectory

import dataframely as dy
import polars as pl
import pytest
from pyiceberg.catalog import load_catalog
from pyiceberg.schema import Schema
from pyiceberg.types import NestedField, StringType
from sqlalchemy.pool import QueuePool

import avalanche as ava
import avalanche.iceberg.namespace as namespace_module
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

    def test_in_memory_single_connection_pool_serializes_connection_leases(
        self, namespace: TestNamespace
    ):
        """A second thread cannot use the in-memory connection concurrently."""
        first_lease = namespace.catalog.engine.connect()
        checkout_started = threading.Event()
        second_lease_acquired = threading.Event()
        errors = []

        def acquire_second_lease():
            checkout_started.set()
            try:
                with namespace.catalog.engine.connect():
                    second_lease_acquired.set()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=acquire_second_lease)
        thread.start()
        try:
            assert checkout_started.wait(5)
            assert not second_lease_acquired.wait(0.2)
        finally:
            first_lease.close()

        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not errors
        assert second_lease_acquired.is_set()

    def test_push_binds_existing_tables_across_instances(self, tmpdir: str):
        """Test that push() binds tables that already exist in the catalog."""

        def make_ns():
            class ReproNamespace(IcebergNs):
                ns_config = IcebergNsConfig(
                    name="repush-namespace",
                    base_location=tmpdir,
                )
                tables: IcebergTableGroup = IcebergTableGroup(
                    test_table=IcebergTable(schema=TestSchema)
                )

            return ReproNamespace(
                catalog="test-catalog",
                load_catalog_props=dict(
                    type="sql",
                    uri=f"sqlite:///{tmpdir}/catalog.db",
                ),
            )

        first = make_ns()
        first.push()
        first.tables.test_table.append(pl.DataFrame({"id": ["1"], "name": ["a"]}))

        second = make_ns()
        second.push()

        assert second.tables.test_table._table is not None
        second.tables.test_table.append(pl.DataFrame({"id": ["2"], "name": ["b"]}))
        assert sorted(second.tables.test_table.read()["id"].to_list()) == ["1", "2"]

    @pytest.mark.parametrize(
        "catalog_path",
        ["explicit", "inferred", "config-loaded", "caller-supplied"],
    )
    def test_in_memory_sql_catalog_survives_more_than_five_driver_threads(
        self, catalog_path: str, monkeypatch, tmpdir: str
    ):
        class ThreadedNamespace(IcebergNs):
            ns_config = IcebergNsConfig(
                name=f"threaded-{catalog_path}",
                base_location=tmpdir,
            )
            records = IcebergTable(schema=TestSchema)

        properties = {"type": "sql", "uri": "sqlite:///:memory:"}
        seeded_namespace = "seeded"

        if catalog_path == "explicit":
            ns = ThreadedNamespace(
                catalog="threaded-explicit",
                load_catalog_props=properties,
            )
        elif catalog_path == "inferred":
            ns = ThreadedNamespace(
                catalog="threaded-inferred",
                load_catalog_props={"uri": "sqlite:///:memory:"},
            )
        else:
            catalog = load_catalog(f"threaded-{catalog_path}", **properties)
            catalog.create_namespace(seeded_namespace)
            catalog.create_table(
                (seeded_namespace, "seeded_table"),
                Schema(NestedField(1, "id", StringType(), required=True)),
                location=f"{tmpdir}/seeded-table",
            )
            if catalog_path == "config-loaded":
                monkeypatch.setattr(namespace_module, "load_catalog", lambda name: catalog)
                ns = ThreadedNamespace()
            else:
                ns = ThreadedNamespace(catalog=catalog)

        ns.push()

        assert ns.catalog.properties["uri"] == "sqlite:///:memory:"
        assert isinstance(ns.catalog.engine.pool, QueuePool)
        with pytest.raises(TypeError, match="in-memory"):
            pickle.dumps(ns.records)
        if catalog_path in {"config-loaded", "caller-supplied"}:
            assert (seeded_namespace,) in ns.catalog.list_namespaces()
            assert (seeded_namespace, "seeded_table") in ns.catalog.list_tables(
                seeded_namespace
            )

        @ava.source
        def list_namespaces():
            return ns.catalog.list_namespaces()

        @ava.workflow
        def sequential_flow():
            return list_namespaces()

        for _ in range(7):
            assert (ns.name,) in sequential_flow().run(executor=ava.LocalExecutor()).result()

        barrier = threading.Barrier(8)

        @ava.source
        def list_namespaces_concurrently():
            barrier.wait(timeout=10)
            return ns.catalog.list_namespaces()

        @ava.workflow
        def concurrent_flow():
            return list_namespaces_concurrently()

        handles = [
            concurrent_flow().run(executor=ava.LocalExecutor()) for _ in range(7)
        ]
        barrier.wait(timeout=10)
        for handle in handles:
            assert (ns.name,) in handle.result(timeout=10)


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
