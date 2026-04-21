## Purpose

This specification defines the `sandbox doctor` diagnostic command, which validates host readiness for sandbox operation by executing a dependency-ordered check pipeline covering binary availability, user existence, service state, Docker configuration, filesystem capabilities, and tooling plane integrity.

## Requirements

### Requirement: Doctor Command Interface
The system SHALL provide a `sandbox doctor --user <name>` command that validates host readiness for sandbox operation. The `--user` parameter SHALL be mandatory with no default value. The doctor module SHALL also expose a programmatic subset API for use by other commands.

#### Scenario: Doctor invoked with user parameter
- **WHEN** the operator runs `sandbox doctor --user sandbox`
- **THEN** the system executes all 12 diagnostic checks and reports results grouped by category

#### Scenario: Doctor invoked without user parameter
- **WHEN** the operator runs `sandbox doctor` without `--user`
- **THEN** the CLI exits with an error indicating that `--user` is required

### Requirement: Binary Availability Checks
The system SHALL verify that `sudo`, `machinectl`, and `setfacl` are present on the host PATH. These are root checks with no dependencies.

**Dependencies:** None (root checks)

#### Scenario: All binaries present
- **WHEN** `sudo`, `machinectl`, and `setfacl` are all resolvable via `shutil.which`
- **THEN** each reports PASS with the resolved path

#### Scenario: Binary missing
- **WHEN** any of `sudo`, `machinectl`, or `setfacl` is not found on PATH
- **THEN** that check reports FAIL with a distro-aware install command (e.g., `sudo apt install acl` on Debian/Ubuntu)

### Requirement: Unprivileged User Existence
The system SHALL verify that the user specified by `--user` exists on the host.

**Dependencies:** None (root check)

#### Scenario: User exists
- **WHEN** `id <user>` exits with code 0
- **THEN** the check reports PASS with the user's UID

#### Scenario: User does not exist
- **WHEN** `id <user>` exits with a non-zero code
- **THEN** the check reports FAIL with instructions to create the user

### Requirement: systemd-machined Service Check
The system SHALL verify that the `systemd-machined` service is active.

**Dependencies:** machinectl binary (Binary Availability)

#### Scenario: Service active
- **WHEN** `systemctl is-active systemd-machined` returns `active`
- **THEN** the check reports PASS

#### Scenario: Service inactive
- **WHEN** `systemctl is-active systemd-machined` returns any value other than `active`
- **THEN** the check reports FAIL with `sudo systemctl enable --now systemd-machined` as the remediation command

### Requirement: machinectl Shell Reachability
The system SHALL verify that the operator can shell into the unprivileged user via `sudo machinectl shell <user>@.host`. The probe SHALL use a 10-second timeout to detect sudoers misconfiguration (password prompt hang).

**Dependencies:** sudo binary, machinectl binary, Unprivileged User Existence, systemd-machined Service

#### Scenario: Shell reachable
- **WHEN** `sudo machinectl shell <user>@.host -- /bin/bash -c "echo ok"` completes successfully within 10 seconds
- **THEN** the check reports PASS

#### Scenario: Shell unreachable due to timeout
- **WHEN** the machinectl probe does not complete within 10 seconds
- **THEN** the check reports FAIL with guidance that the timeout likely indicates a sudoers password prompt, and provides remediation for passwordless machinectl access

#### Scenario: Shell unreachable due to error
- **WHEN** the machinectl probe fails with a non-zero exit code
- **THEN** the check reports FAIL with the stderr output and guidance on common causes (user not found by machined, service not running)

### Requirement: Docker Availability
The system SHALL verify that Docker is installed and accessible to the unprivileged user via machinectl.

**Dependencies:** machinectl Shell Reachability

#### Scenario: Docker available
- **WHEN** `docker version --format '{{.Server.Version}}'` (executed via machinectl as the unprivileged user) returns successfully
- **THEN** the check reports PASS with the Docker version

#### Scenario: Docker not available
- **WHEN** the `docker version` probe fails
- **THEN** the check reports FAIL with guidance on installing Docker in rootless mode for the unprivileged user

### Requirement: Docker Rootless Verification
The system SHALL verify that Docker is running in rootless mode under the unprivileged user.

**Dependencies:** Docker Availability

#### Scenario: Docker rootless confirmed
- **WHEN** `docker info --format '{{.SecurityOptions}}'` (executed via machinectl as the unprivileged user) contains `rootless`
- **THEN** the check reports PASS

#### Scenario: System Docker detected
- **WHEN** `docker info` does not contain `rootless` in its security options
- **THEN** the check reports FAIL, explaining that rootless Docker is a non-negotiable security boundary, with a link to Docker rootless setup documentation

### Requirement: gVisor Runtime Registration
The system SHALL verify that the `runsc` runtime is registered in Docker.

**Dependencies:** Docker Availability

#### Scenario: runsc registered
- **WHEN** `docker info` (via machinectl) lists `runsc` in its available runtimes
- **THEN** the check reports PASS

#### Scenario: runsc not registered
- **WHEN** `runsc` is absent from the Docker runtime list
- **THEN** the check reports FAIL with a link to gVisor installation documentation

### Requirement: Filesystem ACL Support
The system SHALL verify that the filesystem under SANDBOX_AI_HOME supports POSIX ACLs.

**Dependencies:** setfacl binary (Binary Availability)

#### Scenario: ACL supported
- **WHEN** `setfacl -m u:$(whoami):r <test_file>` succeeds on a temporary file within the repository and the ACL is cleaned up
- **THEN** the check reports PASS

#### Scenario: ACL not supported
- **WHEN** the `setfacl` probe fails
- **THEN** the check reports FAIL with guidance on filesystem mount options

### Requirement: Tooling Plane Integrity
The system SHALL verify that the 15 unconditional template and static files exist in `.docker/` and `.config/`.

**Dependencies:** None (root check)

#### Scenario: All files present
- **WHEN** all 15 required files exist at their expected paths
- **THEN** the check reports PASS with the file count

#### Scenario: Files missing
- **WHEN** one or more required files are absent
- **THEN** the check reports FAIL listing the missing files

### Requirement: State Directory Writability
The system SHALL verify that the `.state/` directory is writable.

**Dependencies:** None (root check)

#### Scenario: Directory writable
- **WHEN** a temporary file can be created and removed in `.state/`
- **THEN** the check reports PASS

#### Scenario: Directory not writable
- **WHEN** file creation in `.state/` fails
- **THEN** the check reports FAIL with guidance on directory permissions

### Requirement: Cascading Skip Logic
The system SHALL skip checks whose dependencies have failed, displaying skipped checks explicitly in the output.

#### Scenario: Dependent check skipped after failure
- **WHEN** machinectl Shell Reachability fails
- **THEN** Docker Availability, Docker Rootless Verification, and gVisor Runtime Registration are displayed as skipped with the annotation "requires: machinectl reachable"

#### Scenario: Independent chains unaffected
- **WHEN** machinectl binary check fails in Chain 1
- **THEN** Chain 2 (setfacl, ACL support) and Chain 3 (tooling plane, state dir) still execute fully

### Requirement: Distro-Aware Remediation
The system SHALL detect the host Linux distribution by parsing `/etc/os-release` and tailor fix commands to the detected package manager.

#### Scenario: Debian/Ubuntu host
- **WHEN** `/etc/os-release` contains `ID=debian` or `ID_LIKE=debian`
- **THEN** remediation commands use `sudo apt install <package>`

#### Scenario: Unknown distribution
- **WHEN** the distribution cannot be determined from `/etc/os-release`
- **THEN** remediation commands use generic package names without a package manager prefix

### Requirement: Exit Code Contract
The system SHALL exit with code 0 when all checks pass and code 1 when any check fails.

#### Scenario: All checks pass
- **WHEN** every executed check returns PASS
- **THEN** the process exits with code 0

#### Scenario: Any check fails
- **WHEN** one or more checks return FAIL (skipped checks do not count as failures)
- **THEN** the process exits with code 1

### Requirement: Rich Formatted Output
The system SHALL render results using Rich, grouped by category, with compact success lines and expanded failure blocks.

#### Scenario: Passing check display
- **WHEN** a check passes
- **THEN** it is displayed as a single line: `✓ <check name>` with optional detail

#### Scenario: Failing check display
- **WHEN** a check fails
- **THEN** it is displayed as `✗ <check name>` followed by an indented block containing: the failure description, the fix command, the reason, and a documentation reference

#### Scenario: Skipped check display
- **WHEN** a check is skipped due to a failed dependency
- **THEN** it is displayed as `⊘ <check name> — skipped (requires: <dependency name>)`

#### Scenario: Summary line
- **WHEN** all checks have been evaluated
- **THEN** a summary line is displayed: `N/M passed · X failed · Y skipped`

### Requirement: Check Subset API
The system SHALL provide a function to execute a filtered subset of doctor checks by category, enabling `init` and `start` to run only their relevant dependency chains without duplicating check logic.

#### Scenario: Init runs filesystem and repo checks
- **WHEN** `sandbox init` invokes the doctor subset with categories `["Filesystem", "Repo Integrity"]`
- **THEN** only the 4 checks in those categories are executed (setfacl, ACL support, tooling plane, state dir writable), with dependency graph and cascading skip logic preserved

#### Scenario: Start runs privilege boundary checks
- **WHEN** `sandbox start` invokes the doctor subset with category `["Privilege Boundary"]`
- **THEN** only the 8 checks in that category are executed (sudo, machinectl, user exists, systemd-machined, machinectl reachable, Docker available, Docker rootless, gVisor runsc), with dependency graph and cascading skip logic preserved

#### Scenario: Subset results match full doctor format
- **WHEN** the subset API returns results
- **THEN** the return type is `list[CheckResult]`, identical to `run_checks`, and compatible with `render_results`
