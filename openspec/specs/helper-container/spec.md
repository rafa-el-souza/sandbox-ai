## Purpose

This specification defines the disposable-helper-container primitives used by the orchestrator's helper-recipe phases for ownership and directory creation operations that survive runsc's gofer/directfs boundary. Inline `docker run … busybox` invocations are forbidden; all such operations route through the two primitives in `core.helper_container`.

## Requirements

### Requirement: Helper Container Module Location

The disposable-helper-container primitives SHALL reside in `src/core/helper_container.py` and expose two public functions: `helper_chown_files` and `helper_mkdir_chown_dirs`. All orchestrator code that needs to chown or mkdir as in-userns root SHALL invoke these primitives instead of constructing `docker run` commands inline.

#### Scenario: Module exposes the two primitives
- **WHEN** `core.helper_container` is imported
- **THEN** `helper_chown_files` and `helper_mkdir_chown_dirs` are public callables

#### Scenario: No inline docker run for chown
- **WHEN** the codebase is searched for `docker run … busybox`
- **THEN** the only matches are inside `core/helper_container.py`; all other call sites import the primitives

### Requirement: Helper Image Pinning

The helper container SHALL use the image `IMAGE_REGISTRY["busybox_musl"].pinned` (i.e., `busybox@sha256:<digest>`). The image reference SHALL NOT be hardcoded in `helper_container.py`; it SHALL be looked up via the existing `IMAGE_REGISTRY` so that the image-digest-rotation workflow covers it.

#### Scenario: Image reference comes from IMAGE_REGISTRY
- **WHEN** `helper_chown_files` or `helper_mkdir_chown_dirs` is invoked
- **THEN** the docker run command uses `IMAGE_REGISTRY["busybox_musl"].pinned` for the image argument

#### Scenario: No unpinned `busybox` reference
- **WHEN** the codebase is searched for the literal string `busybox` (without `@sha256:`) in any production code
- **THEN** no occurrences exist in non-test files

### Requirement: Helper Container Hardening Flags

Every helper invocation SHALL include the full hardening flag set: `--runtime=runc`, `--network=none`, `--read-only`, `--tmpfs /tmp`, `--user 0:0`, `--cap-drop ALL`, `--cap-add CHOWN`, `--cap-add DAC_OVERRIDE`, `--security-opt no-new-privileges:true`. The flags SHALL be constructed centrally in `helper_container.py` so both primitives use the same set.

#### Scenario: All hardening flags present on every invocation
- **WHEN** `helper_chown_files` or `helper_mkdir_chown_dirs` constructs its docker run command
- **THEN** the command includes every flag in the hardening set

#### Scenario: runc runtime selected
- **WHEN** the helper invocation includes `--runtime`
- **THEN** the value is `runc` (NOT `runsc`); rationale: helper is short-lived, fully orchestrator-controlled, and requires CAP_CHOWN/DAC_OVERRIDE that runc supports cleanly

#### Scenario: Capabilities minimized
- **WHEN** the helper invocation's capabilities are inspected
- **THEN** only `CHOWN` and `DAC_OVERRIDE` are added; all other capabilities are dropped

### Requirement: Helper Invocation Timeout

Every helper invocation SHALL apply a wall-clock timeout (default 30 seconds; configurable per-call). On timeout, the helper container SHALL be killed and the calling code SHALL raise `SandboxExecutionError` identifying the file/dir set the helper was operating on and the elapsed time.

#### Scenario: Default 30-second timeout
- **WHEN** `helper_chown_files` is invoked without an explicit timeout
- **THEN** the docker run command runs with a 30-second wall-clock limit enforced by the orchestrator (not relying solely on docker's internal timeouts)

#### Scenario: Timeout raises with diagnostic context
- **WHEN** a helper invocation exceeds its timeout
- **THEN** `SandboxExecutionError` is raised with a message including the file/dir set, the consumer uid/gid, and the elapsed seconds

### Requirement: helper_chown_files Primitive Contract

`helper_chown_files(host_user, parent, files, owner_uid, owner_gid, mode, machinectl_auth)` SHALL run a single helper container that, for each file in `files`, executes `cp <parent>/<file> /tmp/<file> && chown <owner_uid>:<owner_gid> /tmp/<file> && chmod <mode> /tmp/<file> && mv /tmp/<file> <parent>/<file>`. The order of operations within the loop is significant — the chown precedes the move-back so the destination's perceived ownership is the new owner from the moment of the rename.

#### Scenario: Multiple files batched in one container
- **WHEN** `helper_chown_files` is called with N files sharing a parent
- **THEN** ONE docker run invocation handles all N files via a single sh -c loop (batching reduces helper-container startup overhead)

#### Scenario: Empty file list is a no-op
- **WHEN** `helper_chown_files` is called with an empty `files` list
- **THEN** no docker run is executed; the function returns immediately

#### Scenario: Idempotent on already-correct files
- **WHEN** `helper_chown_files` is called against files that are already at the target owner/mode
- **THEN** the operation completes successfully (cp/chown/chmod/mv are idempotent on already-correct values)

### Requirement: helper_mkdir_chown_dirs Primitive Contract

`helper_mkdir_chown_dirs(host_user, parent, leaves, owner_uid, owner_gid, machinectl_auth)` SHALL run a single helper container that, for each leaf in `leaves`, executes `mkdir -p <parent>/<leaf> && chown <owner_uid>:<owner_gid> <parent>/<leaf>`. The function SHALL NOT chmod the leaf — chmod on a directory collapses the ACL mask and clamps inherited named entries (e.g., `u:dev:rwx`) to `r-x`.

#### Scenario: mkdir uses -p for idempotency
- **WHEN** `helper_mkdir_chown_dirs` runs against an existing leaf
- **THEN** `mkdir -p` succeeds without error; the existing leaf is preserved

#### Scenario: chown is non-recursive
- **WHEN** `helper_mkdir_chown_dirs` chowns the leaf
- **THEN** only the leaf itself is chowned (not its contents); inherited ACLs handle access for any pre-existing contents inside the leaf

#### Scenario: No chmod on the leaf
- **WHEN** `helper_mkdir_chown_dirs` constructs its sh -c command
- **THEN** the command contains no `chmod` for any leaf (chmod would collapse the ACL mask)

#### Scenario: Multiple leaves batched in one container
- **WHEN** `helper_mkdir_chown_dirs` is called with N leaves sharing a parent
- **THEN** ONE docker run invocation handles all N leaves

### Requirement: Helper Container Bind-Mount Source

The helper container SHALL bind-mount the parent directory containing the target files/leaves at `/p` inside the container (read-write, since the helper needs to write back). It SHALL NOT bind-mount any directory the operation does not need to access.

#### Scenario: Parent dir mounted at /p
- **WHEN** `helper_chown_files(parent="/some/dir", files=[...])` is invoked
- **THEN** the docker run command includes `-v /some/dir:/p` (or equivalent)

#### Scenario: No extraneous mounts
- **WHEN** the helper invocation's mount list is inspected
- **THEN** only the parent directory is mounted; no `--mount type=bind` exists for any directory outside the operation's scope

### Requirement: Helper Invocation via machinectl

The helper container SHALL be launched via the same `machinectl shell <docker_unprivileged_user>@.host /usr/bin/docker run …` boundary used by the rest of the orchestrator, with the auth mode taken from the `machinectl_auth` argument (sourced from `HostConfig.host.machinectl_authentication`).

#### Scenario: Helper crosses the privilege boundary
- **WHEN** any helper primitive is invoked
- **THEN** the docker run command is wrapped in `machinectl shell` per the auth mode (sudo or polkit), matching `core.host_config.machinectl_cmd()`

#### Scenario: No bare docker invocation
- **WHEN** the codebase is searched for direct `docker run` calls without a machinectl wrapper
- **THEN** none exist in helper-related code paths
