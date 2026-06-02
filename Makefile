.PHONY: test test-file test-integration coverage lint typecheck
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

coverage:
	@uv run --quiet --with pytest-cov pytest --cov=core --cov=cli --cov-fail-under=100 --cov-report=term-missing:skip-covered -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d; /^_.*coverage:/d'

test:
	@uv run --quiet pytest -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d'

test-file:
	@uv run --quiet pytest $(FILE) -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d'

test-integration:
	@uv run --quiet pytest tests/integration/ -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d'

lint:
	@uv run --quiet ruff check --output-format concise .

typecheck:
	@uv run --quiet mypy --no-error-summary .
