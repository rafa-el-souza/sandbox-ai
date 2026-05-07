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

### Requirement: Helper Test Mocking Policy

Tests that mock the `Executor.run` invocation issued by `helper_chown_files` or `helper_mkdir_chown_dirs` (or that mock the docker argv assembled by `_hardened_docker_run`) MUST document the rationale at the mock site via an inline comment. Mocks are acceptable for argv-shape assertions (verifying the presence of `--cap-drop ALL`, the pinned image digest, the absence of `--userns=host`, the read-only rootfs flag, the bind-mount target, and similar argv-level invariants) but MUST NOT be the sole coverage for end-to-end ownership semantics.

The capability SHALL maintain at least one integration test under `tests/integration/` that invokes a real helper container against a real rootless userns and asserts that, after the helper completes, the resulting on-disk file or directory's `st_uid` and `st_gid` (as reported by `os.stat`) match the host-absolute target uid/gid passed to the helper. The test SHALL be invocable via `make test-integration` on any host with the prerequisites configured (daemon user reachable via `machinectl shell`, `/etc/subuid` entry for that user, rootless docker, busybox image accessible). The test MAY skip with a clear `pytest.skip(reason)` message when any prerequisite is unavailable; the skip reason MUST be specific enough to identify which precondition failed so the operator (or future CI log reader) can act on it.

CI execution of this test is NOT a requirement of this capability and is explicitly out of scope until the project is hosted on GitHub with a self-hosted runner provisioned (tracked in `openspec/deferred.md`). The test functions as a manual pre-merge gate today; the test source itself does not change when CI is later wired up — only the workflow YAML gains a job that invokes `make test-integration` against the test on the self-hosted runner.

#### Scenario: Argv-shape mock has rationale comment
- **WHEN** a unit test mocks `Executor.run` to assert on the assembled docker argv (e.g., that `--cap-drop ALL` appears in the command line)
- **THEN** an inline comment at the mock site documents that the mock covers argv-shape only and that ownership semantics are exercised separately via the integration test

#### Scenario: End-to-end ownership integration test exists and is invocable
- **WHEN** the test suite is enumerated
- **THEN** at least one test under `tests/integration/` invokes a real helper container (no mocks on `Executor.run` for that test), creates a tmp file or directory, runs `helper_chown_files` or `helper_mkdir_chown_dirs` against it with a host-absolute uid/gid drawn from the daemon user's subuid range, and asserts via `os.stat` that the resulting ownership matches the host-absolute target. The test is invocable via `make test-integration` on any host with the daemon user, subuid entry, and rootless docker configured.

#### Scenario: Integration test skip is observable
- **WHEN** the integration test is skipped because the docker binary, daemon user, `/etc/subuid` entry, or busybox image is unavailable
- **THEN** `pytest.skip` is called with a string identifying which precondition failed (e.g., "skipped: /etc/subuid has no entry for claude-sandbox"); the skip message is specific enough that a future CI log reader (when CI is wired up per the deferred entry) can identify and remediate the missing precondition

### Requirement: helper_chown_files Primitive Contract

`helper_chown_files(host_user, parent, files, owner_uid, owner_gid, mode, machinectl_auth)` SHALL run a single helper container that, for each file in `files`, executes `cp <parent>/<file> /tmp/<file> && chmod <mode> /tmp/<file> && chown <in_container_uid>:<in_container_gid> /tmp/<file> && mv /tmp/<file> <parent>/<file>`. The order of operations within the loop is significant on two axes: (i) the **chmod precedes the chown** because, post-userns-translation, the chown lands the file on a non-root in-container uid, and the helper's cap-add baseline (CHOWN + DAC_OVERRIDE, no CAP_FOWNER) means a subsequent chmod by in-container root would trip EPERM on a foreign-owned file; (ii) the **chown precedes the mv** so the destination's perceived ownership is the new owner from the moment of the rename. See design D7.

The `owner_uid` and `owner_gid` parameters SHALL be interpreted as **host-absolute** values (the orchestrator's plan-time semantics). Before interpolation into the chown argv issued inside the container, the host-absolute values SHALL be translated to their in-container equivalents via `in_container_uid_for_host_uid(owner_uid, host_user)` and `in_container_gid_for_host_gid(owner_gid, host_user)` respectively. This translation crosses the userns boundary: the daemon's userns map then resolves the in-container values back to the host-absolute target on the underlying filesystem. The helper SHALL NOT pass host-absolute values directly into the chown argv (the in-container kernel rejects them with EINVAL when they fall outside the userns map), and SHALL NOT add `--userns=host` to the docker run command.

#### Scenario: Multiple files batched in one container
- **WHEN** `helper_chown_files` is called with N files sharing a parent
- **THEN** ONE docker run invocation handles all N files via a single sh -c loop (batching reduces helper-container startup overhead)

#### Scenario: Empty file list is a no-op
- **WHEN** `helper_chown_files` is called with an empty `files` list
- **THEN** no docker run is executed; the function returns immediately

#### Scenario: Idempotent on already-correct files
- **WHEN** `helper_chown_files` is called against files that are already at the target owner/mode
- **THEN** the operation completes successfully (cp/chmod/chown/mv are idempotent on already-correct values)

#### Scenario: Host-absolute owner_uid is translated before chown interpolation
- **WHEN** `helper_chown_files` is invoked with `owner_uid = 166535`, `owner_gid = 166535`, and `host_user = "claude-sandbox"` (where `/etc/subuid` and `/etc/subgid` have `claude-sandbox:165536:65536`)
- **THEN** the chown argv inside the container reads `chown 1000:1000` (= host-absolute 166535 translated to in-container 1000); the resulting on-disk ownership after the helper completes is host-absolute uid/gid 166535/166535

#### Scenario: Out-of-range owner_uid raises before docker run
- **WHEN** `helper_chown_files` is invoked with an `owner_uid` outside the daemon user's subuid range (e.g., the daemon user's primary uid, or a value below the range)
- **THEN** `in_container_uid_for_host_uid` raises `SubuidOutOfRangeError` (or `NoSubuidRangeError`) before any docker run is issued; no helper container is launched and no file is mutated

#### Scenario: --userns=host is never added to the docker run command
- **WHEN** `helper_chown_files` constructs its docker run command via `_hardened_docker_run`
- **THEN** the command does NOT contain `--userns=host`; the helper inherits the daemon's default rootless userns map

### Requirement: helper_mkdir_chown_dirs Primitive Contract

`helper_mkdir_chown_dirs(host_user, parent, leaves, owner_uid, owner_gid, machinectl_auth)` SHALL run a single helper container that, for each leaf in `leaves`, executes `mkdir -p <parent>/<leaf> && chown <in_container_uid>:<in_container_gid> <parent>/<leaf>`. The function SHALL NOT chmod the leaf — chmod on a directory collapses the ACL mask and clamps inherited named entries (e.g., `u:dev:rwx`) to `r-x`.

The `owner_uid` and `owner_gid` parameters SHALL be interpreted as **host-absolute** values (the orchestrator's plan-time semantics). Before interpolation into the chown argv issued inside the container, the host-absolute values SHALL be translated to their in-container equivalents via `in_container_uid_for_host_uid(owner_uid, host_user)` and `in_container_gid_for_host_gid(owner_gid, host_user)` respectively. The helper SHALL NOT pass host-absolute values directly into the chown argv, and SHALL NOT add `--userns=host` to the docker run command.

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

#### Scenario: Host-absolute owner_uid is translated before chown interpolation
- **WHEN** `helper_mkdir_chown_dirs` is invoked with `owner_uid = 166535`, `owner_gid = 166535`, and `host_user = "claude-sandbox"` (where `/etc/subuid` and `/etc/subgid` have `claude-sandbox:165536:65536`)
- **THEN** the chown argv inside the container reads `chown 1000:1000` for each leaf; the resulting on-disk ownership after the helper completes is host-absolute uid/gid 166535/166535

#### Scenario: Out-of-range owner_uid raises before docker run
- **WHEN** `helper_mkdir_chown_dirs` is invoked with an `owner_uid` outside the daemon user's subuid range
- **THEN** `in_container_uid_for_host_uid` raises `SubuidOutOfRangeError` (or `NoSubuidRangeError`) before any docker run is issued; no helper container is launched and no directory is created

#### Scenario: --userns=host is never added to the docker run command
- **WHEN** `helper_mkdir_chown_dirs` constructs its docker run command via `_hardened_docker_run`
- **THEN** the command does NOT contain `--userns=host`; the helper inherits the daemon's default rootless userns map

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
