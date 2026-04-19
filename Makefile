.PHONY: test coverage lint format typecheck

coverage:
	uv run --with pytest-cov pytest --cov=core --cov=cli --cov-report=term-missing tests/unit/

test:
	uv run pytest tests/unit/

lint:
	uv run ruff check core/ tests/

format:
	uv run ruff format core/ tests/
	uv run ruff check --fix core/ tests/

typecheck:
	uv run mypy core/ tests/
