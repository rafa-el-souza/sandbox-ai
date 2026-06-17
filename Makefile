.PHONY: test test-file test-integration coverage lint format typecheck pyright check
SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c

# NOTE: `format` applies ONLY ruff's import-sort autofix (`ruff check --fix
# --select I`). It deliberately does NOT run `ruff format` (reflows the manual
# line-break style) nor the broad `ruff check --fix` (its `UP`/`RUF`/`B` fixes
# mangled 89 files + stripped `()` from exception calls). Import-sort is the one
# autofix that is provably non-destructive to this project's manual style.
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

format:  # apply ONLY the import-sort autofix (safe; never reflows or rewrites code)
	@uv run --quiet ruff check --fix --select I .

typecheck:
	@uv run --quiet mypy --no-error-summary . && echo "✓ types clean (exit 0)"

pyright:  # pyright --strict (src fully strict; tests scoped per pyrightconfig.json). Prints "0 errors …" on a clean pass.
	@uv run --quiet pyright

check: lint typecheck pyright coverage  # the full gate: lint -> typecheck (mypy) -> pyright -> coverage
