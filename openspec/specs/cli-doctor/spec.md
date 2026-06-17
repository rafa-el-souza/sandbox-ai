## Purpose

This specification defines the `sandbox doctor` diagnostic command, which validates host readiness for sandbox operation by executing a dependency-ordered check pipeline covering binary availability, user existence, service state, Docker configuration, filesystem capabilities, and tooling plane integrity.
## Requirements
### Requirement: Doctor Command Interface
The system SHALL provide a `sandbox doctor [--user <name>]` command that validates host readiness for sandbox operation. The execution mode is resolved first (the marker; see "Execution-Mode-Aware Doctor Checks"), then the user to validate: an explicit `--user` always wins; otherwise in **`separate-user`** mode `--user` is **mandatory** (else the dedicated `docker_unprivileged_user` from the toml, else an error), while in **`operator-rootless`** mode `--user` is **optional** — when omitted, doctor resolves the daemon owner as the invoking operator (`resolve_daemon_owner` = the current user) **toml-free**, reading neither the toml nor `docker_unprivileged_user`. The doctor module SHALL also expose a programmatic subset API for use by other commands.

#### Scenario: Doctor invoked with user parameter
- **WHEN** the operator runs `sandbox doctor --user sandbox`
- **THEN** the system executes the mode-applicable diagnostic checks and reports results grouped by category

#### Scenario: separate-user doctor without `--user` errors
- **WHEN** the operator runs `sandbox doctor` without `--user` in `separate-user` mode
- **THEN** the CLI exits with an error indicating that `--user` is required

#### Scenario: operator-rootless doctor without `--user` resolves the operator toml-free
- **WHEN** the operator runs `sandbox doctor` without `--user` on an `operator-rootless` host (no toml present)
- **THEN** doctor resolves the daemon owner as the invoking operator and runs the mode-applicable checks — it does NOT error, and does NOT read the toml / `docker_unprivileged_user`

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
The system SHALL verify that the operator can cross the privilege boundary into the unprivileged user by running the `auth-probe` dispatcher op via `core.dispatch.probe("auth-probe", …)` — the same seam `sandbox start`'s preflight reuses. The crossing primitive is selected per the "Doctor Cross-Boundary Invocation Routing" requirement (separate-user → `sudo_pipe_cmd`, operator-rootless → local), NOT a hardcoded `sudo machinectl shell`. The probe SHALL use a 10-second timeout to detect a sudoers misconfiguration (password prompt hang). The check's registry **id** remains `machinectl_reachable` (a stable key with `depends_on` edges); its operator-facing **display name** is `boundary reachable` — it crosses the boundary via the dispatcher byte-pipe in separate-user or runs locally in operator-rootless, never a `machinectl` shell, so the display name and the function docstring SHALL NOT describe it as "machinectl".

**Dependencies:** sudo binary; the `systemd-run` byte-pipe crossing launcher; Unprivileged User Existence; systemd-machined Service

#### Scenario: Crossing reachable
- **WHEN** `core.dispatch.probe("auth-probe", [], …)` completes successfully (`ok=True`) within 10 seconds — the cross-boundary argv being `[*sudo_pipe_cmd(<user>), "/bin/bash", "-c", "<dispatch> auth-probe"]`
- **THEN** the check (display name `boundary reachable`, id `machinectl_reachable`) reports PASS

#### Scenario: Crossing unreachable due to timeout
- **WHEN** the `auth-probe` crossing does not complete within 10 seconds (`timed_out=True`)
- **THEN** the check reports FAIL with guidance that the timeout likely indicates a sudoers password prompt, and provides remediation for passwordless boundary access

#### Scenario: Crossing unreachable due to error
- **WHEN** the `auth-probe` crossing fails with a non-zero recovered exit (`ok=False`)
- **THEN** the check reports FAIL with the stderr output and guidance on common causes (user not found by machined, service not running, rule not granting the op)

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

The system SHALL verify that the gVisor runtime is registered in Docker **under the reserved runtime key `sandbox-ai-runsc`** — the name `sandbox setup`'s L6 phase registers in the sandbox user's `daemon.json` (`core.setup.l6_daemon_json._RESERVED_RUNTIME_KEY`), NOT a runtime literally named `runsc`. The check (and the dependent `runsc runtimeArgs` / `--host-uds=none` checks) SHALL look up this reserved key, single-sourced from the registering phase rather than a hardcoded literal (F-024: the prior code keyed on `"runsc"`, producing a permanent false-negative — `runsc runtime not registered` — on every host even when the runtime was correctly registered, because `docker info` lists it as `sandbox-ai-runsc`).

**Dependencies:** Docker Availability

#### Scenario: runsc registered
- **WHEN** `docker info` (via the `docker-info` dispatch op) lists `sandbox-ai-runsc` in its available runtimes
- **THEN** the check reports PASS

#### Scenario: runsc not registered
- **WHEN** `sandbox-ai-runsc` is absent from the Docker runtime list (a bare runtime named `runsc` does NOT satisfy the check — the reserved key is required)
- **THEN** the check reports FAIL with a link to gVisor installation documentation

### Requirement: runsc RuntimeArgs Validation

The system SHALL verify that the reserved `sandbox-ai-runsc` runtime's `runtimeArgs` contain **exactly the args setup's L6 phase configures, single-sourced from `core.setup.l6_daemon_json.EXPECTED_RUNTIME["runtimeArgs"]`** — NOT a hardcoded list. This makes the doctor expectation and the setup configuration one source so they cannot drift: whatever L6 configures is exactly what doctor expects (the F-024 pattern, extended from the reserved-key to the runtimeArgs). The check SHALL use `warn` severity — it is a defense-in-depth advisory.

The current `EXPECTED_RUNTIME["runtimeArgs"]` is illustratively `["--oci-seccomp", "--ignore-cgroups"]` — but the single source of truth is the code constant, not this list, so this spec does not need editing when the arg set changes. `--ignore-cgroups` is present because runsc cannot create its per-container systemd cgroup scope under a rootless daemon (it reaches the system D-Bus and is auth-denied), so it is told to skip cgroup setup; its consequence — gVisor OCI cpu/memory limits are render-time-only (not runtime-enforced) — is a documented, upstream-gated limitation (see `SECURITY.md`), surfaced operationally by the separate instance memory over-commit advisory. `--debug-log` is NOT in the default expectation — gVisor syscall-level debug logging is a deferred **opt-in** (poor always-on default: perf overhead + unbounded disk), so doctor MUST NOT WARN about its absence by default. If a future opt-in adds `--debug-log=<path>` to `EXPECTED_RUNTIME`, this check follows automatically with no further change. (The `--host-uds=all` prohibition is a separate check, unchanged.)

A value-bearing expected arg (e.g. a future `--debug-log=<path>`) is matched on its flag token, so any configured value satisfies it; a flag-only arg (`--oci-seccomp`, `--ignore-cgroups`) matches exactly.

**Dependencies:** gVisor Runtime Registration (`runsc` check)

#### Scenario: All expected args present
- **WHEN** the `sandbox-ai-runsc` runtime's `runtimeArgs` contains every arg in `EXPECTED_RUNTIME["runtimeArgs"]` (extra args, e.g. an operator-added `--debug-log`, are permitted)
- **THEN** the check reports PASS listing the configured expected args

#### Scenario: default runtimeArgs pass (no false --debug-log WARN)
- **WHEN** the `runtimeArgs` is exactly the default `EXPECTED_RUNTIME["runtimeArgs"]` (illustratively `["--oci-seccomp", "--ignore-cgroups"]`)
- **THEN** the check reports PASS — `--debug-log` is a deferred opt-in, NOT expected by default

#### Scenario: A required arg is missing
- **WHEN** the `runtimeArgs` does not contain an arg in `EXPECTED_RUNTIME["runtimeArgs"]` (e.g. `--oci-seccomp` or `--ignore-cgroups` absent)
- **THEN** the check reports WARN naming the missing arg(s) with remediation referencing `~<user>/.config/docker/daemon.json`

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

The ancestor-traverse ACL on the operator's home (`u:<sandbox-user>:--x`) is granted at the **first `sandbox start`** (lifecycle: granted-once/persistent), NOT by `sudo sandbox setup` or `sandbox init`. So a non-traversable ancestor on a freshly-set-up host with no sandbox started yet is **expected, not a defect**. When a blocked ancestor is found the check distinguishes by whether any sandbox is currently running (queried via the `compose-ls` dispatch op): **no sandbox running → SKIP** (the grant is a not-yet-applicable first-`start` artifact); **a sandbox running but the ancestor still blocked → FAIL** (that sandbox cannot reach its workspace). The running-state query is **fail-safe**: if the daemon cannot be queried (docker down / probe failure / unparseable output), the check reports the real traverse gap (FAIL) rather than hiding it behind a SKIP. (This corrects a prior permanent FAIL on every setup-then-`init`-but-not-yet-`start` host.)

#### Scenario: All ancestors traversable
- **WHEN** the sandbox user has `--x` permission (via mode bits or ACLs) on every user-owned ancestor directory from `SANDBOX_AI_HOME` up to the ownership boundary
- **THEN** the check reports PASS

#### Scenario: Ancestor blocked, no sandbox running → SKIP
- **WHEN** a user-owned ancestor (e.g. `/home/<operator>/`) lacks `--x` for the sandbox user AND `compose-ls` reports zero running sandbox projects
- **THEN** the check reports SKIP with detail that the ancestor-traverse ACL is granted at the first `sandbox start` (and a manual `setfacl -m u:<user>:--x <dir>` remediation), NOT FAIL

#### Scenario: Ancestor blocked while a sandbox is running → FAIL
- **WHEN** a user-owned ancestor lacks `--x` for the sandbox user AND at least one sandbox is running (or the running-state cannot be determined — fail-safe)
- **THEN** the check reports FAIL with the specific directory, its current mode, and a fix command: `setfacl -m u:<user>:--x <dir>`

#### Scenario: Symlink detected in ancestor path
- **WHEN** a symlink is detected in the ancestor path (a component resolves to a different physical path)
- **THEN** the check reports WARN noting that the sandbox user may need manual `--x` grants on target-path intermediaries outside the ownership boundary

#### Scenario: acl_support dependency failed — check skipped
- **WHEN** the `acl_support` check has failed or been skipped
- **THEN** the `ancestor_traverse` check is skipped with annotation `requires: acl_support`

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
The system SHALL skip checks whose dependencies have failed, displaying skipped checks explicitly in the output. The skip annotation SHALL name the failed dependency by its registry **id** (`f"skipped (requires: {dep_id})"`), not its display name.

#### Scenario: Dependent check skipped after failure
- **WHEN** the boundary-reachability check (id `machinectl_reachable`) fails
- **THEN** Docker Availability, Docker Rootless Verification, and gVisor Runtime Registration are displayed as skipped with the annotation "requires: machinectl_reachable"

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
- **THEN** the doctor emits a warning: "Found legacy `<cwd>/sandbox-ai.toml`. Per-host config is now setup-determined — run `sudo sandbox setup`. Delete the legacy file."

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
- **THEN** the check fails with: `"FAIL: group '<name>' exists at gid <actual-gid>, which is outside <docker_unprivileged_user>'s subgid range. Delete and re-create the group at a gid in range (the bridge group is setup-derived — re-run \`sudo sandbox setup\`)."`

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

The check uses `core.doctor.checks.workspace_bridge.scan_instance_dirs` to iterate registered instances (per the "Doctor Instance Scan Uses Registry" requirement). (This is the post-refactor module path; the function's behavior, signature, and return value are unchanged from its prior package-level location — only the import path moved.)

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

The doctor's per-instance scanning helper (`core.doctor.checks.workspace_bridge.scan_instance_dirs`) SHALL iterate registered instances from `<sandbox_ai_home()>/state/instances.json` rather than walking `__file__` parents to discover a `sandboxes/` tree. This implementation change is install-mode-independent: doctor checks that depend on per-instance scanning (notably `secrets_hydrated_restrictively` and `pre_existing_instance_layout`) work correctly in both dev checkouts and wheel installs.

(This is the post-refactor module path; the function's behavior, signature, and return value are unchanged from its prior package-level location — only the import path moved.)

This requirement closes change-4's deferred behavior where these checks SKIPped in wheel installs because `__file__` resolved into `site-packages/`.

#### Scenario: Scan iterates registry
- **WHEN** any doctor check that uses `scan_instance_dirs` runs
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

The doctor SHALL include a warn-only check `dev_umask_workspace_friendly` (the check ID is unchanged; its operator-facing **display name is `operator umask workspace-friendly`** — `operator` is the preferred term, replacing the legacy `dev` display string) that warns when (a) at least one workspace is registered AND (b) the operator process's umask masks group-write (`0o022` or worse). Recommendation: **`umask 007`** in shell rc files so operator-edited files in workspaces land mode **`0660`** — group `sb-ws` read/write, NO access for others (least privilege). The prior `umask 002` / `0664` recommendation granted world-read for no benefit (the workspace tree is already `chmod 2770`, so others cannot traverse to the files regardless).

#### Scenario: Umask 022 with workspaces warns
- **WHEN** `sandbox doctor` runs and at least one workspace is registered AND `os.umask(0); os.umask(saved)` returns `0o022`
- **THEN** the check emits a warning recommending `umask 007` (mode `0660`)

#### Scenario: Group-write-preserving umask passes
- **WHEN** `sandbox doctor` runs and the operator umask allows group-write while blocking others-write (e.g. `0o002` or `0o007`)
- **THEN** the check passes silently

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
- **Image Digest Resolvability Check**: routes through `docker-manifest-inspect`, invoked **twice per `IMAGE_REGISTRY` entry** — once with the entry's `.pinned` digest ref `<name>@sha256:<64hex>` (stale-digest detection) and once with its `.tagged` tag ref `<name>:<tag>` (best-effort tag-drift detection); each call passes exactly one ref, drawn from the op's Q7 membership domain `{pin.pinned} ∪ {pin.tagged}`. (Source `supply_chain.py:check_image_digests` loops `IMAGE_REGISTRY` and runs `docker manifest inspect` on BOTH `pin.pinned` AND `pin.tagged`; the dual-ref domain is the runtime-dispatcher spec's Q7 resolution.)
- **Compose status / compose-related doctor checks** (e.g., compose-ls within Privilege Boundary chain): route through `compose-ls`.

The semantic content of each affected doctor check (what it verifies, what PASS/WARN/FAIL means, the 10-second timeout for the auth probe, the format-string expectations, the cascading-skip dependencies) is preserved unchanged. Only the underlying boundary-crossing mechanism shifts from inline machinectl-cmd-built argv to dispatcher-routed typed ops. The cross-boundary argv each routed invocation ultimately emits depends on the execution mode of the host (per `build_invocation`): under separate-user it is `[*sudo_pipe_cmd(<user>), …]`, and under operator-rootless it is the bare local target argv with no crossing prefix.

#### Scenario: machinectl Shell Reachability uses the auth-probe op
- **WHEN** the `machinectl Shell Reachability` doctor check executes its probe on a separate-user host
- **THEN** the check invokes `core.dispatch.invoke("auth-probe", [], host_config)` (with a 10-second timeout from the check's invocation context); the dispatcher's target argv is `["/bin/bash", "-c", "echo ok"]` per `runtime-dispatcher`'s op contract; the resulting cross-boundary argv is `[*sudo_pipe_cmd(<user>), "/bin/bash", "-c", "/usr/local/libexec/sandbox-ai/dispatch auth-probe"]`

#### Scenario: gVisor Runtime Registration uses docker-info runtimes preset
- **WHEN** the `gVisor Runtime Registration` doctor check executes its probe
- **THEN** the check invokes `core.dispatch.invoke("docker-info", ["runtimes"], host_config)`; the dispatcher's target argv is `["/bin/bash", "-c", "docker info --format '{{json .Runtimes}}'"]`; the check parses the stdout JSON to extract `.runsc` (or `.sandbox-ai-runsc` post-sandbox-setup) and reports PASS/FAIL/WARN per the existing semantic contract

#### Scenario: Doctor checks do not call machinectl_cmd directly
- **WHEN** the convention meta-test in `tests/unit/test_conventions.py` runs against `src/core/doctor/checks/`
- **THEN** no module under `src/core/doctor/checks/` contains `machinectl_cmd` references; the meta-test passes (doctor's check modules are NOT in any allowlist category — the allowed direct callers are only `src/core/host_config.py`, `src/core/dispatch.py`, and the `src/core/setup/*.py` setup-phase package)

#### Scenario: Doctor check semantics preserved across the refactor
- **WHEN** the refactored doctor runs against an unchanged host
- **THEN** every check's PASS/WARN/FAIL verdict matches its pre-refactor behavior; the rendered output (column names, severity rendering, dependency cascading) is byte-identical to the pre-refactor doctor output (verified by a golden-file test or equivalent)

### Requirement: runsc Pinned Match Check

The doctor SHALL include a check `runsc_pinned_match` that verifies the sha512 of `/usr/local/libexec/sandbox-ai/runsc` matches `BINARY_REGISTRY["runsc"].sha512`. The check SHALL invoke `core.binary_install.verify_only("runsc", host_config)` (read-only; no network calls). On match → PASS. On absence (file not present) → SKIP with remediation `run 'sudo sandbox setup' to install runsc`. On drift (sha differs from pinned) → WARN with both shas and remediation `run 'sudo sandbox setup --update-runsc' to apply the pinned version`.

**Dependencies:** none beyond filesystem readability (the check reads a root-owned file; doctor MUST be invoked with sufficient permissions to read `/usr/local/libexec/sandbox-ai/runsc`).

#### Scenario: runsc matches pinned sha
- **WHEN** `/usr/local/libexec/sandbox-ai/runsc` exists and its sha512 matches `BINARY_REGISTRY["runsc"].sha512`
- **THEN** the check reports PASS with the installed sha (truncated)

#### Scenario: runsc absent
- **WHEN** `/usr/local/libexec/sandbox-ai/runsc` does not exist
- **THEN** the check reports SKIP with detail `runsc not installed; run 'sudo sandbox setup' to install`

#### Scenario: runsc drift detected
- **WHEN** `/usr/local/libexec/sandbox-ai/runsc` exists and its sha512 differs from `BINARY_REGISTRY["runsc"].sha512`
- **THEN** the check reports WARN with detail `runsc drift: installed sha <X>, pinned sha <Y>. Run 'sudo sandbox setup --update-runsc' to apply.`

### Requirement: Dispatcher Sha Drift Check

The doctor SHALL include a check `dispatcher_sha_drift` that compares the on-disk dispatcher binary against the manifest written by setup's L6.5 phase. The manifest at `/usr/local/libexec/sandbox-ai/dispatcher.manifest.json` (the host-plane path alongside the binary — see the `sandbox-setup` capability's "Dispatcher Manifest Schema" requirement) is a JSON document with `compiled_sha512`, `source_bundle_sha512`, and `compile_timestamp` fields. The check imports the manifest path from setup's L6.5 phase (single source) so the two cannot disagree on its location.

The check SHALL verify:

1. **Binary integrity**: sha512 of `/usr/local/libexec/sandbox-ai/dispatch` matches the manifest's `compiled_sha512`.
2. **Source freshness**: the current source bundle's sha512 (computed by hashing `src/templates/dispatch/{main.go, go.mod, go.sum, vendor/**}` per the schema requirement) matches the manifest's `source_bundle_sha512`.

Verdicts:
- Both match → PASS with detail showing the truncated shas + compile timestamp.
- Binary absent OR manifest absent → SKIP with remediation `run 'sudo sandbox setup' to install the dispatcher`.
- Binary sha differs from manifest's `compiled_sha512` → WARN with detail `dispatcher binary differs from setup's recorded sha. Re-run 'sudo sandbox setup' to refresh, or investigate tampering.` (Tamper or hand-replacement scenario.)
- Source bundle sha differs from manifest's `source_bundle_sha512` → WARN with detail `dispatcher binary was compiled from an older source bundle (wheel upgrade since last setup). Re-run 'sudo sandbox setup' to recompile against current source.` (Wheel-upgrade scenario.)

**Dependencies:** filesystem readability for the dispatcher binary, the manifest, and the dispatcher source bundle.

#### Scenario: Both binary and source-bundle shas match manifest
- **WHEN** `/usr/local/libexec/sandbox-ai/dispatch`, `/usr/local/libexec/sandbox-ai/dispatcher.manifest.json`, and the dispatcher source bundle are all present; the binary's sha matches `manifest.compiled_sha512`; the current source bundle's sha matches `manifest.source_bundle_sha512`
- **THEN** the check reports PASS

#### Scenario: Manifest absent
- **WHEN** `/usr/local/libexec/sandbox-ai/dispatcher.manifest.json` does not exist
- **THEN** the check reports SKIP with the install-setup hint

#### Scenario: Binary tampered (sha mismatch on compiled_sha512)
- **WHEN** `/usr/local/libexec/sandbox-ai/dispatch` sha differs from `manifest.compiled_sha512` but the current source bundle sha still matches `manifest.source_bundle_sha512`
- **THEN** the check reports WARN with the tamper-hint variant naming the manifest's recorded compiled sha and the binary's current sha

#### Scenario: Wheel upgraded since last setup (sha mismatch on source_bundle_sha512)
- **WHEN** the source bundle's sha differs from `manifest.source_bundle_sha512` but the on-disk binary's sha still matches `manifest.compiled_sha512`
- **THEN** the check reports WARN with the wheel-upgrade-hint variant prompting the operator to re-run setup to recompile

### Requirement: Binary Integrity Posture Check

The doctor SHALL include a check `binary_integrity_posture` that probes the host for the presence and enforcement state of binary-integrity mechanisms:

- **dm-verity**: probe `/proc/cmdline` for `dm-verity` markers AND check `dmsetup status` output for an active verity device on the partition hosting `/usr/local/libexec/sandbox-ai/`. Report ACTIVE / INACTIVE.
- **IMA-appraise**: probe `/sys/kernel/security/ima/policy` for `appraise` directives. Report APPRAISING / NOT-APPRAISING.
- **fapolicyd**: probe `systemctl is-active fapolicyd` AND `fapolicyd-cli --check-status` for enforcing mode. Report ENFORCING / PERMISSIVE / NOT-RUNNING.
- **AIDE**: probe `which aide` AND check for `/var/lib/aide/aide.db` existence. Report INSTALLED-DB-PRESENT / INSTALLED-DB-MISSING / NOT-INSTALLED.

The check SHALL report PASS (informational) regardless of the findings — the check exists to report posture, not to gate. The check's `detail` field SHALL enumerate the four mechanisms' states in a structured format. The check's remediation field SHALL recommend configuration in production-sensitive contexts but SHALL NOT bootstrap any of these tools.

**Dependencies:** filesystem readability for `/proc/cmdline` and `/sys/kernel/security/ima/policy`; presence of `dmsetup`, `systemctl`, `fapolicyd-cli`, `aide` binaries (each probe gracefully reports `NOT-INSTALLED` if the binary is absent).

#### Scenario: All four tools detected and enforcing
- **WHEN** dm-verity is active, IMA is appraising, fapolicyd is enforcing, AIDE is installed with a present DB
- **THEN** the check reports PASS with detail showing all four states; the remediation field is empty (or `posture is fully hardened`)

#### Scenario: None of the four tools active
- **WHEN** none of dm-verity / IMA / fapolicyd / AIDE are active on this host
- **THEN** the check reports PASS (informational) with detail showing all NOT-ENFORCING / NOT-INSTALLED states; remediation suggests `for production hosts, consider configuring dm-verity, IMA-appraise, fapolicyd, or AIDE; sandbox-ai's manifest detects accidental drift but does not provide attack-resistant integrity`

### Requirement: Setup Invariants Check

The doctor SHALL include a check `setup_invariants` that performs a read-only audit of setup's owned-namespace artifacts:

- Each enumerated owned drop-in path is present, owned by root, with the correct mode.
- `/etc/subuid` and `/etc/subgid` entries for the sandbox user exist with adequate range size.
- The `sb-ws` group exists at a gid in the sandbox user's subgid range.
- The operator is a member of `sb-ws` (per `/etc/group`, NOT per running-process supplementary groups — the latter is a different check `dev_in_workspace_bridge_group` and may show stale state pre-relogin).
- `/usr/local/libexec/sandbox-ai/` directory exists with mode 0755 root:root.
- **machinectl-path stability** (`_audit_machinectl_stability`): `machinectl` resolves uniquely on the sudoers `secure_path` basis (inode-deduped per F-005). Post-C-009-D4 the operator SUDO drop-in is **pipe-only** — its per-op `Cmnd_Spec` pins the `systemd-run` byte-pipe launcher (`SYSTEMD_RUN_PATH`), NOT `machinectl` — so there is **no `machinectl` path in the drop-in to match against** (that drop-in match moved to the systemd-run-path-stability sub-check below). This sub-check is therefore **resolve/shadow-only**: it reuses L0's single-source `resolve_machinectl_path` and WARNs only if `machinectl` is absent or non-unique on secure_path. `machinectl` remains load-bearing for the **root L5/L6/L7 setup crossings** (which are not in the operator rule), so an absent / non-unique `machinectl` is still a real stability problem — the resolver raising IS the check. (Matching `machinectl` against the operator drop-in would false-WARN on every healthy C-009 host, since that drop-in no longer contains a `machinectl` path — D-pathstab.)
- **systemd-run-path stability** (`_audit_systemd_run_stability` — the new C-009-D4 sub-check, see the ADDED requirement below): the re-resolved `systemd-run` byte-pipe launcher (`resolve_systemd_run_path`, on the same `secure_path` basis) equals the absolute path pinned in the installed SUDO drop-in's pipe `Cmnd_Spec` entries. Detects post-setup drift (a second/shadowing `systemd-run` earlier on secure_path) that would silently break the orchestrator's `sudo systemd-run --pipe …` op grant even though the drop-in is present and well-formed. This is the steady-state counterpart of L3a's setup-time relative-form pipe probe (B-3 defense), and is the SUDO-mode analogue of what the machinectl-path-stability check did before the pipe switch.
- **sudoers rule-body content audit** (the F-004 `sudoers_rule_shape` audit, folded here rather than a standalone check — same data source, same WARN policy): re-render the expected `SANDBOX_OPS` body from the current `core.dispatch.Op` enum + operator + hostname + resolved `SYSTEMD_RUN_PATH` (NOT `MACHINECTL_PATH` — the SUDO operator rule renders the per-op **pipe** `Cmnd_Spec` keyed on the `systemd-run` launcher), and compare against the installed `/etc/sudoers.d/sandbox-ai-machinectl-<operator>`. Specifically assert: (a) the enumerated op set exactly equals `core.dispatch.Op` (the twelve-op enum, including `preflight` and the streaming `fwd` — neither under- nor over-enumerated, catching a wheel upgrade that added/removed an op without a setup re-run; a drop-in missing the `fwd` spec is exactly the stale-host state that breaks separate-user SUDO attach, so this WARN is the named remedy surface for C-010's migration); (b) ZERO `"` (double-quote) characters appear in any `Cmnd_Spec` body (the F-004 silent-footgun shape — a drop-in that passes `visudo -cf` but matches nothing at runtime); (c) every op-name segment matches `[a-z0-9-]+` and embedded whitespace is backslash-escaped (not quoted). On any mismatch: WARN `sudoers drop-in content drifted from canonical (F-004 / op-enum drift): <specifics>. Run 'sudo sandbox setup' to regenerate.`
- **sudo-version floor**: parse `sudo --version`; the rule shape is empirically validated on sudo **1.9.5p2 → 1.9.17p2** (V9c/V9e/V9e-2, 11 distro images incl. the RHEL 8.10 / Rocky 8.9 floor). If the host's sudo is **older than 1.9.5p2** (only EOL distros — RHEL 7 = 1.8.23, Debian 10 = 1.8.27), WARN: `sudo <version> predates the validated floor 1.9.5p2; the V9 sudoers rule shape is unverified on this version and 'Defaults fast_glob' may be load-bearing-and-unconfirmed here. Supported enterprise distros ship ≥1.9.5p2.` This is the steady-state surface of the same check L0 performs at setup time; it WARNs (does not FAIL) consistent with this check's policy and because sub-floor sudo only occurs on out-of-support EOL distros.

The check SHALL report PASS if all invariants hold. For any missing/wrong invariant, the check SHALL report WARN (not FAIL — drift may be operator-intentional) with detail naming the specific invariant violated and remediation `run 'sudo sandbox setup' to restore canonical setup state`.

**Operator resolution under `sandbox doctor`.** The audit needs the operator identity to locate the per-operator drop-in and check `sb-ws` membership. It first tries setup's strict `resolve_operator()` (precedence `--operator` → `$SUDO_USER` → refuse). Under a **plain `sandbox doctor`** — run by the operator AS THEMSELVES, not via `sudo` — that precedence has no context and raises; the check MUST then fall back to the **current real user** (`pwd.getpwuid(os.getuid())`), which IS the operator in that invocation, and run the full audit. It MUST NOT short-circuit with an "operator unresolvable" WARN (that left the audit dead in doctor's normal, non-sudo invocation). `resolve_operator()` itself stays strict for `setup` (which MUST refuse without explicit context — no current-user heuristic).

**Root-only drop-in under a non-root invocation.** The per-operator sudoers drop-in is `0440 root:root` inside a `0750` `/etc/sudoers.d/`, so a plain `sandbox doctor` (running as the operator) cannot read it. Reading it MUST raise no uncaught exception (a `PermissionError` MUST NOT crash the doctor run): the check treats an unreadable-but-present drop-in as **NOT missing** (no "missing" violation) and **skips** the rule-body + systemd-run-stability audits that need its content, returning the operator-readable invariants' verdict with a note that the rule is validated at install time by setup's L3a per-op probe. (The machinectl-path-stability sub-check still runs — it is resolve/shadow-only and needs no drop-in content. `sudo sandbox doctor` is NOT a fuller path — post round-9/F-021 it resolves root's home, not the operator's, and exits without a config.) The operator-readable invariants (reserved-dir mode/ownership, subuid/subgid ranges, `sb-ws` group membership, sudo-version floor, machinectl-path resolution) are still audited.

**Dependencies:** filesystem readability; presence of `getent`, `stat`, `sudo` (for `sudo --version`); importability of `core.dispatch.Op` (for the rule-body op-enum comparison).

#### Scenario: Fresh post-setup host
- **WHEN** the operator runs `sandbox doctor` immediately after `sudo sandbox setup` completes
- **THEN** the check reports PASS; every enumerated invariant holds

#### Scenario: Plain `sandbox doctor` resolves the operator as the current user
- **WHEN** the operator runs `sandbox doctor` directly (not via `sudo`), so `resolve_operator()`'s setup precedence has no context and raises `OperatorResolutionError`
- **THEN** the check falls back to the current real user (`pwd.getpwuid(os.getuid())`) as the operator and runs the full audit — it does NOT short-circuit with an "operator unresolvable" WARN

#### Scenario: Root-only sudoers drop-in under a plain (operator) `sandbox doctor`
- **WHEN** a plain `sandbox doctor` (operator, not root) audits invariants and reading the `0440 root:root` per-operator sudoers drop-in raises `PermissionError`
- **THEN** the check does NOT crash and does NOT report the drop-in "missing"; it skips the rule-body + systemd-run-stability audits (which need the drop-in content) and reports PASS (assuming the operator-readable invariants hold) with a note that the rule is validated at install time by setup's L3a per-op probe

#### Scenario: Drop-in file removed by operator
- **WHEN** an operator manually `rm`s `/etc/sudoers.d/sandbox-ai-machinectl-<operator>` and runs `sandbox doctor`
- **THEN** the check reports WARN with detail `sudoers drop-in /etc/sudoers.d/sandbox-ai-machinectl-<operator> missing. Run 'sudo sandbox setup' to restore.`

#### Scenario: Second machinectl appears on secure_path post-setup
- **WHEN** post-setup a second executable `machinectl` appears on secure_path (e.g. `/usr/local/bin/machinectl`) and the operator runs `sandbox doctor`
- **THEN** the machinectl-path-stability sub-check reports WARN (the F-005 resolver refuses a non-unique `machinectl`): a second `machinectl` (`<found-path>`) now exists on secure_path. `machinectl` is load-bearing for setup's root L5/L6/L7 crossings, so the non-unique state is a hygiene/availability concern; remediation: `remove the unexpected '<found-path>' (the orchestrator expects only the systemd-provided '/usr/bin/machinectl'), or run 'sudo sandbox setup' to re-evaluate`. WARN (not FAIL) per the check's policy. NOTE: a second `machinectl` no longer breaks the operator **op** crossings — under C-009 those cross via `sudo systemd-run --pipe`, so the operator rule pins `SYSTEMD_RUN_PATH`, not `MACHINECTL_PATH`; a shadowing **`systemd-run`** is what would break an op grant (see the systemd-run-path-stability sub-check).

#### Scenario: Shadowing systemd-run drifts from the pinned pipe Cmnd_Spec
- **WHEN** post-setup the re-resolved `systemd-run` (on the secure_path basis) is no longer the absolute path pinned in the installed SUDO drop-in's pipe `Cmnd_Spec` entries (e.g. a second `systemd-run` appeared earlier on secure_path) and the operator runs `sandbox doctor`
- **THEN** the systemd-run-path-stability sub-check reports WARN naming the drift (`re-resolved systemd-run '<found>' is not the '<pinned>' pinned in the SUDO drop-in's pipe Cmnd_Spec; this drift breaks the orchestrator's sudo_pipe_cmd op crossings`), with remediation to remove the unexpected copy or re-run setup. WARN (not FAIL) per the check's policy

#### Scenario: Operator removed from sb-ws
- **WHEN** an admin runs `gpasswd -d <operator> sb-ws` and the operator runs `sandbox doctor`
- **THEN** the check reports WARN with detail `operator <operator> not in sb-ws group per /etc/group. Run 'sudo sandbox setup' to restore (and log out/in to refresh group set).`

#### Scenario: Installed drop-in contains an F-004 silent-footgun shape
- **WHEN** the installed `/etc/sudoers.d/sandbox-ai-machinectl-<operator>` contains a `"` character inside a `Cmnd_Spec` body (e.g. an operator hand-edited it, or an old buggy renderer wrote it) and the operator runs `sandbox doctor`
- **THEN** the rule-body content audit reports WARN: `sudoers drop-in content drifted from canonical (F-004 silent-footgun shape: double-quote in a Cmnd_Spec — the rule passes 'visudo -cf' but matches nothing at runtime). Run 'sudo sandbox setup' to regenerate.`

#### Scenario: Op enumeration drifted after a wheel upgrade
- **WHEN** the wheel was upgraded so `core.dispatch.Op` gained an op, but `sudo sandbox setup` was not re-run, so the installed drop-in's `SANDBOX_OPS` under-enumerates the current op set, and the operator runs `sandbox doctor`
- **THEN** the rule-body content audit reports WARN naming the missing op(s) and `Run 'sudo sandbox setup' to regenerate the rule for the current dispatcher op set.`

#### Scenario: Host sudo predates the validated floor
- **WHEN** `sandbox doctor` runs on a host whose `sudo --version` is older than 1.9.5p2 (e.g. an EOL RHEL 7 with sudo 1.8.23)
- **THEN** the sudo-version-floor invariant reports WARN: `sudo 1.8.23 predates the validated floor 1.9.5p2; the V9 sudoers rule shape is unverified on this version (supported enterprise distros ship ≥1.9.5p2; only EOL distros are below).` — WARN not FAIL (out-of-support EOL territory, not a functional break per V9c knowledge)

### Requirement: Execution-Mode-Aware Doctor Checks

`sandbox doctor` SHALL honor the active execution mode, which is the **marker-resolved** value (`resolve_execution_mode`, per the `host-config` capability's "Docker Execution Mode Selector" requirement) overlaid onto `host_config.host.docker_execution_mode`; the doctor runner's callers (including `sandbox start`'s pre-flight `run_check_subset`) SHALL thread the resolved mode and the `resolve_daemon_owner` owner so the checks evaluate against the real mode rather than a defaulted/mode-less config. In `operator-rootless` mode `sandbox doctor` SHALL resolve the daemon owner as the invoking operator (`resolve_daemon_owner`) — reading neither a toml nor `docker_unprivileged_user` (the D7 owner-read discipline). In `operator-rootless` mode it SHALL skip the checks that only make sense for the `machinectl` crossing — boundary reachability (`machinectl_reachable`), systemd-machined Service Check, Unprivileged User Existence (the dedicated daemon user), and the dispatcher-integrity checks (dispatcher-sha drift / the setup-invariants machinectl-stability + sudoers-rule-body audit) — and SHALL run the docker/runsc/supply-chain/compose-collision checks **locally**. In `separate-user` mode every check SHALL behave exactly as before. A check that does not apply in the active mode SHALL report an explicit mode-skip status; it SHALL NOT report PASS (no false green).

On a host that has **not been set up** for the invoking operator (`resolve_execution_mode` raises `ModeMarkerMissing`), `sandbox doctor` SHALL NOT guess an execution mode. It SHALL run the mode-INVARIANT checks (filesystem, repo-integrity, per-user-tree) and resolve the daemon owner mode-independently, and SHALL report an explicit **"not set up yet"** mode-skip for every mode-SPECIFIC check (boundary reachability, systemd-machined, dedicated-user existence, dispatcher integrity, docker/runsc/supply-chain/compose-collision), with a friendly notice "this host isn't set up yet — run `sudo sandbox setup`". The notice SHALL NOT name the internal marker or `setup-state.json`. No mode-specific check SHALL report PASS or FAIL on an unprovisioned host — the unprovisioned state is surfaced as an explicit skip, not as a defaulted-mode probe.

#### Scenario: crossing checks skipped in operator-rootless

- **WHEN** `sandbox doctor` runs with `docker_execution_mode == operator-rootless`
- **THEN** the boundary-reachability, systemd-machined, dedicated-user-existence, and dispatcher-integrity checks report a mode-skip status (not PASS), and the docker/runsc/supply-chain checks run as local `docker …` queries with no `machinectl` crossing

#### Scenario: all checks unchanged in separate-user

- **WHEN** `sandbox doctor` runs with `docker_execution_mode == separate-user`
- **THEN** every check behaves exactly as before this change (including the crossing and dispatcher-integrity checks)

#### Scenario: unprovisioned host reports an explicit not-set-up skip

- **WHEN** `sandbox doctor` runs on a host that has not been set up for the invoking operator (`resolve_execution_mode` raises `ModeMarkerMissing`)
- **THEN** doctor runs the mode-invariant checks (filesystem, repo-integrity, per-user-tree) and reports every mode-specific check as an explicit "not set up yet — run `sudo sandbox setup`" mode-skip — it does NOT guess `operator-rootless`, does NOT run the crossing/docker checks under a defaulted mode, and names neither the marker nor `setup-state.json`

### Requirement: Daemon User Privilege Invariant (separate-user)

In `separate-user` mode, `sandbox doctor` SHALL verify that the dedicated daemon user (`docker_unprivileged_user`) has **no path to root via sudo** — neither (a) membership in a privilege-granting group (`sudo`, `wheel`, or other admin group) NOR (b) a sudoers-**policy** grant (an `/etc/sudoers.d/` drop-in or a `NOPASSWD` rule), detected by parsing `sudo -n -l -U <user>`. This no-privilege property is what makes the separate-user blast-radius reduction load-bearing: a container/runtime escape that reaches the daemon owner lands on a dead-end account only if that account cannot escalate. The sudoers-policy query (`-U <user>`) requires root; under a non-root `sandbox doctor` it is **not determinable**, so the check falls back to group membership only and notes that the full audit needs `sudo sandbox doctor` — it SHALL NOT emit a false WARN on the indeterminate case. The check SHALL be WARN severity (an operator who deliberately privileged the daemon user should be told, not hard-blocked). It MAY be implemented as a standalone check or folded into the existing `setup_invariants` check.

#### Scenario: daemon user with no sudo path passes

- **WHEN** `sandbox doctor` runs in `separate-user` mode and the daemon user is in no `sudo`/`wheel`/admin group and (where determinable) the sudoers policy grants it no sudo
- **THEN** the invariant check passes

#### Scenario: daemon user with a group OR policy sudo grant warns

- **WHEN** the dedicated daemon user is a member of `sudo` (or `wheel`/admin), OR the sudoers policy grants it sudo (a drop-in / NOPASSWD rule)
- **THEN** the check emits a WARN explaining that a privileged daemon user defeats the separate-user blast-radius reduction, and how to remove the grant

#### Scenario: non-root doctor cannot query another user's sudoers policy

- **WHEN** `sandbox doctor` runs as a non-root user (so `sudo -n -l -U <daemon-user>` is not permitted) and the daemon user is in no admin group
- **THEN** the check passes on the group evidence and notes that the sudoers-policy audit was not checked (run `sudo sandbox doctor` for the full audit) — it does NOT emit a false WARN

### Requirement: Sudoer Daemon-Owner Warning (operator-rootless)

In `operator-rootless` mode, `sandbox doctor` SHALL WARN when the operator account that owns rootless Docker is a sudoer — detected by **either** (a) membership in `sudo`/`wheel`/admin, **or** (b) a sudoers-**policy** grant (an `/etc/sudoers.d/` drop-in or `NOPASSWD` rule), determined by parsing `sudo -n -l` for the invoking operator (a self-query needing no root). NOPASSWD is the instant-escalation, common cloud-VM / cloud-init case, so it MUST be detected even when the operator is in no admin group. The WARN SHALL name the detected path (admin group(s), and/or passwordless-via-the-sudoers-policy / drop-in), explain that because the daemon owner can `sudo` a (rare, gVisor-fronted) escape reaching it could escalate to root, and point to two remedies: run sandboxes as a dedicated **non-sudo** operator account, or set `docker_execution_mode = separate-user`. This SHALL be WARN severity, never FAIL — it is an informed-tradeoff signal, not a misconfiguration.

#### Scenario: group-sudoer operator owner warns

- **WHEN** `sandbox doctor` runs in `operator-rootless` mode and the operator account is a member of `sudo`/`wheel`/admin
- **THEN** a WARN is emitted naming the escalation tradeoff and the two remedies (dedicated non-sudo operator, or `separate-user` mode)

#### Scenario: drop-in / NOPASSWD operator owner warns (no admin group)

- **WHEN** `sandbox doctor` runs in `operator-rootless` mode and the operator account is in **no** admin group but the sudoers policy grants it sudo via an `/etc/sudoers.d/` drop-in or NOPASSWD rule
- **THEN** a WARN is emitted naming the passwordless-sudo-via-the-sudoers-policy path and the two remedies

#### Scenario: non-sudo operator owner is clean

- **WHEN** `sandbox doctor` runs in `operator-rootless` mode and the operator account is in no `sudo`/`wheel`/admin group **and** the sudoers policy grants it no sudo
- **THEN** no sudoer-owner WARN is emitted

### Requirement: Host CPU Capacity Check

`sandbox doctor` SHALL include a check that, for each registered instance with a rendered `compose.yml`, compares every service's `cpus` limit against the host's CPU count (obtained as in the `hydration-pipeline` capability) and emits a WARN-severity status when any rendered `cpus` exceeds the host count. The check SHALL source the limits from the instance's rendered `compose.yml` (the authoritative artifact), not from re-spelled constants. The warning SHALL name the offending service, its `cpus` value, and the host CPU count, so the operator sees an actionable message in place of Docker's `range of CPUs is from 0.01 to N.NN` error. Instances without a rendered `compose.yml` SHALL be skipped.

Because hydration clamps CPU limits at render, this check primarily guards on-disk divergence — compose rendered before host-aware clamping existed, rendered on a larger host and relocated, or hand-edited.

#### Scenario: Rendered CPU limit exceeds host count

- **WHEN** an instance's rendered `compose.yml` requests `cpus: "4.0"` for a service and the host has 2 CPUs
- **THEN** `sandbox doctor` reports a WARN naming the service, `4.0`, and the host's `2`

#### Scenario: Rendered CPU limits fit the host

- **WHEN** every service's rendered `cpus` is at or below the host CPU count
- **THEN** the check reports OK (no warning)

#### Scenario: Instance without rendered compose is skipped

- **WHEN** a registered instance has no rendered `compose.yml`
- **THEN** the CPU capacity check skips that instance without error

### Requirement: Instance Memory Over-Commit Check

`sandbox doctor` SHALL include a check that, for each registered instance with a rendered `compose.yml`, sums the services' `mem_limit` values and emits a WARN-severity status when the total exceeds the host's physical RAM. Host RAM SHALL be read locally (e.g. `os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")`). The limits SHALL be sourced from the rendered `compose.yml`, parsed from Docker size strings (e.g. `"8gb"`, `"512m"`) to bytes. The warning SHALL name the summed request, the host RAM, and the consequence (containers may be OOM-killed under memory pressure). This is advisory only — a WARN does not, by itself, flip the doctor exit contract to failure. Instances without a rendered `compose.yml` SHALL be skipped.

Memory limits are not clamped (a `mem_limit` above host RAM over-commits rather than failing `start`); this check provides visibility only.

#### Scenario: Summed memory request exceeds host RAM

- **WHEN** an instance's rendered services sum to a `mem_limit` total greater than the host's physical RAM
- **THEN** `sandbox doctor` reports a WARN naming the summed request, the host RAM, and the OOM-under-pressure consequence

#### Scenario: Summed memory request fits host RAM

- **WHEN** the summed `mem_limit` total is at or below the host's physical RAM
- **THEN** the check reports OK (no warning)

#### Scenario: Over-commit warning is advisory

- **WHEN** the memory over-commit check emits a WARN
- **THEN** the WARN alone does not change the doctor exit code to a failure (consistent with the existing Warn Severity Status contract)

### Requirement: systemd-run Path-Stability Sub-Check (`_audit_systemd_run_stability`)

`setup_invariants` SHALL include a `_audit_systemd_run_stability` sub-check that confirms the byte-pipe launcher pinned in the installed SUDO drop-in is still the one that resolves on the host. Post-C-009-D4 the operator SUDO drop-in is the `sudo_pipe_cmd` crossing, so each per-op `Cmnd_Spec` pins the absolute `SYSTEMD_RUN_PATH`. If the re-resolved `systemd-run` is no longer the one pinned in the installed drop-in (a second copy, a shadow earlier on secure_path), the SUDO op grant breaks even though the drop-in is present and well-formed. The sub-check SHALL:

- resolve `systemd-run` on the sudoers `secure_path` basis via L0's single-source `resolve_systemd_run_path` (the same `secure_path` / inode-dedupe basis the renderer used), surfacing a resolver refusal (absent / non-unique) as a WARN;
- when the drop-in content is available, assert the resolved absolute path is present in the installed drop-in's pipe `Cmnd_Spec` bodies; on drift, WARN naming the resolved-vs-pinned mismatch and that it breaks the orchestrator's `sudo_pipe_cmd` op crossings;
- skip the drop-in match (without a "missing" violation) when the drop-in content is unavailable (the `0440 root:root` unreadable-under-plain-doctor case), consistent with the parent check's content-skip policy.

This is the SUDO-mode steady-state counterpart of L3a's setup-time relative-form pipe probe and the analogue of the machinectl-path-stability check before the pipe switch. It is WARN-not-FAIL per the parent check's policy.

#### Scenario: systemd-run resolves and matches the pinned pipe Cmnd_Spec
- **WHEN** `systemd-run` resolves uniquely on secure_path to the same absolute path pinned in the installed SUDO drop-in's pipe `Cmnd_Spec` entries
- **THEN** the sub-check passes (no WARN contributed)

#### Scenario: systemd-run resolution drift breaks the op grant
- **WHEN** the re-resolved `systemd-run` differs from the path pinned in the drop-in's pipe `Cmnd_Spec` (a second/shadowing copy earlier on secure_path)
- **THEN** the sub-check WARNs naming the resolved-vs-pinned mismatch and that it breaks the orchestrator's `sudo_pipe_cmd` op crossings; remediation: remove the unexpected copy or run `sudo sandbox setup`

#### Scenario: drop-in content unavailable under a plain doctor
- **WHEN** the SUDO drop-in is present but unreadable (`0440 root:root`, plain operator `sandbox doctor`)
- **THEN** the sub-check still resolves `systemd-run` (resolve/shadow signal) but skips the drop-in `Cmnd_Spec` match without contributing a "missing" violation

### Requirement: Retired Host Config File Detection
`sandbox doctor` SHALL detect a leftover canonical host config file at `<sandbox_ai_home()>/config/sandbox-ai.toml` and WARN that it is obsolete. `sandbox-ai.toml` is retired (the `host-config` capability); host facts are now setup-determined and recorded by `sudo sandbox setup`. The warning SHALL direct the operator to delete the file and re-run setup, and SHALL NOT reference the internal marker.

#### Scenario: Retired canonical host config detected
- **WHEN** `sandbox doctor` runs and `<sandbox_ai_home()>/config/sandbox-ai.toml` exists
- **THEN** the doctor emits a WARN: "Found an obsolete `<resolved-home>/config/sandbox-ai.toml`. Host config is now setup-determined — delete this file and run `sudo sandbox setup`." and names neither the marker nor `setup-state.json`

#### Scenario: No warning when the retired file is absent
- **WHEN** `sandbox doctor` runs and no `<sandbox_ai_home()>/config/sandbox-ai.toml` exists
- **THEN** the doctor emits no retired-host-config warning

