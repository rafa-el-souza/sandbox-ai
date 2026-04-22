## Purpose

This specification defines the dynamic CLAUDE.md rendering pipeline that generates agent containment rules and system boundary documentation from sandbox component state on every `sandbox start`.

## Requirements

### Requirement: Dynamic CLAUDE.md Rendering
The system SHALL render `CLAUDE.md` as a Jinja2 template on every `sandbox start`, producing a file with three sections: (1) immutable sandbox containment rules, (2) component-conditional system boundaries, and (3) optional user custom rules. The rendered output SHALL be re-generated on every cold start to track `sandbox.toml` component state.

#### Scenario: CLAUDE.md rendered with base containment rules
- **WHEN** `render_templates()` processes `core/CLAUDE.md`
- **THEN** the rendered output contains the sandbox containment rules section (rootless container constraints, proxy pre-configuration notice, DNS restriction notice)

#### Scenario: CLAUDE.md includes PostgreSQL boundaries when enabled
- **WHEN** `render_templates()` runs with `db_postgres_enabled = True` in the context
- **THEN** the rendered `CLAUDE.md` contains a system boundary entry for PostgreSQL with the host (`db-postgres`), port (`5432`), user (`{{ pg_user }}`), and database (`{{ pg_db }}`)

#### Scenario: CLAUDE.md omits PostgreSQL boundaries when disabled
- **WHEN** `render_templates()` runs with `db_postgres_enabled = False` in the context
- **THEN** the rendered `CLAUDE.md` does not contain any PostgreSQL connection details

#### Scenario: CLAUDE.md includes Firecrawl boundaries when enabled
- **WHEN** `render_templates()` runs with `mcp_firecrawl_enabled = True` in the context
- **THEN** the rendered `CLAUDE.md` contains a system boundary entry for Firecrawl MCP

#### Scenario: CLAUDE.md omits Firecrawl boundaries when disabled
- **WHEN** `render_templates()` runs with `mcp_firecrawl_enabled = False` in the context
- **THEN** the rendered `CLAUDE.md` does not contain any Firecrawl reference

### Requirement: User Custom Rules Concatenation
The system SHALL load user-authored custom agent rules from `custom/config/core/CLAUDE.md` in the instance directory and concatenate them into the rendered `CLAUDE.md` output. The custom rules SHALL appear after the sandbox-managed sections, under a clearly delimited comment block.

#### Scenario: Custom rules present — concatenated into rendered output
- **WHEN** `custom/config/core/CLAUDE.md` exists in the instance directory and contains content
- **THEN** the rendered `CLAUDE.md` includes the file's content after the system boundaries section, within a `<!-- USER_CUSTOM_RULES -->` comment block

#### Scenario: Custom rules absent — rendered output omits custom section
- **WHEN** `custom/config/core/CLAUDE.md` does not exist in the instance directory
- **THEN** the rendered `CLAUDE.md` contains only the containment rules and system boundaries sections (no empty custom rules block)

#### Scenario: Custom rules empty — rendered output omits custom section
- **WHEN** `custom/config/core/CLAUDE.md` exists but is empty or whitespace-only
- **THEN** the rendered `CLAUDE.md` contains only the containment rules and system boundaries sections

### Requirement: CLAUDE.md Read-Only Global Mount
The rendered `CLAUDE.md` SHALL be bind-mounted into the core container at `/home/agent/.claude/CLAUDE.md` with `:ro` mode, as a file overlay on top of the `:rw` directory mount for `/home/agent/.claude/`. This ensures the agent can write to the `.claude/` directory (cache state) but cannot modify the containment rules.

#### Scenario: CLAUDE.md mounted read-only at global scope
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it contains a volume entry `{{ instance_dir }}/config/core/CLAUDE.md:/home/agent/.claude/CLAUDE.md:ro`

#### Scenario: File overlay after directory mount
- **WHEN** the rendered `compose.yml` core service volume list is inspected
- **THEN** the CLAUDE.md file mount appears after the `.claude/` directory mount in declaration order

### Requirement: Optional File Reader Helper
The system SHALL provide a `_read_optional_file(path: str) -> str` helper in `hydration.py` that returns the file's contents (trailing whitespace stripped) if the file exists, or an empty string if the file does not exist. This helper SHALL be used by `build_jinja_context()` to load `custom_claude_rules`.

#### Scenario: File exists — contents returned
- **WHEN** `_read_optional_file()` is called with a path to an existing file containing `"custom rules\n"`
- **THEN** it returns `"custom rules"`

#### Scenario: File absent — empty string returned
- **WHEN** `_read_optional_file()` is called with a path to a non-existent file
- **THEN** it returns `""`

### Requirement: DO NOT EDIT Header
The rendered `CLAUDE.md` SHALL include a comment header indicating it is auto-generated and directing users to the custom override path.

#### Scenario: Header present in rendered output
- **WHEN** the rendered `CLAUDE.md` is inspected
- **THEN** it begins with a comment containing "DO NOT EDIT" and a reference to `custom/config/core/CLAUDE.md` as the override path
