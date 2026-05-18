## Purpose

This specification defines the `sandbox doctor` diagnostic command, which validates host readiness for sandbox operation by executing a dependency-ordered check pipeline covering binary availability, user existence, service state, Docker configuration, filesystem capabilities, and tooling plane integrity.
## Requirements
### Requirement: Doctor Command Interface
The system SHALL provide a `sandbox doctor --user <name>` command that validates host readiness for sandbox operation. The `--user` parameter SHALL be mandatory with no default value. The doctor module SHALL also expose a programmatic subset API for use by other commands.

#### Scenario: Doctor invoked with user parameter
- **WHEN** the operator runs `sandbox doctor --user sandbox`
- **THEN** the system executes all 15 diagnostic checks and reports results grouped by category

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

### Requirement: runsc RuntimeArgs Validation
The system SHALL verify that the sandbox user's rootless Docker daemon has `--oci-seccomp` and `--debug-log` configured in the `runsc` runtime's `runtimeArgs`, and does NOT have `--host-uds=all`. This check SHALL use `warn` severity — it is a defense-in-depth advisory, not a hard prerequisite.

**Dependencies:** gVisor Runtime Registration (`runsc` check)

#### Scenario: Both runtimeArgs present
- **WHEN** `docker info --format '{{json .Runtimes}}'` (via machinectl) returns a `runsc` entry with `runtimeArgs` containing both `--oci-seccomp` and an arg prefixed with `--debug-log`
- **THEN** the check reports PASS

#### Scenario: Missing --oci-seccomp
- **WHEN** the `runsc` runtime's `runtimeArgs` does not contain `--oci-seccomp`
- **THEN** the check reports WARN with remediation referencing `~<user>/.config/docker/daemon.json`

#### Scenario: Missing --debug-log
- **WHEN** the `runsc` runtime's `runtimeArgs` does not contain an arg prefixed with `--debug-log`
- **THEN** the check reports WARN with remediation referencing `~<user>/.config/docker/daemon.json`

#### Scenario: Both runtimeArgs missing
- **WHEN** the `runsc` runtime's `runtimeArgs` is empty or missing both args
- **THEN** the check reports WARN listing both missing args

#### Scenario: runsc dependency failed — check skipped
- **WHEN** the `runsc` check has failed or been skipped
- **THEN** the runtimeArgs check is skipped with annotation `requires: runsc`

### Requirement: Host UDS Runtime Validation
The system SHALL verify that the `runsc` runtime does NOT have `--host-uds=all` configured. This check SHALL use `warn` severity — it is a defense-in-depth advisory confirming that the default `--host-uds=none` is in effect.

**Dependencies:** gVisor Runtime Registration (`runsc` check)

#### Scenario: --host-uds=none confirmed (default)
- **WHEN** `docker info --format '{{json .Runtimes}}'` (via machinectl) returns a `runsc` entry whose `runtimeArgs` does NOT contain `--host-uds=all`
- **THEN** the check reports PASS

#### Scenario: --host-uds=all detected
- **WHEN** the `runsc` runtime's `runtimeArgs` contains `--host-uds=all`
- **THEN** the check reports WARN with remediation: remove `--host-uds=all` from `runtimeArgs` in `~<user>/.config/docker/daemon.json` (the default `none` is correct for this architecture)

#### Scenario: runsc dependency failed — check skipped
- **WHEN** the `runsc` check has failed or been skipped
- **THEN** the `--host-uds` check is skipped with annotation `requires: runsc`

### Requirement: Image Digest Resolvability Check
The system SHALL provide a `check_image_digests` doctor check in the `"Supply Chain"` category that verifies all `IMAGE_REGISTRY` entries are resolvable against their respective container registries. The check SHALL depend on `docker_available`. The check SHALL use `docker manifest inspect` via machinectl to probe each digest.

**Dependencies:** Docker Availability (`docker_available` check)

#### Scenario: All digests resolvable
- **WHEN** `docker manifest inspect <ref>@<digest>` succeeds for every `IMAGE_REGISTRY` entry (via machinectl)
- **THEN** the check reports PASS with the count of verified images

#### Scenario: Stale digest detected
- **WHEN** `docker manifest inspect <ref>@<digest>` returns `MANIFEST_UNKNOWN` or exits non-zero for any entry
- **THEN** the check reports FAIL identifying the stale entry by key name and its unresolvable digest

#### Scenario: Tag drift detected (informational)
- **WHEN** the pinned digest for an entry differs from the current tag resolution (`docker manifest inspect <ref>:<tag>`)
- **THEN** the check reports INFO (not FAIL) noting that the upstream tag has been re-pushed and rotation may be warranted

#### Scenario: Registry unreachable
- **WHEN** the manifest inspection times out (2-second timeout) or network is unavailable
- **THEN** the check reports SKIP with `"registry unreachable"` — non-fatal, preserving offline/air-gapped use

#### Scenario: docker_available dependency failed — check skipped
- **WHEN** the `docker_available` check has failed or been skipped
- **THEN** the `check_image_digests` check is skipped with annotation `requires: docker_available`

### Requirement: Warn Severity Status
The system SHALL support a `warn` status in `CheckResult` for defense-in-depth advisories that inform without blocking. `warn` results SHALL NOT cascade skip to dependent checks. `warn` results SHALL NOT block `sandbox start` pre-flight gates. `warn` results SHALL NOT cause a non-zero exit code from `sandbox doctor`.

#### Scenario: Warn does not cascade skip
- **WHEN** a check returns `warn` and has dependent checks
- **THEN** the dependent checks still execute (they are NOT skipped)

#### Scenario: Warn does not block sandbox start
- **WHEN** `sandbox start` runs the Privilege Boundary pre-flight subset and a check returns `warn`
- **THEN** the start pipeline proceeds — the warn result does not trigger exit code 1

#### Scenario: Warn does not affect doctor exit code
- **WHEN** `sandbox doctor` completes with one or more `warn` results and zero `fail` results
- **THEN** the process exits with code 0

### Requirement: Warn Display in Rich Output
The system SHALL render `warn` results with yellow styling, showing the check name, detail, and remediation.

#### Scenario: Warn check display
- **WHEN** a check returns `warn`
- **THEN** it is displayed as `⚠ <check name>` in yellow with detail and remediation on subsequent indented lines

#### Scenario: Summary line includes warn count
- **WHEN** all checks have been evaluated and one or more returned `warn`
- **THEN** the summary line includes the warn count: `N/M passed · W warnings · X failed · Y skipped`

#### Scenario: Summary style reflects warn state
- **WHEN** all checks have been evaluated with zero failures and one or more warnings
- **THEN** the summary line is styled yellow (not green, not red)

#### Scenario: Summary style with failures takes precedence
- **WHEN** all checks have been evaluated with one or more failures (regardless of warnings)
- **THEN** the summary line is styled red (failures take precedence over warnings)

### Requirement: Filesystem ACL Support
The system SHALL verify that the filesystem under SANDBOX_AI_HOME supports POSIX ACLs.

**Dependencies:** setfacl binary (Binary Availability)

#### Scenario: ACL supported
- **WHEN** `setfacl -m u:$(whoami):r <test_file>` succeeds on a temporary file within the repository and the ACL is cleaned up
- **THEN** the check reports PASS

#### Scenario: ACL not supported
- **WHEN** the `setfacl` probe fails
- **THEN** the check reports FAIL with guidance on filesystem mount options

### Requirement: Ancestor Traverse Verification
The system SHALL provide an `ancestor_traverse` check in Chain 2 (Filesystem) that verifies the sandbox user can traverse ancestor directories from `SANDBOX_AI_HOME` to `/`. The check SHALL depend on the `acl_support` check.

#### Scenario: All ancestors traversable
- **WHEN** the sandbox user has `--x` permission (via mode bits or ACLs) on every user-owned ancestor directory from `SANDBOX_AI_HOME` up to the ownership boundary
- **THEN** the check reports PASS

#### Scenario: Ancestor lacks traverse permission
- **WHEN** a user-owned ancestor directory (e.g., `/home/user/`) has mode 0700 and no ACL entry granting `--x` to the sandbox user
- **THEN** the check reports FAIL with the specific directory, its current mode, and a fix command: `setfacl -m u:<user>:--x <dir>`

#### Scenario: Symlink detected in ancestor path
- **WHEN** a symlink is detected in the ancestor path (a component resolves to a different physical path)
- **THEN** the check reports WARN noting that the sandbox user may need manual `--x` grants on target-path intermediaries outside the ownership boundary

#### Scenario: acl_support dependency failed — check skipped
- **WHEN** the `acl_support` check has failed or been skipped
- **THEN** the `ancestor_traverse` check is skipped with annotation `requires: acl_support`

#### Scenario: Check executes without user_exists dependency
- **WHEN** the `ancestor_traverse` check is invoked and the `user_exists` check has not been explicitly evaluated in this chain
- **THEN** the check still executes successfully using the provided user parameter (no cross-chain `depends_on` required)

### Requirement: Tooling Plane Integrity
The system SHALL verify that the unconditional template and static files exist in `templates/docker/` and `templates/config/`. The unconditional file count SHALL be 17 (16 original + `templates/docker/coredns/Dockerfile.coredns`).

**Dependencies:** None (root check)

#### Scenario: All files present
- **WHEN** all 17 required files exist at their expected paths (including `templates/docker/coredns/Dockerfile.coredns`)
- **THEN** the check reports PASS with the file count

#### Scenario: Files missing
- **WHEN** one or more required files are absent
- **THEN** the check reports FAIL listing the missing files

### Requirement: State Directory Writability
The system SHALL verify that the per-user state directory `<sandbox_ai_user_home()>/state/` is writable.

**Dependencies:** None (root check)

#### Scenario: Directory writable
- **WHEN** a temporary file can be created and removed in `<home>/state/`
- **THEN** the check reports PASS

#### Scenario: Directory not writable
- **WHEN** file creation in `<home>/state/` fails
- **THEN** the check reports FAIL with guidance on directory permissions (`chmod 0700 <home>/state/`)

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
The system SHALL exit with code 0 when all checks pass (or warn) and code 1 when any check fails. Warn results SHALL NOT cause a non-zero exit code.

#### Scenario: All checks pass
- **WHEN** every executed check returns PASS or WARN
- **THEN** the process exits with code 0

#### Scenario: Any check fails
- **WHEN** one or more checks return FAIL (skipped and warned checks do not count as failures)
- **THEN** the process exits with code 1

### Requirement: Rich Formatted Output
The system SHALL render results using Rich, grouped by category, with compact success lines, expanded failure blocks, and yellow warning blocks.

#### Scenario: Passing check display
- **WHEN** a check passes
- **THEN** it is displayed as a single line: `✓ <check name>` with optional detail

#### Scenario: Failing check display
- **WHEN** a check fails
- **THEN** it is displayed as `✗ <check name>` followed by an indented block containing: the failure description, the fix command, the reason, and a documentation reference

#### Scenario: Skipped check display
- **WHEN** a check is skipped due to a failed dependency
- **THEN** it is displayed as `⊘ <check name> — skipped (requires: <dependency name>)`

#### Scenario: Warning check display
- **WHEN** a check returns warn
- **THEN** it is displayed as `⚠ <check name>` in yellow followed by an indented block containing: the warning detail and remediation

#### Scenario: Summary line
- **WHEN** all checks have been evaluated
- **THEN** a summary line is displayed: `N/M passed · W warnings · X failed · Y skipped` (segments with zero count are omitted)

### Requirement: Check Subset API
The system SHALL provide a function to execute a filtered subset of doctor checks by category, enabling `init` and `start` to run only their relevant dependency chains without duplicating check logic. The Privilege Boundary chain SHALL include `compose_project_name_collision` as a dependent check (depends on `machinectl_reachable`).

#### Scenario: Init runs filesystem and repo checks
- **WHEN** `sandbox init <inst>` invokes the doctor subset with categories `["Filesystem", "Repo Integrity"]`
- **THEN** only the checks in those categories are executed (setfacl, ACL support, tooling plane, state dir writable), with dependency graph and cascading skip logic preserved

#### Scenario: Start runs privilege boundary checks
- **WHEN** `sandbox start <inst>` invokes the doctor subset with category `["Privilege Boundary"]`
- **THEN** the chain executes the existing privilege-boundary checks plus `compose_project_name_collision` (gated on `machinectl_reachable`); dependency graph and cascading skip logic preserved

#### Scenario: Init pre-flight includes compose_project_name_collision
- **WHEN** `sandbox init <inst>` runs the pre-flight after `machinectl_reachable` passes
- **THEN** `compose_project_name_collision` runs and rejects init if a daemon-side project already exists with the prefixed name

#### Scenario: Subset results match full doctor format
- **WHEN** the subset API returns results
- **THEN** the return type is `list[CheckResult]`, identical to `run_checks`, and compatible with `render_results`

### Requirement: Per-User Tree Existence Check
The `sandbox doctor` command SHALL include a check that the per-user tree (`<home>/`, `<home>/config/`, `<home>/state/`) exists. If any directory is missing, the doctor SHALL report the omission and direct the operator to run `sandbox init`. The check SHALL NOT auto-create the tree.

#### Scenario: Tree present
- **WHEN** `sandbox doctor` runs and all three directories exist
- **THEN** the per-user-tree-existence check passes

#### Scenario: Tree absent
- **WHEN** `sandbox doctor` runs and `<home>/` does not exist
- **THEN** the check reports: "FAIL: per-user tree not initialized at `<resolved-home>`. Run `sandbox init` to create it." and the doctor exits non-zero

#### Scenario: Partial tree
- **WHEN** `sandbox doctor` runs and `<home>/` exists but `<home>/state/` is missing
- **THEN** the check reports the specific missing subdirectory and directs the operator to run `sandbox init` (which is idempotent and will create the missing piece)

### Requirement: Per-User Tree Mode Check
The `sandbox doctor` command SHALL include a check that `<home>/`, `<home>/config/`, and `<home>/state/` each have mode `0700`. If any is more permissive, the doctor SHALL emit a warning identifying the path, the actual mode, and the expected mode. The doctor SHALL NOT auto-fix the mode.

#### Scenario: All modes correct
- **WHEN** `sandbox doctor` runs and all three directories have mode `0700`
- **THEN** the per-user-tree-mode check passes silently

#### Scenario: Mode drift on state subdirectory
- **WHEN** `sandbox doctor` runs and `<home>/state/` has mode `0755`
- **THEN** the check warns: "WARNING: `<home>/state/` has mode `0755`; expected `0700`. Run `chmod 0700 <home>/state/` to remediate." The doctor continues to other checks (warning, not failure).

#### Scenario: Mode check skipped when tree absent
- **WHEN** `sandbox doctor` runs and `<home>/` does not exist
- **THEN** the per-user-tree-mode check is skipped (the existence check already reported the absence)

### Requirement: Resolved Per-User Home in Doctor Output
The `sandbox doctor` command SHALL display the resolved per-user home path in its output. This makes a misconfigured `SANDBOX_AI_USER_HOME` env var visible to the operator.

#### Scenario: Default home displayed
- **WHEN** `sandbox doctor` runs with `SANDBOX_AI_USER_HOME` unset
- **THEN** the doctor output contains a line of the form: "Per-user home: `<expanded-path>`" where `<expanded-path>` is `os.path.expanduser("~/.sandbox-ai")`

#### Scenario: Override home displayed
- **WHEN** `sandbox doctor` runs with `SANDBOX_AI_USER_HOME=/tmp/test-home` set
- **THEN** the doctor output contains: "Per-user home: `/tmp/test-home`" so the operator sees the override is active

### Requirement: Legacy CWD-Local File Detection
The `sandbox doctor` command SHALL detect legacy `<cwd>/sandbox-ai.toml` and `<cwd>/.state/` and warn the operator that these files are no longer used.

#### Scenario: Legacy host config detected
- **WHEN** `sandbox doctor` runs and `<cwd>/sandbox-ai.toml` exists
- **THEN** the doctor emits a warning: "Found legacy `<cwd>/sandbox-ai.toml`. Per-host config now lives at `<resolved-home>/config/sandbox-ai.toml`. Migrate manually or delete the legacy file."

#### Scenario: Legacy state directory detected
- **WHEN** `sandbox doctor` runs and `<cwd>/.state/` exists
- **THEN** the doctor emits a warning: "Found legacy `<cwd>/.state/`. Orchestrator state now lives at `<resolved-home>/state/`. Migrate manually or delete the legacy directory."

### Requirement: Workspace Bridge Group Existence Check

The doctor SHALL include a check `workspace_bridge_group_exists` that verifies the configured bridge group (`HostSettings.workspace_bridge_group`, default `sb-ws`) exists at a gid in the daemon user's subgid range. On failure, the check SHALL print copy-pasteable `groupadd` and `usermod` commands using `autodetect_workspace_bridge_gid_recommendation` to fill in the recommended gid (per Decision 13 / Option M).

#### Scenario: Group exists with valid gid passes
- **WHEN** the doctor runs and `getent group <bridge-group>` returns a gid in `<docker_unprivileged_user>`'s subgid range
- **THEN** the check passes

#### Scenario: Group missing prints recommendation with autodetected gid
- **WHEN** the doctor runs and the configured bridge group does not exist
- **THEN** the check fails with: `"FAIL: group '<name>' does not exist. Run: sudo groupadd -g <recommended-gid> <name> && sudo usermod -aG <name> <current-user>. Then log out and back in."` where `<name>` is the configured `workspace_bridge_group` and `<recommended-gid>` comes from `autodetect_workspace_bridge_gid_recommendation`

#### Scenario: Group exists but gid out of range
- **WHEN** the doctor runs and the configured bridge group exists at a gid OUTSIDE the daemon user's subgid range
- **THEN** the check fails with: `"FAIL: group '<name>' exists at gid <actual-gid>, which is outside <docker_unprivileged_user>'s subgid range. Either delete and re-create the group at a gid in range, or override [host].workspace_bridge_group to point at a different group."`

#### Scenario: Recommendation falls back if autodetect itself fails
- **WHEN** the doctor's failure path tries to compute a recommendation but `autodetect_workspace_bridge_gid_recommendation` raises (e.g., `NoSubgidRangeError` because the daemon user is not in `/etc/subgid`)
- **THEN** the failure message includes the autodetect error and a fallback hint to verify rootless docker setup

### Requirement: Dev Process Membership Check

The doctor SHALL include a check `dev_in_workspace_bridge_group` that verifies the doctor's *current process* has the bridge gid in its supplementary groups (`os.getgroups()`). This catches the post-`usermod`/pre-relogin pitfall where `/etc/group` shows membership but the running process's group set is stale.

#### Scenario: Process has gid in supplementary groups passes
- **WHEN** the doctor runs and `workspace_bridge_gid(host)` is in `os.getgroups()`
- **THEN** the check passes

#### Scenario: /etc/group says member but process group set is stale
- **WHEN** the doctor runs, `os.getlogin()` is in the bridge group's `gr_mem` per `grp.getgrnam`, but `workspace_bridge_gid(host)` is NOT in `os.getgroups()`
- **THEN** the check fails with: `"User is a member of '<name>' in /etc/group, but the current process's supplementary groups don't include gid <bridge-gid>. Log out and log back in to refresh group membership before running 'sandbox start'."`

#### Scenario: Not a member at all
- **WHEN** the doctor runs and the user is not in the bridge group's `gr_mem`
- **THEN** the check fails with: `"User is not a member of '<name>'. Run: sudo usermod -aG <name> <current-user>, then log out and back in."`

### Requirement: Subuid Resolver Sanity Check

The doctor SHALL include a check `subuid_resolver_works` that verifies `host_id_for_in_container(1000, host.docker_unprivileged_user)` returns a sane mapped uid (i.e., does not raise `NoSubuidRangeError` or `SubuidOutOfRangeError`).

#### Scenario: Resolver returns a uid
- **WHEN** the doctor runs and `host_id_for_in_container(1000, ...)` returns a positive integer
- **THEN** the check passes

#### Scenario: Daemon user has no subuid entry
- **WHEN** the doctor runs and `host_id_for_in_container(1000, ...)` raises `NoSubuidRangeError`
- **THEN** the check fails with the error's message and a hint pointing at `/etc/subuid` and rootless docker setup documentation

### Requirement: Helper Image Locally-Cached Check

The doctor SHALL include a warn-only check `helper_image_pulled` that runs `docker image inspect <pinned-busybox-ref>` and reports whether the image is locally cached. The check SHALL NOT trigger a pull (diagnostic commands have no side effects).

#### Scenario: Image cached passes
- **WHEN** the doctor runs and `docker image inspect <pinned-busybox-ref>` exits 0
- **THEN** the check passes

#### Scenario: Image not cached warns
- **WHEN** the doctor runs and `docker image inspect <pinned-busybox-ref>` exits non-zero
- **THEN** the check emits a warning: `"WARN: helper image <pinned-ref> is not locally cached; will be pulled on first 'sandbox start' (~1MB)."`. The doctor proceeds with subsequent checks; this is not a fatal error.

### Requirement: Secrets Hydrated Restrictively Check

The doctor SHALL include a warn-only check `secrets_hydrated_restrictively` that scans `secrets/` and `config/` files in registered instance directories for any file with `other::r--` (mode bits revealing world-readable). Such files indicate a hydration regression where Decision 6's restrictive-mode-at-write-time contract was violated.

#### Scenario: All sensitive files have restrictive mode
- **WHEN** the doctor runs and every file under `secrets/` and the consumer-uid-0-chown ro-files set has mode `0600` or `0640` (no `other::r--`)
- **THEN** the check passes

#### Scenario: Stray world-readable secret detected
- **WHEN** the doctor finds any secret file with `other::r--` set
- **THEN** the check emits a warning identifying the file and recommending `sandbox stop && sandbox start` (which re-hydrates and re-applies the helper-cp+chown phase)

### Requirement: Pre-Existing Instance Layout Check

The doctor SHALL include a warn-only check `pre_existing_instance_layout` that detects instance directories whose cache/log leaves are in a state inconsistent with the post-Change-D scaffold-vs-helper boundary (per `orchestrator-volumes`'s "Scaffold-vs-Helper Boundary" requirement). The check SHALL distinguish three states for each leaf in the "Cache/Log Leaf Inventory":

- **Leaf absent** — pass. The expected state for an instance that has been init'd but never started (the helper recipe has not run yet). No misconfiguration to report.
- **Leaf present and consumer-subuid-owned** — pass. The helper recipe ran successfully on a prior start; on-disk ownership matches the contract.
- **Leaf present and dev-owned** — warn. Either a legacy instance from before the scaffold-vs-helper boundary was enforced (pre-Change-D scaffold pre-created the leaf as `dev:dev`), or a misconfiguration where the helper recipe failed silently on a prior start. The check SHALL recommend `rm -rf <home>/instances/<inst>/<leaf-path>` for each affected leaf; running the remediation lets the next `sandbox start` succeed because the helper recipe creates the leaf fresh as claude-sandbox-owned.

The check uses `core.doctor.checks.workspace_bridge._scan_instance_dirs` to iterate registered instances (per the "Doctor Instance Scan Uses Registry" requirement). (This is the post-refactor module path; the function's behavior, signature, and return value are unchanged from its prior package-level location — only the import path moved.)

The per-leaf ownership lookup used by the check SHALL be expressed as an injectable callable (`uid_for_path: Callable[[str], int]`) with a default that wraps `os.stat(path).st_uid`. Tests MAY override the resolver to make per-path ownership deterministic without monkeypatching `os.stat`. Production behavior MUST remain identical to direct `os.stat` use, including raising `OSError` for absent leaves so the absent-leaf branch is reached as before.

#### Scenario: Just-init'd instance passes (leaf absent)
- **WHEN** the doctor inspects a freshly-init'd instance whose cache/log leaves do not yet exist on disk
- **THEN** the check passes for that instance with no warnings (the absent state is the expected scaffold-vs-helper boundary outcome)

#### Scenario: Started instance with consumer-owned leaves passes
- **WHEN** the doctor inspects an instance whose cache/log leaves are present and consumer-subuid-owned
- **THEN** the check passes for that instance

#### Scenario: Legacy / misconfigured instance with dev-owned leaves warns
- **WHEN** the doctor inspects an instance whose `cache/core/.claude` (or any cache/log leaf per `orchestrator-volumes`'s "Cache/Log Leaf Inventory") exists on disk and is owned by `dev` rather than the consumer subuid
- **THEN** the check emits a warning identifying the instance and the affected leaf path(s), and recommending `rm -rf <home>/instances/<instance-name>/<leaf-path>` for each affected leaf. After remediation, the next `sandbox start` runs the helper recipe, which creates the leaf as claude-sandbox-owned and chowns to consumer subuid.

#### Scenario: Mixed-state instance reports per-leaf
- **WHEN** the doctor inspects an instance where one cache/log leaf is consumer-owned (helper ran) but another is dev-owned (e.g., partial helper failure)
- **THEN** the check passes the consumer-owned leaf silently and warns on the dev-owned leaf with the per-leaf `rm -rf` remediation; the warning enumerates each affected leaf separately for operator clarity

#### Scenario: Test override of ownership resolver yields deterministic mixed-state reporting
- **WHEN** a test calls the check with `uid_for_path=` set to a callable that returns a uid mapping per path (e.g., consumer-subuid for one leaf, a different uid for another)
- **THEN** the check uses the supplied resolver in place of `os.stat` for every per-leaf ownership comparison, the absent-leaf branch is still triggered when the resolver raises `OSError`, and the resulting `CheckResult` enumerates only the leaves whose resolver-returned uid does not match the consumer subuid

### Requirement: Doctor Instance Scan Uses Registry

The doctor's per-instance scanning helper (`core.doctor.checks.workspace_bridge._scan_instance_dirs`) SHALL iterate registered instances from `<sandbox_ai_home()>/state/instances.json` rather than walking `__file__` parents to discover a `sandboxes/` tree. This implementation change is install-mode-independent: doctor checks that depend on per-instance scanning (notably `secrets_hydrated_restrictively` and `pre_existing_instance_layout`) work correctly in both dev checkouts and wheel installs.

(This is the post-refactor module path; the function's behavior, signature, and return value are unchanged from its prior package-level location — only the import path moved.)

This requirement closes change-4's deferred behavior where these checks SKIPped in wheel installs because `__file__` resolved into `site-packages/`.

#### Scenario: Scan iterates registry
- **WHEN** any doctor check that uses `_scan_instance_dirs` runs
- **THEN** the helper reads `<sandbox_ai_home()>/state/instances.json` and yields each registered instance's `instance_dir`; `__file__`-derived discovery is NOT used

#### Scenario: Scan works in wheel install
- **WHEN** `sandbox doctor` runs after a wheel install (`uv tool install sandbox-ai`)
- **THEN** `secrets_hydrated_restrictively` and `pre_existing_instance_layout` execute against registered instances; they do NOT SKIP with a wheel-install diagnostic

#### Scenario: Unregistered partial-init dirs not scanned
- **WHEN** stray instance dirs exist on disk but are absent from `instances.json` (e.g., partial-init artifacts)
- **THEN** the registry-driven scan does NOT include them; `legacy_sandboxes_dir_detected` and `legacy_registry_shape` cover the stray-state cases via their own logic

### Requirement: Backups Disk-Pressure Check

The doctor SHALL include a warn-only check `backups_disk_pressure` that reports if `~/.sandbox-ai/workspaces/_backups/` total size exceeds 5 GB OR if the total entry count (number of `<ts>/` dirs across all `<inst>/<ws>/`) exceeds 50. The remediation message SHALL recommend manual `rm -rf` of stale backups.

#### Scenario: Disk pressure warning at threshold
- **WHEN** `sandbox doctor` runs and `_backups/` total size exceeds 5 GB
- **THEN** `backups_disk_pressure` emits a warning identifying the size and recommending cleanup

#### Scenario: Entry count warning at threshold
- **WHEN** `sandbox doctor` runs and `_backups/` contains more than 50 `<ts>/` entries
- **THEN** `backups_disk_pressure` emits a warning identifying the count

#### Scenario: Below threshold passes
- **WHEN** `sandbox doctor` runs and `_backups/` is below both thresholds (or absent)
- **THEN** `backups_disk_pressure` passes silently

### Requirement: Backups Partial-Dirs Check

The doctor SHALL include a warn-only check `backups_partial_dirs_present` that reports if any `*.partial/` directory in `_backups/` is older than 1 hour. Remediation: manual cleanup or auto-cleanup with operator approval (post-MVP).

#### Scenario: Stale partial warning
- **WHEN** `sandbox doctor` runs and a `*.partial/` directory exists in `_backups/` older than 1 hour
- **THEN** `backups_partial_dirs_present` emits a warning identifying the stale partial path

### Requirement: Dev Umask Workspace-Friendly Check

The doctor SHALL include a warn-only check `dev_umask_workspace_friendly` that warns when (a) at least one workspace is registered AND (b) the dev process's umask is `0o022` or worse. Recommendation: `umask 002` in shell rc files so dev-edited files in workspaces land mode `0664` group sb-ws (allowing the agent to read).

#### Scenario: Umask 022 with workspaces warns
- **WHEN** `sandbox doctor` runs and at least one workspace is registered AND `os.umask(0); os.umask(saved)` returns `0o022`
- **THEN** `dev_umask_workspace_friendly` emits a warning recommending `umask 002`

#### Scenario: Umask 002 passes
- **WHEN** `sandbox doctor` runs and the dev umask is `0o002` or stricter
- **THEN** `dev_umask_workspace_friendly` passes silently

#### Scenario: No workspaces yet skips check
- **WHEN** `sandbox doctor` runs and no instances/workspaces are registered
- **THEN** `dev_umask_workspace_friendly` is skipped (no false-positive warning before the first init)

### Requirement: Compose Project Name Collision Check

The doctor SHALL include a check `compose_project_name_collision` (severity `fail`) that detects whether a daemon-side compose project with the prefixed name (`<sanitized-dev-username>-<inst>`, per `instance-registry`) already exists for any not-yet-registered instance the operator is about to create. The check is also run by `sandbox init` as a pre-flight gate.

The check depends on `machinectl_reachable` (Privilege Boundary chain) — it runs `docker compose ls` via the daemon to enumerate existing projects.

#### Scenario: Collision at init time rejected
- **WHEN** `sandbox init <inst>` is invoked and the daemon already has a compose project with the prefixed name (e.g., another dev with the same username already created an instance with that name)
- **THEN** init fails the pre-flight gate with an explicit collision error

#### Scenario: No collision passes
- **WHEN** `sandbox doctor` runs (or `sandbox init` pre-flight) and no daemon-side compose project matches the prefixed name
- **THEN** the check passes

#### Scenario: machinectl_reachable failure cascades
- **WHEN** `machinectl_reachable` has failed
- **THEN** `compose_project_name_collision` is skipped with annotation `requires: machinectl_reachable`

### Requirement: Workspace Path in Walker Boundary Check

The doctor SHALL include a check `workspace_path_in_walker_boundary` (severity `fail`) that scans every registered workspace's `path` and flags any that match the walker boundary list (per `instance-workspace-model`'s walker safety rules). Remediation: `sandbox workspace remove --purge` and re-add at a safe path.

#### Scenario: Workspace at boundary path fails check
- **WHEN** `sandbox doctor` finds a registered `workspace.path` that matches `/`, `/etc`, `/home`, the user's home dir, or any other boundary entry
- **THEN** the check fails identifying the workspace and recommending remediation

#### Scenario: All workspaces at safe paths passes
- **WHEN** every registered workspace's `path` is below `~/.sandbox-ai/workspaces/<inst>/`
- **THEN** the check passes

### Requirement: Workspace Home Single-Filesystem Check

The doctor SHALL include a warn-only check `workspace_home_single_filesystem` that verifies `~/.sandbox-ai/` and `~/.sandbox-ai/workspaces/` share a filesystem (`statvfs(...).f_fsid` or `os.stat(...).st_dev` comparison). Cross-fs splits would cause `os.rename` (used by `workspace rename` and backup atomic-rename) to raise `EXDEV`.

#### Scenario: Single-fs passes
- **WHEN** `sandbox doctor` runs and both paths share `st_dev`
- **THEN** the check passes silently

#### Scenario: Cross-fs warns
- **WHEN** `sandbox doctor` runs and `st_dev` differs (e.g., `~/.sandbox-ai/workspaces/` is a separately-mounted filesystem)
- **THEN** the check warns that workspace rename and atomic backup rename will fail with EXDEV; remediation suggests consolidating onto one filesystem

### Requirement: Legacy Sandboxes Dir Detection Check

The doctor SHALL include a warn-only check `legacy_sandboxes_dir_detected` that flags `<cwd>/sandboxes/` directories. These indicate pre-change-5 layouts that are no longer used. Recommendation: manual cleanup after confirming no useful state remains.

#### Scenario: Legacy sandboxes dir warns
- **WHEN** `sandbox doctor` runs and `<cwd>/sandboxes/` exists
- **THEN** the check emits a warning identifying the path and recommending manual cleanup

### Requirement: Legacy user_project_root Check

The doctor SHALL include a warn-only check `legacy_workspace_in_user_project_root` that scans every registered instance's `sandbox.toml` for a `[instance].user_project_root` field. Presence indicates a pre-change-5 instance that needs `destroy + init` migration.

#### Scenario: Legacy field detected
- **WHEN** `sandbox doctor` runs and a registered instance's `sandbox.toml` contains `instance.user_project_root`
- **THEN** the check warns identifying the instance and recommending `sandbox destroy <inst> && sandbox init <inst> --copy <ws>=<former-user-project-root>`

### Requirement: Legacy Registry Shape Check

The doctor SHALL include a warn-only check `legacy_registry_shape` that detects path-keyed `instances.json` (pre-change-5 shape: `{abs(cwd): {...}}`). Recommendation: `rm ~/.sandbox-ai/state/instances.json && sandbox init <each-inst>`.

#### Scenario: Path-keyed registry warns
- **WHEN** `sandbox doctor` runs and `instances.json` contains keys that look like absolute paths (start with `/`)
- **THEN** the check warns identifying the legacy shape and recommending the manual recovery

#### Scenario: Name-keyed registry passes
- **WHEN** `sandbox doctor` runs and `instances.json` keys are valid instance names
- **THEN** the check passes

### Requirement: Doctor Cross-Boundary Invocation Routing

All `sandbox doctor` checks that cross the `dev → <sandbox-user>` privilege boundary SHALL route their invocations through `core.dispatch.invoke(op, args, host_config)` using the typed op surface defined by `runtime-dispatcher`. The checks SHALL NOT call `core.host_config.machinectl_cmd(...)` directly. This routing applies to:

- **machinectl Shell Reachability**: routes through `auth-probe` (no args). The 10-second timeout is enforced by the doctor check's invocation context, not by the dispatcher.
- **Docker Availability**: routes through `docker-version` (no args). (Source `privilege_boundary.py:155` runs `docker version --format '{{.Server.Version}}'` — the `docker version` subcommand, NOT `docker info`; there is no `default` `docker-info` preset.)
- **Docker Rootless Verification**: routes through `docker-info` with the `security-options` preset (`docker info --format '{{.SecurityOptions}}'`, source `:186`).
- **gVisor Runtime Registration**: routes through `docker-info` with the `runtimes` preset (`docker info --format '{{json .Runtimes}}'`, source `:219`).
- **runsc RuntimeArgs Validation**: routes through `docker-info` with the `runtimes` preset (source `:260`, same invocation shape).
- **Host UDS Runtime Validation**: routes through `docker-info` with the `runtimes` preset (source `:323`, same invocation shape).
- **Image Digest Resolvability Check**: routes through `docker-manifest-inspect`, invoked once per `IMAGE_REGISTRY` pin with that pin's `<name>@sha256:<64hex>` ref as the single arg. (Source `supply_chain.py:27` runs `docker manifest inspect <pinned-ref>` in a loop over `IMAGE_REGISTRY`.)
- **Compose status / compose-related doctor checks** (e.g., compose-ls within Privilege Boundary chain): route through `compose-ls`.

The semantic content of each affected doctor check (what it verifies, what PASS/WARN/FAIL means, the 10-second timeout for the auth probe, the format-string expectations, the cascading-skip dependencies) is preserved unchanged. Only the underlying boundary-crossing mechanism shifts from inline machinectl-cmd-built argv to dispatcher-routed typed ops.

#### Scenario: machinectl Shell Reachability uses the auth-probe op
- **WHEN** the `machinectl Shell Reachability` doctor check executes its probe
- **THEN** the check invokes `core.dispatch.invoke("auth-probe", [], host_config)` (with a 10-second timeout from the check's invocation context); the dispatcher's target argv is `["/bin/bash", "-c", "echo ok"]` per `runtime-dispatcher`'s op contract; the resulting cross-boundary argv is `[*machinectl_cmd(<user>, <auth>), "/bin/bash", "-c", "/usr/local/libexec/sandbox-ai/dispatch auth-probe"]`

#### Scenario: gVisor Runtime Registration uses docker-info runtimes preset
- **WHEN** the `gVisor Runtime Registration` doctor check executes its probe
- **THEN** the check invokes `core.dispatch.invoke("docker-info", ["runtimes"], host_config)`; the dispatcher's target argv is `["/bin/bash", "-c", "docker info --format '{{json .Runtimes}}'"]`; the check parses the stdout JSON to extract `.runsc` (or `.sandbox-ai-runsc` post-sandbox-setup) and reports PASS/FAIL/WARN per the existing semantic contract

#### Scenario: Doctor checks do not call machinectl_cmd directly
- **WHEN** the convention meta-test in `tests/unit/test_conventions.py` runs against `src/core/doctor/checks/`
- **THEN** no module under `src/core/doctor/checks/` contains `machinectl_cmd` references; the meta-test passes (doctor's check modules are NOT in any allowlist category — the allowed direct callers are only `src/core/host_config.py`, `src/core/dispatch.py`, and the `src/core/setup/*.py` setup-phase package)

#### Scenario: Doctor check semantics preserved across the refactor
- **WHEN** the refactored doctor runs against an unchanged host
- **THEN** every check's PASS/WARN/FAIL verdict matches its pre-refactor behavior; the rendered output (column names, severity rendering, dependency cascading) is byte-identical to the pre-refactor doctor output (verified by a golden-file test or equivalent)

