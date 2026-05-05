## Purpose

This specification defines the `sandbox start --dry-run` simulation pipeline, which validates the full start lifecycle without side effects — covering instance resolution, IPAM slot preview, template validation, tooling plane file verification, and command preview.

## Requirements

### Requirement: Dry-Run Flag on Start
The system SHALL accept a `--dry-run` flag on `sandbox start` that simulates the full start pipeline without side effects.

#### Scenario: Dry-run invoked
- **WHEN** the operator runs `sandbox start --dry-run`
- **THEN** the system executes all validation phases and reports what each phase would do, without writing files, modifying the IPAM ledger, executing subprocesses, or acquiring locks

#### Scenario: Dry-run exit code on success
- **WHEN** dry-run completes and all phases validate
- **THEN** the process exits with code 0

#### Scenario: Dry-run exit code on failure
- **WHEN** dry-run detects a problem (invalid config, missing template, missing tooling plane file)
- **THEN** the process exits with code 1 with the failure reason displayed

### Requirement: Instance Resolution in Dry-Run
The system SHALL resolve the instance from the registry in dry-run mode using the same read-only lookup as normal start. Dry-run SHALL require a prior `sandbox init`. The `docker_unprivileged_user` SHALL be sourced from host config (`sandbox-ai.toml`), not from instance config.

#### Scenario: Existing instance resolved
- **WHEN** dry-run is invoked and the project directory has a registered instance
- **THEN** the instance directory and config are resolved and displayed

#### Scenario: Existing instance with incomplete secrets
- **WHEN** dry-run resolves an existing instance and `.sandbox.env` is missing keys required by the current config (e.g., `PG_PASSWORD` when `db_postgres.enabled = true`)
- **THEN** dry-run reports the missing secrets as warnings

#### Scenario: No instance found — error with guidance
- **WHEN** dry-run is invoked and no instance exists for the project directory
- **THEN** the CLI exits with "No sandbox instance found. Run `sandbox init` first." and exit code 1

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
The system SHALL display the exact subprocess commands that would be executed during a real start. Command previews SHALL reflect the configured `machinectl_authentication` mode — omitting the `sudo` prefix when mode is `"polkit"`. The preview SHALL enumerate each ownership-sensitive phase's planned operations separately (named-ACL grants from `_acl_grant_plan`; cache/log mkdir+chown from `_helper_mkdir_chown_plan`; ro-file cp+chown from `_helper_cp_chown_plan`; workspace shared-group operations from `_workspace_shared_group_plan`).

#### Scenario: Compose command displayed
- **WHEN** dry-run completes validation
- **THEN** the full `docker compose` command is displayed, including all `-f` flags for component-conditional extras

#### Scenario: Handover command displayed (sudo mode)
- **WHEN** dry-run completes validation and `machinectl_authentication` is `"sudo"`
- **THEN** the preview shows `sudo machinectl shell ... docker exec -it`

#### Scenario: Handover command displayed (polkit mode)
- **WHEN** dry-run completes validation and `machinectl_authentication` is `"polkit"`
- **THEN** the preview shows `machinectl shell ... docker exec -it` without `sudo` prefix

#### Scenario: Named-ACL grant commands displayed
- **WHEN** dry-run completes validation
- **THEN** the `setfacl` commands emitted by `_acl_grant_plan()` are displayed (instance root, docker/, config/, secrets/ traverse, .sandbox.env, ancestor traverse, workspace named-ACL effective + default)

#### Scenario: Helper-recipe operations displayed
- **WHEN** dry-run completes validation
- **THEN** the cache/log helper-mkdir+chown plan and the ro-files helper-cp+chown plan are displayed with their resolved consumer-uid:gid and mode values; the workspace shared-group plan is displayed with the resolved bridge-gid

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
