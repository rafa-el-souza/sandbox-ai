## Purpose

This specification defines the `.sandbox.env` secrets file schema, governing secure file creation, component-conditional secret blocks, interactive prompting, and non-interactive fallback behavior.

## Requirements

### Requirement: Secure File Creation
The system SHALL create `.sandbox.env` with owner-read-only permissions atomically, with no race window between file creation and permission application.

#### Scenario: File created with mode 0600
- **WHEN** scaffold creates `.sandbox.env` for the first time
- **THEN** the file is opened via `os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o600)` and the mode is 0600 from the moment the file exists on disk

### Requirement: Component-Conditional Secret Blocks
The system SHALL write only the secret variable blocks for components that are enabled in `sandbox.toml`.

#### Scenario: Disabled component omits secrets
- **WHEN** `components.db_postgres = false`
- **THEN** the `PG_USER`, `PG_PASSWORD`, and `PG_DB` variables are NOT written to `.sandbox.env`

#### Scenario: Enabled component includes secrets
- **WHEN** `components.db_postgres = true`
- **THEN** the `PG_USER`, `PG_PASSWORD`, and `PG_DB` variables ARE written to `.sandbox.env`, with `PG_USER` and `PG_DB` prefilled with defaults and `PG_PASSWORD` left empty for the user to fill

### Requirement: Interactive Secret Prompting
The system SHALL collect required secret values interactively using `getpass()` when a TTY is attached, with no echo to the terminal.

#### Scenario: Required secret collected via getpass
- **WHEN** scaffold runs in an interactive TTY context and `CORE_ANTHROPIC_API_KEY` is empty in `.sandbox.env`
- **THEN** the CLI prompts via `getpass.getpass("CORE_ANTHROPIC_API_KEY (Anthropic API key for the agent): ")` and writes the returned value to `.sandbox.env`

#### Scenario: Collected secrets not echoed
- **WHEN** the user types their secret at the `getpass()` prompt
- **THEN** the entered characters are NOT displayed on the terminal and NOT written to shell history

### Requirement: Non-Interactive Fallback
The system SHALL fail immediately with a clear path reference when required secrets are absent and no TTY is attached.

#### Scenario: Missing secrets in CI context
- **WHEN** scaffold runs without a TTY (e.g., CI pipeline) and required secrets are empty in `.sandbox.env`
- **THEN** the CLI exits with: "Sandbox initialized. Populate .sandbox.env at <path> and re-run sandbox start."
