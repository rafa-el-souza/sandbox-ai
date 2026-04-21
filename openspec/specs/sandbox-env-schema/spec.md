## Purpose

This specification defines the `.sandbox.env` secrets file schema, governing secure file creation, component-conditional secret blocks, interactive prompting, and non-interactive fallback behavior.

## Requirements

### Requirement: Secure File Creation
The system SHALL create `.sandbox.env` with owner-read-only permissions atomically, with no race window between file creation and permission application.

#### Scenario: File created with mode 0600
- **WHEN** scaffold creates `.sandbox.env` for the first time
- **THEN** the file is opened via `os.open(path, O_WRONLY | O_CREAT | O_EXCL, 0o600)` and the mode is 0600 from the moment the file exists on disk

### Requirement: Component-Conditional Secret Blocks
The system SHALL write only the secret variable blocks for components that are enabled in `sandbox.toml`. Auto-generated secrets SHALL be pre-populated with cryptographically secure values at scaffold time.

#### Scenario: Disabled component omits secrets
- **WHEN** `components.db_postgres = false`
- **THEN** the `PG_USER`, `PG_PASSWORD`, and `PG_DB` variables are NOT written to `.sandbox.env`

#### Scenario: Enabled database component auto-generates password
- **WHEN** `components.db_postgres = true` and `create_env_file()` runs
- **THEN** `PG_PASSWORD` is pre-populated with a value from `generate_credential()` (43-character base64url string, 256 bits entropy) and `PG_USER`/`PG_DB` are prefilled with defaults

#### Scenario: Enabled component with user-provided secret leaves placeholder
- **WHEN** `components.mcp_firecrawl = true` and `create_env_file()` runs
- **THEN** `FIRECRAWL_API_KEY` is written with an empty value (`FIRECRAWL_API_KEY=""`) for the user to populate

### Requirement: Interactive Secret Prompting
The system SHALL collect required secret values interactively using `getpass()` when a TTY is attached, with no echo to the terminal. Auto-generated secrets SHALL NOT be included in the interactive prompt list.

#### Scenario: Required secret collected via getpass
- **WHEN** scaffold runs in an interactive TTY context and `CORE_ANTHROPIC_API_KEY` is empty in `.sandbox.env`
- **THEN** the CLI prompts via `getpass.getpass("CORE_ANTHROPIC_API_KEY (Anthropic API key for the agent): ")` and writes the returned value to `.sandbox.env`

#### Scenario: Auto-generated secret not prompted
- **WHEN** scaffold runs in an interactive TTY context and `PG_PASSWORD` was auto-generated
- **THEN** the CLI does NOT prompt for `PG_PASSWORD` — the pre-populated value is used as-is

#### Scenario: Collected secrets not echoed
- **WHEN** the user types their secret at the `getpass()` prompt
- **THEN** the entered characters are NOT displayed on the terminal and NOT written to shell history

### Requirement: Credential Generation Function
The system SHALL provide a single credential-type-agnostic function `generate_credential()` for generating cryptographically secure credential strings.

#### Scenario: Generated credential meets entropy requirements
- **WHEN** `generate_credential()` is called
- **THEN** it returns a 43-character base64url string produced by `secrets.token_urlsafe(32)` (256 bits entropy)

#### Scenario: Proxy password uses generate_credential
- **WHEN** the proxy authentication credential is generated during `sandbox start`
- **THEN** it is produced by calling `generate_credential()` (formerly `generate_proxy_password()`)

### Requirement: Non-Interactive Fallback
The system SHALL fail immediately with a clear path reference when required secrets are absent and no TTY is attached.

#### Scenario: Missing secrets in CI context
- **WHEN** scaffold runs without a TTY (e.g., CI pipeline) and required secrets are empty in `.sandbox.env`
- **THEN** the CLI exits with: "Sandbox initialized. Populate .sandbox.env at <path> and re-run sandbox start."
