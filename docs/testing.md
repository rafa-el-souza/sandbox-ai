# Testing

## Commands

The project pins Python 3.14 (`.python-version`) and depends on [`uv`](https://docs.astral.sh/uv/). Never call `python` directly — always go through `uv run` or `make`.

```bash
make test                       # unit tests (tests/unit)
make test-integration           # subprocess-level integration tests (tests/integration)
make test-file FILE=tests/unit/core/test_ipam.py   # single file
uv run pytest tests/unit/core/test_ipam.py::test_name   # single test
make coverage                   # enforces 100% on core/ + cli/
make lint                       # ruff
make typecheck                  # mypy --strict
```

`pytest.testpaths = ["tests/unit"]` — integration tests under `tests/integration/` are not collected by default and must be invoked explicitly.

The CLI entrypoint is `sandbox = "cli.main:app"` (typer). Run as `uv run sandbox <cmd>`.

## Conventions

- 100% coverage gate on `src/core/` and `src/cli/` — new code without tests will fail `make coverage`.
- mypy is strict, ruff line-length 120, target `py314`. Selected rules: `E,F,W,I,UP,B,SIM,TCH,RUF`.
- `src/cli/__main__.py` is excluded from coverage (`tool.coverage.run.omit`).
- Tests live in `tests/unit/` mirroring the `src/core/` and `src/cli/` layout.

### Testing

The following test conventions are enforced by meta-tests at `tests/unit/test_layout.py` and `tests/unit/test_conventions.py`. A new contribution that violates them fails `make coverage`.

- **Layout mirrors source.** Every `src/<pkg>/<module>.py` requires a test at `tests/unit/<pkg>/test_<module>.py`. Exceptions go in `_LAYOUT_ALLOWLIST` with a one-line reason (visible in diffs). The reverse direction is also checked: orphan test files (no matching src module) fail unless allowlisted (e.g., tests for `scripts/` developer tools).
- **Fixtures over plain helpers.** Cross-file shared setup uses pytest fixtures defined in the relevant `conftest.py`. Importing helper *functions* from a `conftest.py` at runtime is forbidden (`from tests.X.conftest import _helper`). Type-only imports under `if TYPE_CHECKING:` are fine — those are protocols/aliases, not behavior. Discover available fixtures via `uv run pytest --fixtures <test-file>`.
- **Markers must be registered.** Every custom `@pytest.mark.X` is declared in `pyproject.toml`'s `[tool.pytest.ini_options].markers` list with a one-line description. Pytest builtins (`parametrize`, `usefixtures`, `skip`, `skipif`, `xfail`, `filterwarnings`) are exempt. Existing custom markers — `integration`, `no_warm_mock`, `no_host_config_mock`, `no_seed_registry` — follow the opt-out-of-autouse pattern: tests that need to bypass an autouse fixture (e.g., to exercise the real function) mark themselves accordingly.
- **No suppression directives.** `# noqa`, `# type: ignore`, `# pragma: no cover` are forbidden in `src/` and `tests/`. Restructure the code (use `cast(...)`, the `setattr(obj, name, value)` runtime path, string-form `monkeypatch.setattr("module.name", ...)`, etc.) instead of silencing the linter. Pre-existing exceptions are tracked explicitly in `tests/unit/test_conventions.py::_GRANDFATHERED_SUPPRESSIONS` with a corresponding deferred-work entry documenting how to remove them.
- **Pre-fix verification protocol.** When an OpenSpec change ships a regression test, the test must demonstrably FAIL against the pre-fix tree. The standard recipe (logged inline in the change's `tasks.md`): `git stash -u && git checkout main -- <changed-files> && make test-file FILE=<new-test>` → record observed failure → `git stash pop && git checkout HEAD -- <changed-files>`. The recorded failure is a load-bearing artifact: it proves the test catches the bug class, not just the absence of the symptom.
