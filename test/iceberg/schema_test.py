"""Tests for schema conversion utilities."""

import pytest
from pyiceberg.schema import Schema as IcebergSchema

from avalanche.iceberg.schema import (
    dataframely_to_iceberg_schema,
    normalize_schema,
)


class TestDataFramelySchemaConversion:
    """Test DataFramely to Iceberg schema conversion."""

    def test_dataframely_to_iceberg_conversion(self):
        """Test converting DataFramely schema to Iceberg schema."""
        import dataframely as dy

        class TestSchema(dy.Schema):
            id = dy.Int64(nullable=False)
            name = dy.String(nullable=False)
            age = dy.Int32(nullable=True)

        iceberg_schema = dataframely_to_iceberg_schema(TestSchema)

        assert isinstance(iceberg_schema, IcebergSchema)
        # Check fields exist
        field_names = [field.name for field in iceberg_schema.fields]
        assert "id" in field_names
        assert "name" in field_names
        assert "age" in field_names

    def test_normalize_schema_with_dataframely(self):
        """Test normalize_schema with DataFramely schema."""
        import dataframely as dy

        class TestSchema(dy.Schema):
            id = dy.Int64(nullable=False)
            name = dy.String(nullable=False)

        normalized = normalize_schema(TestSchema)
        assert isinstance(normalized, IcebergSchema)

    def test_normalize_schema_with_iceberg(self):
        """Test normalize_schema with PyIceberg schema (passthrough)."""
        from pyiceberg.schema import Schema
        from pyiceberg.types import NestedField, StringType

        iceberg_schema = Schema(
            fields=[
                NestedField(1, "id", StringType(), required=True),
                NestedField(2, "name", StringType(), required=True),
            ]
        )

        normalized = normalize_schema(iceberg_schema)
        assert normalized is iceberg_schema  # Should be same object

    def test_normalize_schema_with_invalid_type(self):
        """Test normalize_schema raises error for invalid types."""
        with pytest.raises(TypeError, match="Schema must be either"):
            normalize_schema("not a schema")

        with pytest.raises(TypeError, match="Schema must be either"):
            normalize_schema({"id": "int64"})


class TestDataFramelySchemaWithTable:
    """Test using DataFramely schemas with IcebergTable."""

    def test_table_with_dataframely_schema(self):
        """Test creating IcebergTable with DataFramely schema."""
        import dataframely as dy

        from avalanche.iceberg import IcebergTable

        class TestSchema(dy.Schema):
            id = dy.Int64(nullable=False)
            name = dy.String(nullable=False)
            value = dy.Float64(nullable=True)

        table = IcebergTable(schema=TestSchema)

        # Schema should be normalized to IcebergSchema
        assert isinstance(table.schema, IcebergSchema)

        # Should have the expected fields
        field_names = [field.name for field in table.schema.fields]
        assert "id" in field_names
        assert "name" in field_names
        assert "value" in field_names
