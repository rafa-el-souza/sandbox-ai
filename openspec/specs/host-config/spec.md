## Purpose

This specification defines the `sandbox-ai.toml` per-host orchestrator configuration file — schema, location, Pydantic model, loader interface, and the centralized machinectl command prefix builder that consumes it.

## Requirements

### Requirement: Host Config File Location and Format
The system SHALL read per-host orchestrator configuration from a file named `sandbox-ai.toml` located in the project root directory. The file SHALL use TOML format with a `[host]` section.

#### Scenario: Valid host config parsed
- **WHEN** `sandbox-ai.toml` exists in the project directory with a valid `[host]` section
- **THEN** the system parses it into a `HostConfig` Pydantic model without errors

#### Scenario: Host config not found
- **WHEN** `sandbox-ai.toml` does not exist in the project directory and no CLI override flags are provided
- **THEN** the loader raises `FileNotFoundError` which callers translate to a user-facing error: "No sandbox-ai.toml found. Create one or pass --user."

#### Scenario: Invalid TOML rejected
- **WHEN** `sandbox-ai.toml` contains malformed TOML syntax
- **THEN** the loader raises a parse error before any state changes occur

### Requirement: Host Config Schema
The `[host]` section SHALL contain `docker_unprivileged_user` (required string) and `machinectl_authentication` (string enum, default `"sudo"`). The `machinectl_authentication` field SHALL accept exactly two values: `"sudo"` and `"polkit"`.

#### Scenario: Both fields present
- **WHEN** `sandbox-ai.toml` contains `[host]` with `docker_unprivileged_user = "sandbox"` and `machinectl_authentication = "polkit"`
- **THEN** the model validates successfully with `docker_unprivileged_user == "sandbox"` and `machinectl_authentication == MachinectlAuth.POLKIT`

#### Scenario: Authentication defaults to sudo
- **WHEN** `sandbox-ai.toml` contains `[host]` with `docker_unprivileged_user` but omits `machinectl_authentication`
- **THEN** the model applies default `machinectl_authentication = "sudo"`

#### Scenario: Invalid authentication value rejected
- **WHEN** `sandbox-ai.toml` contains `machinectl_authentication = "pkexec"`
- **THEN** the Pydantic model raises a `ValidationError` identifying the invalid enum value

#### Scenario: Missing docker_unprivileged_user rejected
- **WHEN** `sandbox-ai.toml` contains `[host]` without `docker_unprivileged_user`
- **THEN** the Pydantic model raises a `ValidationError` identifying the missing required field

### Requirement: Path-Parameterized Loader
The `HostConfig.from_toml()` class method SHALL accept an explicit `project_dir: str` parameter and SHALL NOT read `os.getcwd()` internally.

#### Scenario: Loader uses provided path
- **WHEN** `HostConfig.from_toml("/some/project")` is called
- **THEN** the loader reads `/some/project/sandbox-ai.toml` regardless of the process CWD

### Requirement: Pydantic Model Structure
The host config model SHALL use a `MachinectlAuth` StrEnum for the authentication field and a nested `HostSettings` model for the `[host]` section.

#### Scenario: MachinectlAuth enum members
- **WHEN** the `MachinectlAuth` enum is inspected
- **THEN** it contains exactly two members: `SUDO = "sudo"` and `POLKIT = "polkit"`

#### Scenario: HostSettings nested model
- **WHEN** a `HostConfig` is loaded
- **THEN** `host_config.host` is a `HostSettings` instance with `docker_unprivileged_user` and `machinectl_authentication` attributes

### Requirement: Centralized machinectl Command Prefix Builder
The system SHALL provide a `machinectl_cmd(user, auth)` function that returns the complete machinectl shell prefix as a `list[str]`. All machinectl invocations across the CLI and doctor modules SHALL use this function.

#### Scenario: Sudo mode prefix
- **WHEN** `machinectl_cmd("sandbox", MachinectlAuth.SUDO)` is called
- **THEN** it returns `["sudo", "machinectl", "shell", "sandbox@.host"]`

#### Scenario: Polkit mode prefix
- **WHEN** `machinectl_cmd("sandbox", MachinectlAuth.POLKIT)` is called
- **THEN** it returns `["machinectl", "shell", "sandbox@.host"]`

### Requirement: Module Location
The `HostConfig` model, `MachinectlAuth` enum, `HostSettings` model, and `machinectl_cmd` function SHALL reside in `core/host_config.py`.

#### Scenario: Import path
- **WHEN** other modules need host config or machinectl prefix building
- **THEN** they import from `core.host_config`
