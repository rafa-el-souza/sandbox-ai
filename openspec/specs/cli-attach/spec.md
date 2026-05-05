## Purpose

This specification defines the `sandbox attach` command lifecycle, governing warm state verification and PTY handover without re-hydration.

## Requirements

### Requirement: Warm State Verification Before Attach
The system SHALL verify that the sandbox's containers are running before attempting to drop the user into the admin container.

#### Scenario: Running sandbox allows attach
- **WHEN** `sandbox attach` is invoked and `docker compose ps -q` returns non-empty output
- **THEN** the CLI proceeds to hand the terminal over to the admin container

#### Scenario: Stopped sandbox rejects attach
- **WHEN** `sandbox attach` is invoked and no containers are running for the instance
- **THEN** the CLI exits with: "Sandbox '<name>' is not running. Use 'sandbox start' to launch."

### Requirement: PTY Handover Without Re-Hydration
The system SHALL drop the user into the admin container via machinectl and `docker exec -it` without re-running hydration, credential generation, or IPAM allocation. The machinectl invocation SHALL use the configured authentication mode from host config.

#### Scenario: Attach bypasses hydration
- **WHEN** `sandbox attach` completes its warm state check successfully
- **THEN** no Jinja2 templates are rendered, no `.htpasswd` is regenerated, and no IPAM ledger is read or modified

#### Scenario: Terminal handed to admin container (sudo mode)
- **WHEN** containers are confirmed running and `machinectl_authentication` is `"sudo"`
- **THEN** `sudo machinectl shell <docker_unprivileged_user>@.host /usr/bin/docker exec -it <name>-admin-1 zsh` is executed

#### Scenario: Terminal handed to admin container (polkit mode)
- **WHEN** containers are confirmed running and `machinectl_authentication` is `"polkit"`
- **THEN** `machinectl shell <docker_unprivileged_user>@.host /usr/bin/docker exec -it <name>-admin-1 zsh` is executed without `sudo` prefix


### Requirement: Per-User State Initialization Required
The `sandbox attach` command SHALL refuse to operate when the per-user state tree is not initialized. Initialization is signaled by the presence of `<sandbox_ai_user_home()>/state/instances.json`. On absence, the command SHALL exit with a clear error directing the operator to run `sandbox init`.

#### Scenario: Attach on uninitialized host
- **WHEN** `sandbox attach` is invoked and `<home>/state/instances.json` does not exist
- **THEN** the CLI exits with: "Error: per-user state not initialized at `<resolved-home>`. Run `sandbox init` first." and exit code 1

#### Scenario: Resolved home in error message
- **WHEN** the attach command above runs with `SANDBOX_AI_USER_HOME=/tmp/test-home` set
- **THEN** the error message contains `/tmp/test-home`
