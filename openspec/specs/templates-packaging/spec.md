## Purpose

This specification defines how Jinja2 templates and static config files are packaged, distributed, and discovered at runtime. Templates ship as a top-level Python package (`templates`) inside the wheel and are loaded via Jinja2's `PackageLoader` backed by `importlib.resources`, so they work identically from a wheel install and from a development checkout — without any filesystem path configuration.

## Requirements

### Requirement: Templates Ship as a Python Package

Jinja2 templates SHALL be distributed with the orchestrator as a top-level Python package named `templates`. The package SHALL contain empty `__init__.py` markers in every directory that contains template files (recursively), so that `importlib.resources.files("templates")` returns a `Traversable` rooted at the package and traversal into subdirectories is discoverable.

#### Scenario: Templates package importable from wheel install
- **WHEN** the orchestrator is installed via `uv tool install .`
- **THEN** `import templates` succeeds and `importlib.resources.files("templates")` returns a `Traversable` representing the templates package root

#### Scenario: Templates package importable from development checkout
- **WHEN** the orchestrator is run via `uv run sandbox …` from a development checkout
- **THEN** `import templates` succeeds and `importlib.resources.files("templates")` returns a `Traversable` representing the templates package root (resolving to `src/templates/` in the source tree)

#### Scenario: Subdirectory traversal works
- **WHEN** `importlib.resources.files("templates").joinpath("docker", "compose.yml")` is evaluated
- **THEN** the result is a `Traversable` referring to the compose template, regardless of whether the orchestrator is wheel-installed or run from source

#### Scenario: Every template subdirectory has an `__init__.py`
- **WHEN** the templates package is inspected
- **THEN** every directory containing template files (e.g., `templates/config/proxy/`, `templates/docker/extras/`, `templates/config/coredns/`) contains an empty `__init__.py` marker

### Requirement: Wheel Packaging via Hatch

The Python build backend SHALL package `src/cli`, `src/core`, and `src/templates` as three top-level wheel packages. The wheel installation SHALL produce three importable top-level modules: `cli`, `core`, `templates`.

#### Scenario: Three packages in the wheel
- **WHEN** the wheel is built (e.g., via `uv build` or `hatch build`)
- **THEN** the wheel's `RECORD` lists files under `cli/`, `core/`, and `templates/` (top-level), each containing the corresponding source tree

#### Scenario: pyproject.toml wheel target lists three packages
- **WHEN** `pyproject.toml` is inspected
- **THEN** `[tool.hatch.build.targets.wheel].packages` is `["src/cli", "src/core", "src/templates"]` (or equivalent), explicitly enumerating the three roots

#### Scenario: Templates ship in the wheel
- **WHEN** the wheel is built and inspected
- **THEN** every file under `src/templates/` (Jinja2 templates, static config files, `__init__.py` markers) is present in the wheel under the `templates/` top-level path

### Requirement: Template Discovery via Jinja2 PackageLoader

The hydration pipeline SHALL load templates via `jinja2.PackageLoader("templates", package_path="")`. The empty `package_path` is required because the package itself is named `templates`; PackageLoader's default `package_path="templates"` would otherwise look for a `templates/templates/` subdirectory.

#### Scenario: PackageLoader constructed with package_path=""
- **WHEN** the hydration pipeline initializes its Jinja2 `Environment`
- **THEN** the loader is `jinja2.PackageLoader("templates", package_path="")`

#### Scenario: Default package_path would fail
- **WHEN** the loader is constructed as `jinja2.PackageLoader("templates")` (default `package_path="templates"`)
- **THEN** template lookups fail because `templates/templates/` does not exist in the package

#### Scenario: Includes and extends resolve correctly
- **WHEN** a template uses `{% include %}` or `{% extends %}` referring to another template by package-relative path (e.g., `{% include "config/shared.j2" %}`)
- **THEN** PackageLoader resolves the include via `importlib.resources` traversal and renders successfully

### Requirement: Templates Are Read-Only at Runtime

The templates package SHALL NOT be modified by the orchestrator at runtime. All write operations target the per-instance directory (`<instance_dir>/`) or per-user state (`~/.sandbox-ai/`). The templates package is the immutable source plane.

#### Scenario: Hydration writes only to instance directory
- **WHEN** `render_templates()` completes
- **THEN** every write occurred under `<instance_dir>/` (or its subdirectories); the templates package on the filesystem (or in the wheel) is unchanged

#### Scenario: No write helpers target the templates package
- **WHEN** the orchestrator source is inspected for write operations against the templates package
- **THEN** no `open(..., "w")`, `Path.write_text()`, `shutil.copy*` (with templates as destination), or similar mutating call exists with a templates-package destination

### Requirement: No Filesystem Path Configuration for Templates

The hydration pipeline's public API (`render_templates`, `validate_templates`) SHALL NOT accept a filesystem path argument for template discovery. Template location is determined entirely by the Python package mechanism.

#### Scenario: render_templates signature has no template-path argument
- **WHEN** the `render_templates` function signature is inspected
- **THEN** it does not include a `tooling_plane: str` (or analogous) parameter; the function takes only `context`, `instance_dir`, and the keyword-only feature flags

#### Scenario: validate_templates signature has no template-path argument
- **WHEN** the `validate_templates` function signature is inspected
- **THEN** it does not include a `tooling_plane: str` parameter; the function takes only `context` and the keyword-only feature flags

#### Scenario: No environment variable overrides template location
- **WHEN** the orchestrator runs with any environment variable set (e.g., `TEMPLATES_DIR`, `SANDBOX_AI_TEMPLATES_PATH`)
- **THEN** the template location is unaffected — only the `templates` Python package is consulted
