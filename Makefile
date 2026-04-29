.PHONY: test test-file coverage lint format typecheck
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

coverage:
	@uv run --quiet --with pytest-cov pytest --cov=core --cov=cli --cov-fail-under=100 --cov-report=term-missing:skip-covered -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d; /^_.*coverage:/d'

test:
	@uv run --quiet pytest -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d'

test-file:
	@uv run --quiet pytest $(FILE) -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d'

lint:
	@uv run --quiet ruff check --output-format concise .

format:
	@uv run --quiet ruff format .
	@uv run --quiet ruff check --fix .

typecheck:
	@uv run --quiet mypy --no-error-summary .
