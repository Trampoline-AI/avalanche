.PHONY: test test-cov test-cov-html lint format check install clean

# Run tests
test:
	uv run pytest

# Run tests with coverage report
test-cov:
	uv run pytest --cov --cov-report=term-missing

# Run tests with HTML coverage report
test-cov-html:
	uv run pytest --cov --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

# Run linter
lint:
	uv run ruff check src/ test/

# Format code
format:
	uv run ruff check --fix src/ test/
	uv run ruff format src/ test/

# Run all checks (lint + test)
check: lint test

# Install dependencies
install:
	uv sync

# Clean build artifacts
clean:
	rm -rf .pytest_cache htmlcov .coverage .ruff_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
