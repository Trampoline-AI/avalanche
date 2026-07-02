"""Tests for Cursor - simple position tracking for external sources."""

import pytest

from avalanche.runtime import Cursor


class MockTransaction:
    """Mock transaction for testing."""

    def __init__(self, table: "MockTable"):
        self.table = table

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return None

    def set_properties(self, **kwargs):
        """Update table properties."""
        self.table.properties.update(kwargs)


class MockTable:
    """Mock table for testing."""

    def __init__(self, name: str):
        self.name = name
        self.properties = {}

    def transaction(self):
        """Create a mock transaction."""
        return MockTransaction(self)


class TestCursor:
    """Test Cursor initialization and interface."""

    def test_cursor_initialization(self):
        """Test that Cursor can be initialized with a table."""
        table = MockTable("docs")
        cursor = Cursor(table, key="default")

        assert cursor.table is table
        assert cursor.key == "default"
        assert cursor._property_key == "avalanche.cursor.default"

    def test_cursor_with_custom_key(self):
        """Test Cursor with custom key."""
        table = MockTable("docs")
        cursor = Cursor(table, key="checkpoint_a")

        assert cursor.key == "checkpoint_a"
        assert cursor._property_key == "avalanche.cursor.checkpoint_a"

    def test_cursor_key_validation(self):
        """Test that Cursor validates key format (snake_case)."""
        table = MockTable("docs")

        # Empty string should raise ValueError
        with pytest.raises(ValueError, match="snake_case"):
            Cursor(table, key="")

        # Whitespace-only should raise ValueError
        with pytest.raises(ValueError, match="snake_case"):
            Cursor(table, key="   ")

        # Invalid: special characters
        with pytest.raises(ValueError, match="snake_case"):
            Cursor(table, key="invalid key!")

        with pytest.raises(ValueError, match="snake_case"):
            Cursor(table, key="key@special")

        # Invalid: uppercase letters
        with pytest.raises(ValueError, match="snake_case"):
            Cursor(table, key="MyKey")

        # Invalid: starts with number
        with pytest.raises(ValueError, match="snake_case"):
            Cursor(table, key="123start")

        # Invalid: starts with underscore
        with pytest.raises(ValueError, match="snake_case"):
            Cursor(table, key="_private")

        # Invalid: dashes (not allowed in snake_case)
        with pytest.raises(ValueError, match="snake_case"):
            Cursor(table, key="my-cursor")

        # Valid keys should work
        Cursor(table, key="default")
        Cursor(table, key="valid_key_123")
        Cursor(table, key="checkpoint1")
        Cursor(table, key="last_processed_id")

    def test_cursor_get_returns_none_when_not_set(self):
        """Test that get() returns None when cursor value is not set."""
        table = MockTable("docs")
        cursor = Cursor(table, key="test")

        # MockTable has no properties, should return None
        assert cursor.get() is None

    def test_cursor_set_requires_transaction(self):
        """Test that set() requires transaction context."""
        table = MockTable("docs")
        cursor = Cursor(table, key="test")

        # set() outside transaction should raise RuntimeError
        with pytest.raises(RuntimeError, match="cursor.transaction\\(\\) context"):
            cursor.set(123)

    def test_cursor_transaction_method(self):
        """Test cursor.transaction() for cursor operations."""
        table = MockTable("docs")
        cursor = Cursor(table, key="checkpoint")

        # Use cursor.transaction() explicitly
        with cursor.transaction() as tx:
            # tx should be the transaction
            assert tx is not None

            # get() works
            assert cursor.get() is None

            # set() works within context
            cursor.set(42)

        # Value should be persisted
        assert table.properties["avalanche.cursor.checkpoint"] == "42"

    def test_cursor_get_and_set_workflow(self):
        """Test typical cursor get/set workflow."""
        table = MockTable("docs")
        cursor = Cursor(table, key="last_id")

        # Initial value is None
        assert cursor.get() is None

        # Set value in transaction
        with cursor.transaction():
            cursor.set(100)

        # Value persists after transaction
        assert cursor.get() == "100"

        # Update value
        with cursor.tx():  # Test shorthand
            current = cursor.get()
            cursor.set(int(current) + 50)

        assert cursor.get() == "150"

    def test_cursor_transaction_returns_table_transaction(self):
        """Test that transaction() returns the table transaction for advanced use."""
        table = MockTable("docs")
        cursor = Cursor(table, key="test")

        with cursor.transaction() as tx:
            # Can use transaction for other operations
            tx.set_properties(custom_prop="custom_value")
            cursor.set(123)

        # Both properties should be set
        assert table.properties["avalanche.cursor.test"] == "123"
        assert table.properties["custom_prop"] == "custom_value"

    def test_cursor_tx_shorthand(self):
        """Test that tx() is shorthand for transaction()."""
        table = MockTable("docs")
        cursor = Cursor(table, key="test")

        # tx() should work the same as transaction()
        with cursor.tx() as tx:
            assert tx is not None
            cursor.set("shorthand_works")

        assert cursor.get() == "shorthand_works"

    def test_cursor_repr(self):
        """Test Cursor string representation."""
        table = MockTable("docs")
        cursor = Cursor(table, key="test")

        assert "Cursor" in repr(cursor)
        assert "test" in repr(cursor)

    def test_cursor_as_default_argument_pattern(self):
        """Test the pattern of using Cursor as default argument."""
        table = MockTable("docs")

        # This is the pattern used in task signatures
        def my_task(*, cursor=Cursor(table, key="task_cursor")):
            return cursor

        # When called without args, gets the default
        result = my_task()
        assert isinstance(result, Cursor)
        assert result.table is table
        assert result.key == "task_cursor"
