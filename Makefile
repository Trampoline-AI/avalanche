.PHONY: test test-cov test-cov-html lint format precommit-check check smoke-test tui-bench proto brand install clean web-assets-check web-bench

# Run tests with every supported executor/storage extra installed.
test:
	uv run --all-extras pytest

# Run tests with coverage report
test-cov:
	uv run --all-extras pytest --cov --cov-report=term-missing

# Run tests with HTML coverage report
test-cov-html:
	uv run --all-extras pytest --cov --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

# Run linter
lint:
	uv run ruff check src/ test/

# Format code
format:
	uv run ruff check --fix src/ test/
	uv run ruff format src/ test/

# Run the full pre-commit gate (lint + test)
precommit-check: lint test

# Backwards-compatible alias for the full pre-commit gate
check: precommit-check

# Run bounded smoke tests for the documented user path
smoke-test:
	uv run pytest test/smoke_test.py test/example_smoke_test.py test/operator_example_discovery_test.py -v

# Measure TUI refresh scaling as log history grows
tui-bench:
	uv run python scripts/benchmark_tui.py --assert-budget

# Regenerate checked-in operator protobuf modules.
proto:
	cd src/runtime/operator/proto && uv run python -m grpc_tools.protoc \
		-I. --python_out=. --pyi_out=. --grpc_python_out=. operator.proto
	perl -pi -e 's/^import operator_pb2 as operator__pb2$$/from . import operator_pb2 as operator__pb2/' \
		src/runtime/operator/proto/operator_pb2_grpc.py

# Regenerate the checked-in TypeScript operator client.
web-proto:
	cd web/operator && pnpm generate

# Build the packaged browser interface.
web-build:
	cd web/operator && pnpm build

# Rebuild the packaged browser interface and reject unstaged generated assets.
web-assets-check: web-build
	@if ! git diff --quiet -- src/runtime/operator/web_assets || \
		test -n "$$(git ls-files --others --exclude-standard -- src/runtime/operator/web_assets)"; then \
		printf '\n'; \
		git status --short -- src/runtime/operator/web_assets; \
		if test "$(WEB_ASSETS_CHECK_CONTEXT)" = "ci"; then \
			printf '\nERROR: Packaged browser assets are stale.\n'; \
			printf 'CI rebuilt them only for verification; files in this runner will be discarded.\n'; \
			printf 'Rebuild and stage them on your branch with:\n'; \
			printf '  make web-build\n'; \
			printf '  git add -A src/runtime/operator/web_assets\n'; \
			printf 'Then commit the generated files and push again.\n'; \
		else \
			printf '\nERROR: Packaged browser assets were stale and have now been rebuilt.\n'; \
			printf 'The generated files above are now in your working tree.\n'; \
			printf 'Include them with your web source changes by running:\n'; \
			printf '  git add -A src/runtime/operator/web_assets\n'; \
			printf 'Then retry your commit.\n'; \
		fi; \
		exit 1; \
	fi

# Run browser projection and component tests.
web-test:
	cd web/operator && pnpm test

# Run the Vitest volume gate followed by the Node-managed real-Chromium virtualizer gate.
web-bench:
	cd web/operator && pnpm benchmark

# Build checked-in brand image artifacts from the Three.js source HTML.
brand:
	node docs/assets/brand/source/export-brand-assets.mjs

# Install dependencies
install:
	uv sync

# Clean build artifacts
clean:
	rm -rf .pytest_cache htmlcov .coverage .ruff_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
