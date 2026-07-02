"""
Schema conversion utilities for Iceberg.

Handles conversion from DataFramely schemas to PyIceberg schemas.
"""

from typing import Any, Type, Union

import dataframely as dy
import pyarrow as pa
from pyiceberg.schema import Schema as IcebergSchema
from pyiceberg.types import (
    BinaryType,
    BooleanType,
    DateType,
    DoubleType,
    FloatType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestampType,
)


def _pyarrow_type_to_iceberg_type(arrow_type: pa.DataType) -> Any:
    """
    Convert PyArrow type to PyIceberg type.

    Args:
        arrow_type: PyArrow data type

    Returns:
        PyIceberg type instance
    """
    # Map PyArrow types to PyIceberg types
    if pa.types.is_int64(arrow_type):
        return LongType()
    elif pa.types.is_int32(arrow_type):
        return IntegerType()
    elif pa.types.is_float32(arrow_type):
        return FloatType()
    elif pa.types.is_float64(arrow_type):
        return DoubleType()
    elif pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return StringType()
    elif pa.types.is_binary(arrow_type) or pa.types.is_large_binary(arrow_type):
        return BinaryType()
    elif pa.types.is_boolean(arrow_type):
        return BooleanType()
    elif pa.types.is_date(arrow_type):
        return DateType()
    elif pa.types.is_timestamp(arrow_type):
        return TimestampType()
    else:
        raise NotImplementedError(f"PyArrow type {arrow_type} not yet supported for conversion")


def dataframely_to_iceberg_schema(schema: Type[dy.Schema]) -> IcebergSchema:
    """
    Convert a DataFramely schema to PyIceberg schema.

    Converts field by field to create proper Iceberg fields with IDs.

    Args:
        schema: DataFramely Schema class

    Returns:
        PyIceberg Schema instance

    Example:
        import dataframely as dy

        class MySchema(dy.Schema):
            id = dy.Int64(nullable=False)
            name = dy.String(nullable=False)

        iceberg_schema = dataframely_to_iceberg_schema(MySchema)
    """
    # Get PyArrow schema from DataFramely
    pyarrow_schema = schema.pyarrow_schema()

    # Convert field by field
    iceberg_fields = []
    for i, arrow_field in enumerate(pyarrow_schema):
        field_id = i + 1  # Iceberg field IDs start at 1
        field_name = arrow_field.name
        field_type = _pyarrow_type_to_iceberg_type(arrow_field.type)
        required = not arrow_field.nullable

        iceberg_field = NestedField(
            field_id=field_id,
            name=field_name,
            field_type=field_type,
            required=required,
        )
        iceberg_fields.append(iceberg_field)

    return IcebergSchema(*iceberg_fields)


def normalize_schema(schema: Union[Type[dy.Schema], IcebergSchema]) -> IcebergSchema:
    """
    Normalize a schema to PyIceberg Schema.

    Accepts:
    - PyIceberg Schema (returns as-is)
    - DataFramely Schema (converts via PyArrow)

    Args:
        schema: Schema in any supported format

    Returns:
        PyIceberg Schema instance

    Raises:
        TypeError: If schema type is not supported

    Example:
        # Works with PyIceberg Schema
        schema = normalize_schema(my_iceberg_schema)

        # Works with DataFramely schema
        import dataframely as dy

        class MySchema(dy.Schema):
            id = dy.Int64(nullable=False)

        schema = normalize_schema(MySchema)
    """
    # If already PyIceberg Schema, return as-is
    if isinstance(schema, IcebergSchema):
        return schema

    # Check if it's a DataFramely Schema class
    if isinstance(schema, type) and issubclass(schema, dy.Schema):
        return dataframely_to_iceberg_schema(schema)

    # Unsupported type
    raise TypeError(
        f"Schema must be either PyIceberg Schema or DataFramely Schema class, "
        f"got {type(schema)}"
    )
