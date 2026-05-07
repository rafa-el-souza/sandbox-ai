## Purpose

This specification defines the `sandbox start --dry-run` simulation pipeline, which validates the full start lifecycle without side effects — covering instance resolution, IPAM slot preview, template validation, tooling plane file verification, and command preview.

## Requirements

### Requirement: Dry-Run Flag on Start
The system SHALL accept a `--dry-run` flag on `sandbox start <inst>` that simulates the full start pipeline without side effects.

#### Scenario: Dry-run invoked
- **WHEN** the operator runs `sandbox start <inst> --dry-run`
- **THEN** the system executes all validation phases and reports what each phase would do, without writing files, modifying the IPAM ledger, executing subprocesses, or acquiring locks

#### Scenario: Dry-run exit code on success
- **WHEN** dry-run completes and all phases validate
- **THEN** the process exits with code 0

#### Scenario: Dry-run exit code on failure
- **WHEN** dry-run detects a problem (invalid config, missing template, missing tooling plane file)
- **THEN** the process exits with code 1 with the failure reason displayed

### Requirement: Instance Resolution in Dry-Run
The system SHALL resolve the instance from the registry in dry-run mode using the same read-only lookup as normal start. Dry-run SHALL require a prior `sandbox init <inst>`. The `docker_unprivileged_user` SHALL be sourced from host config (`sandbox-ai.toml`), not from instance config. Resolution is by explicit `<inst>` argument; CWD-based discovery is removed.

#### Scenario: Existing instance resolved
- **WHEN** dry-run is invoked and `<inst>` has a registered entry in `~/.sandbox-ai/state/instances.json`
- **THEN** the instance directory and config are resolved and displayed

#### Scenario: Existing instance with incomplete secrets
- **WHEN** dry-run resolves an existing instance and `.sandbox.env` is missing keys required by the current config (e.g., `PG_PASSWORD` when `db_postgres.enabled = true`)
- **THEN** dry-run reports the missing secrets as warnings

#### Scenario: No instance found — error with guidance
- **WHEN** dry-run is invoked and `<inst>` is not present in the registry
- **THEN** the CLI exits with "No sandbox instance named '<inst>'. Run `sandbox init <inst>` first." and exit code 1

### Requirement: IPAM Slot Preview
The system SHALL compute the IPAM slot that would be allocated without writing to the ledger.

#### Scenario: Preview slot for new instance
- **WHEN** dry-run computes IPAM for an instance not in the ledger
- **THEN** the lowest available slot is displayed along with six derived subnets (isolated, core_proxy, dns, admin, admin_proxy, egress), with a note that the slot is subject to concurrent changes

#### Scenario: Preview slot for existing instance
- **WHEN** dry-run computes IPAM for an instance already in the ledger
- **THEN** the existing allocation is displayed

### Requirement: Template Validation
The system SHALL render all Jinja2 templates to memory during dry-run, validating that they parse correctly with the computed context.

#### Scenario: All templates valid
- **WHEN** all templates render without errors
- **THEN** dry-run reports the count of validated templates and their output paths

#### Scenario: Template error detected
- **WHEN** a Jinja2 template contains an undefined variable or syntax error
- **THEN** dry-run reports the template path and the specific error, then exits with code 1

### Requirement: Tooling Plane File Verification
The system SHALL verify that all files referenced by the hydration pipeline exist in the tooling plane, using the parsed config to determine which conditional files are required.

#### Scenario: All files present including config-dependent variants
- **WHEN** the config specifies `base_distro_family = "wolfi"` for core and `base_distro_family = "debian"` for admin
- **THEN** dry-run verifies `templates/docker/core/Dockerfile.core.wolfi` and `templates/docker/admin/Dockerfile.admin.debian` exist, along with all unconditional files

#### Scenario: Conditional extras verified when enabled
- **WHEN** the config has `components.db_postgres.enabled = true` and `components.mcp_firecrawl = true`
- **THEN** dry-run additionally verifies `templates/docker/extras/db-postgres.yml`, `templates/docker/extras/mcp-firecrawl.yml`, and `templates/docker/extras/Dockerfile.mcp-firecrawl`

#### Scenario: Missing file detected
- **WHEN** a required file is missing from the tooling plane
- **THEN** dry-run reports the missing file path and exits with code 1

### Requirement: Command Preview
The system SHALL display the exact subprocess commands that would be executed during a real start. Command previews SHALL reflect the configured `machinectl_authentication` mode — omitting the `sudo` prefix when mode is `"polkit"`. The preview SHALL enumerate each ownership-sensitive phase's planned operations separately (named-ACL grants from `_acl_grant_plan`; cache/log mkdir+chown from `_helper_mkdir_chown_plan`; ro-file cp+chown from `_helper_cp_chown_plan`; per-workspace shared-group operations from `_workspace_shared_group_plan` — fanned out per workspace in `[workspaces]`).

The `docker compose up` command displayed by the preview SHALL be obtained from the same `_compose_up_cmd_plan` helper that `_phase_compose_up` uses for live execution (per `cli-start`'s "Compose Environment File Flag" requirement). The displayed inner `bash -c` command string SHALL be byte-identical to what the live path would execute given the same `(instance_dir, project_name, config)` inputs. The preview SHALL NOT reconstruct the compose command from local variables in the dry-run helper-recipe loops; in particular, no inner-loop variable in the helper-mkdir or helper-cp preview blocks SHALL shadow the compose-files string.

#### Scenario: Compose command displayed
- **WHEN** dry-run completes validation
- **THEN** the full `docker compose` command is displayed, including all `-f` flags for component-conditional extras

#### Scenario: Compose command matches live execution byte-for-byte

- **WHEN** `sandbox start <inst> --dry-run` is invoked and the compose up command is displayed, and `sandbox start <inst>` is invoked for the same instance and configuration with `_phase_compose_up`'s subprocess invocation captured
- **THEN** the inner `bash -c` argument string rendered in the dry-run preview equals the inner `bash -c` argument string passed to `Executor.run` by `_phase_compose_up`, byte-for-byte

#### Scenario: Helper-cp preview does not corrupt the compose preview

- **WHEN** dry-run runs against an instance whose hydration emits one or more helper-cp groups (e.g., `ipc_known_hosts`, `ipc_ssh_key`)
- **THEN** the rendered compose up command contains `docker compose -f <instance_dir>/docker/compose.yml [...]` and does NOT contain helper-cp filenames joined by `, ` in place of compose file flags

#### Scenario: Handover command displayed (sudo mode)
- **WHEN** dry-run completes validation and `machinectl_authentication` is `"sudo"`
- **THEN** the preview shows `sudo machinectl shell ... docker exec -it`

#### Scenario: Handover command displayed (polkit mode)
- **WHEN** dry-run completes validation and `machinectl_authentication` is `"polkit"`
- **THEN** the preview shows `machinectl shell ... docker exec -it` without `sudo` prefix

#### Scenario: Named-ACL grant commands displayed (per-workspace)
- **WHEN** dry-run completes validation for an instance with workspaces `main` and `scratch`
- **THEN** the `setfacl` commands emitted by `_acl_grant_plan()` are displayed: instance root, docker/, config/, secrets/ traverse, .sandbox.env, ancestor traverse (deduplicated across instance and workspace chains), AND per-workspace named-ACL effective + default for both `<main.path>` and `<scratch.path>`

#### Scenario: Helper-recipe operations displayed with per-workspace fan-out
- **WHEN** dry-run completes validation
- **THEN** the cache/log helper-mkdir+chown plan and the ro-files helper-cp+chown plan are displayed with their resolved consumer-uid:gid and mode values; the per-workspace shared-group plan is displayed with the resolved bridge-gid AND the list of workspace paths each operation will be applied to

#### Scenario: Helper-recipe plans degrade gracefully when unresolvable
- **WHEN** dry-run runs on a host where the bridge group or subuid range cannot be resolved
- **THEN** the preview reports each unresolvable plan with a clear "unavailable" annotation rather than crashing

### Requirement: IPAMLedger Read-Only Peek
The `IPAMLedger` class SHALL provide a `peek_next_slot(instance_id)` method that returns `tuple[int, bool]` — the slot index and whether the instance already has an existing allocation — without acquiring a lock or modifying the ledger.

#### Scenario: Peek returns next available slot for new instance
- **WHEN** `peek_next_slot` is called for an instance not in the ledger
- **THEN** it returns `(lowest_available_base_index, False)` without writing to the ledger file

#### Scenario: Peek returns existing allocation
- **WHEN** `peek_next_slot` is called for an instance already in the ledger
- **THEN** it returns `(existing_base_index, True)`

#### Scenario: Peek on exhausted ledger
- **WHEN** `peek_next_slot` is called for a new instance and all 5,705 slots are consumed
- **THEN** it raises `IPAMExhaustedError`
