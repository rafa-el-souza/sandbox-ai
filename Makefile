.PHONY: test test-file test-integration coverage lint format typecheck check
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

# NOTE: `format` applies ruff's SAFE lint autofixes (`ruff check --fix`) only —
# it does NOT run `ruff format`, which has a code-mangling bug (it reflowed 89
# files + broke `()`-spacing; the old combined target was removed in main 85a3b4e).
#
# `lint` and `typecheck` echo a "✓ … (exit 0)" line on success so a clean run is
# visibly a pass rather than empty output (mypy is otherwise silent on success).

coverage:
	@uv run --quiet --with pytest-cov pytest --cov=core --cov=cli --cov-fail-under=100 --cov-report=term-missing:skip-covered -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d; /^_.*coverage:/d'

test:
	@uv run --quiet pytest -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d'

test-file:
	@uv run --quiet pytest $(FILE) -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d'

test-integration:
	@uv run --quiet pytest tests/integration/ -q --tb=short --no-header 2>&1 | sed -E '/^[.FEsxX ]+(\[|$$)/d'

lint:
	@uv run --quiet ruff check -q --output-format concise . && echo "✓ lint clean (exit 0)"

format:  # apply ruff's SAFE autofixes only (NOT `ruff format`, which is bugged)
	@uv run --quiet ruff check --fix .

typecheck:
	@uv run --quiet mypy --no-error-summary . && echo "✓ types clean (exit 0)"

check: lint typecheck coverage  # the full gate: lint -> typecheck -> coverage
