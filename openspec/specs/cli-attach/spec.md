## Purpose

This specification defines the `sandbox attach` command lifecycle, governing warm state verification and PTY handover without re-hydration.

## Requirements

### Requirement: Optional Workspace Argument

`sandbox attach <inst> [<ws-name>]` SHALL accept an optional workspace name. When the instance has exactly one workspace (N=1), the argument MAY be omitted; the system defaults to that single workspace. When N>1 and the argument is omitted, the system SHALL exit with the list of available workspaces and exit code 1. When the argument is supplied, it MUST exist in `sandbox.toml [workspaces]`.

The default-when-N=1 is computed at attach time (no "default workspace" field stored in sandbox.toml or registry).

#### Scenario: N=1 omitted argument defaults
- **WHEN** `sandbox attach foo` is invoked and `foo` has exactly one workspace `main`
- **THEN** the system defaults to `main` and proceeds to PTY handover with cwd `/workspaces/main`

#### Scenario: N>1 omitted argument errors
- **WHEN** `sandbox attach foo` is invoked and `foo` has multiple workspaces
- **THEN** the CLI exits with: "Multiple workspaces in 'foo'. Pick one: <list>." and exit code 1

#### Scenario: Unknown workspace argument rejected
- **WHEN** `sandbox attach foo nonexistent` is invoked and `nonexistent` is not in `[workspaces]`
- **THEN** the CLI exits with a "workspace not found" error

#### Scenario: docker exec sets cwd to /workspaces/<ws>
- **WHEN** attach proceeds to PTY handover with workspace `<ws>` resolved
- **THEN** the `docker exec` command includes `-w /workspaces/<ws>` to set the in-container cwd

### Requirement: Per-Instance Backup Lock Check

`sandbox attach <inst>` SHALL check `<inst>.backup.lock` and refuse fast if held. (Attach against a stopped-but-being-backed-up instance is rejected; the instance must complete the backup before attach can proceed — although in practice attach against a non-running instance fails the warm-state check first.)

#### Scenario: Concurrent backup blocks attach
- **WHEN** `sandbox attach <inst>` is invoked while `<inst>.backup.lock` is held
- **THEN** attach exits with a "Backup in progress" error

### Requirement: Warm State Verification Before Attach
The system SHALL verify that the sandbox's containers are running before attempting to drop the user into the admin container.

#### Scenario: Running sandbox allows attach
- **WHEN** `sandbox attach <inst>` is invoked and `docker compose ps -q` returns non-empty output
- **THEN** the CLI proceeds to hand the terminal over to the admin container

#### Scenario: Stopped sandbox rejects attach
- **WHEN** `sandbox attach <inst>` is invoked and no containers are running for the instance
- **THEN** the CLI exits with: "Sandbox '<inst>' is not running. Use 'sandbox start <inst>' to launch."

### Requirement: PTY Handover Without Re-Hydration
The system SHALL drop the user into the admin container via machinectl and `docker exec -it` without re-running hydration, credential generation, or IPAM allocation. The machinectl invocation SHALL use the configured authentication mode from host config. The `docker exec` invocation SHALL include `-w /workspaces/<ws>` to set the in-container cwd to the resolved workspace.

#### Scenario: Attach bypasses hydration
- **WHEN** `sandbox attach <inst>` completes its warm state check successfully
- **THEN** no Jinja2 templates are rendered, no `.htpasswd` is regenerated, and no IPAM ledger is read or modified

#### Scenario: Terminal handed to admin container with workspace cwd (sudo mode)
- **WHEN** containers are confirmed running, `state.lock` is released, and `machinectl_authentication` is `"sudo"`, with workspace `<ws>` resolved
- **THEN** `sudo machinectl shell <docker_unprivileged_user>@.host /usr/bin/docker exec -it -w /workspaces/<ws> <inst>-admin-1 zsh` is executed

#### Scenario: Terminal handed to admin container with workspace cwd (polkit mode)
- **WHEN** containers are confirmed running, `state.lock` is released, and `machinectl_authentication` is `"polkit"`, with workspace `<ws>` resolved
- **THEN** `machinectl shell <docker_unprivileged_user>@.host /usr/bin/docker exec -it -w /workspaces/<ws> <inst>-admin-1 zsh` is executed without `sudo` prefix


### Requirement: Per-User State Initialization Required
The `sandbox attach` command SHALL refuse to operate when the per-user state tree is not initialized. Initialization is signaled by the presence of `<sandbox_ai_user_home()>/state/instances.json`. On absence, the command SHALL exit with a clear error directing the operator to run `sandbox init`.

#### Scenario: Attach on uninitialized host
- **WHEN** `sandbox attach` is invoked and `<home>/state/instances.json` does not exist
- **THEN** the CLI exits with: "Error: per-user state not initialized at `<resolved-home>`. Run `sandbox init` first." and exit code 1

#### Scenario: Resolved home in error message
- **WHEN** the attach command above runs with `SANDBOX_AI_USER_HOME=/tmp/test-home` set
- **THEN** the error message contains `/tmp/test-home`
