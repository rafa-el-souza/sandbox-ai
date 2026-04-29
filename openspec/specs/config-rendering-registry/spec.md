## Purpose

This specification defines the authoritative file rendering registry that governs which files are processed as Jinja2 templates versus static copies, and the context contract enforcement model that ensures template completeness.

## Requirements

### Requirement: Authoritative Rendering Registry
The system SHALL declare two module-level lists — `_JINJA_RENDERED_DOCKER` and `_JINJA_RENDERED_CONFIG` — as the single authoritative registry of files processed by both `render_templates()` and `validate_templates()`. Each entry SHALL be a `(source_relative, destination_relative)` tuple. No other code path SHALL maintain a separate enumeration of rendered templates.

#### Scenario: Registry lists consumed by render_templates
- **WHEN** `render_templates()` processes the standard template set (excluding distro-selected Dockerfiles, static copies, feature-gated extras, and programmatically generated files)
- **THEN** it iterates over `_JINJA_RENDERED_DOCKER` and `_JINJA_RENDERED_CONFIG`, calling `_render_file()` for each `(source, destination)` tuple

#### Scenario: Registry lists consumed by validate_templates
- **WHEN** `validate_templates()` builds its template validation list
- **THEN** it includes all entries from `_JINJA_RENDERED_DOCKER` and `_JINJA_RENDERED_CONFIG` (in addition to distro-selected Dockerfiles and feature-gated extras)

#### Scenario: Registry covers all Jinja2-rendered config files
- **WHEN** `_JINJA_RENDERED_CONFIG` is inspected
- **THEN** it contains entries for `coredns/Corefile`, `dnsdist/dnsdist.conf`, `proxy/squid.conf`, `core/.gitconfig`, `core/.npmrc`, `core/.bashrc`, `core/CLAUDE.md`, `core/sshd_config`, `admin/.zshrc`, `admin/.tmux.conf`, and `admin/.gitconfig`

### Requirement: Static Config Minimization
The system SHALL classify files as static (copied via `shutil.copy2`) only when they contain zero Jinja2 template syntax. Files containing `{{ }}` or `{% %}` markers SHALL be in `_JINJA_RENDERED_CONFIG`.

#### Scenario: Reduced static config lists
- **WHEN** `_STATIC_CONFIG_CORE`, `_STATIC_CONFIG_ADMIN`, and `_STATIC_CONFIG_PROXY` are inspected
- **THEN** `_STATIC_CONFIG_CORE` is empty (`.claude.json` removed — now programmatically generated), `_STATIC_CONFIG_ADMIN` contains only `gitmux.conf` and `starship.toml`, and `_STATIC_CONFIG_PROXY` contains only `ERR_SANDBOX_403`

### Requirement: Context Contract Enforcement
The system SHALL resolve all template variable defaults in `build_jinja_context()`. Templates SHALL use bare `{{ var }}` without Jinja2 `| default()` filters. The `jinja2.StrictUndefined` configuration SHALL enforce completeness — any variable referenced in a template but absent from the context SHALL raise `UndefinedError` at render time and during `--dry-run` validation.

#### Scenario: Context builder docstring documents the contract
- **WHEN** `build_jinja_context()` is inspected
- **THEN** its docstring or a comment block documents the contract: all defaults resolved here, no `| default()` filters in templates, `StrictUndefined` enforces completeness

#### Scenario: StrictUndefined catches missing key in new template
- **WHEN** a newly-rendered config template (e.g., `.gitconfig`) references a variable not present in the context
- **THEN** `jinja2.StrictUndefined` raises `UndefinedError` during both `render_templates()` and `validate_templates()`

### Requirement: Post-Render Marker Scan
The system's unit test suite SHALL include a test that scans all files in the rendered instance directory for unresolved `{{ }}` markers after `render_templates()` completes. This tests the invariant "no rendered instance should contain template syntax" rather than the mechanism "this file is in the correct list".

#### Scenario: No unresolved Jinja2 markers in rendered output
- **WHEN** `render_templates()` completes with all components enabled
- **THEN** no file under `sandboxes/<id>/` contains a literal `{{` string
