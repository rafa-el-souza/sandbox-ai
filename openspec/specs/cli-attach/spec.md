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
The system SHALL drop the user into the admin container via machinectl and `docker exec -it` without re-running hydration, credential generation, or IPAM allocation.

#### Scenario: Attach bypasses hydration
- **WHEN** `sandbox attach` completes its warm state check successfully
- **THEN** no Jinja2 templates are rendered, no `.htpasswd` is regenerated, and no IPAM ledger is read or modified

#### Scenario: Terminal handed to admin container
- **WHEN** containers are confirmed running
- **THEN** `sudo machinectl shell <host_unprivileged_user>@.host /usr/bin/docker exec -it <name>-admin-1 zsh` is executed and the user's terminal is owned by that session
