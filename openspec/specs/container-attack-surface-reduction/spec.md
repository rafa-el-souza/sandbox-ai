## Purpose

This specification defines the binary attack surface reduction, toolchain relocation, and read-only root filesystem enforcement for the core, admin, and firecrawl containers.

## Requirements

### Requirement: Runtime Binary Pruning
The core container's final image SHALL NOT contain `curl`, `wget`, `unzip`, or `socat`. These binaries SHALL be available only in a build-time stage.

#### Scenario: curl absent from core runtime
- **WHEN** the core container executes `which curl`
- **THEN** the command returns "not found"

#### Scenario: wget absent from core runtime
- **WHEN** the core container executes `which wget`
- **THEN** the command returns "not found"

#### Scenario: unzip absent from core runtime
- **WHEN** the core container executes `which unzip`
- **THEN** the command returns "not found"

#### Scenario: socat absent from core runtime
- **WHEN** the core container executes `which socat`
- **THEN** the command returns "not found"

#### Scenario: Runtime packages preserved
- **WHEN** the core container is inspected
- **THEN** `bash`, `git`, `jq`, `python3`, `py3-pip`, `coreutils`, `file`, `gzip`, `tzdata`, `ca-certificates`, and `nano` are all available

### Requirement: Firecrawl Binary Pruning
The firecrawl container SHALL NOT contain `socat` in its final image.

#### Scenario: socat absent from firecrawl runtime
- **WHEN** the firecrawl container executes `which socat`
- **THEN** the command returns "not found"

### Requirement: Toolchain Relocation to /usr/local/
All image-layer toolchains SHALL be installed under `/usr/local/` instead of `$HOME`. After relocation, `$HOME` SHALL contain zero image-layer data, eliminating tmpfs shadowing risk. This requirement applies to the core container only; admin no longer carries any toolchain post-reframe.

#### Scenario: NVM relocated to /usr/local/nvm
- **WHEN** the core container is inspected
- **THEN** `NVM_DIR` is set to `/usr/local/nvm` and `node --version` returns the expected version

#### Scenario: Go workspace relocated to /usr/local/go-workspace
- **WHEN** the core container is inspected
- **THEN** `GOPATH` is set to `/usr/local/go-workspace` and `gopls version` is available

#### Scenario: npm-ts relocated to /usr/local/npm-ts
- **WHEN** the core container is inspected
- **THEN** `tsserver` and `tsc` are available from `/usr/local/npm-ts/bin/`

#### Scenario: npm-python relocated to /usr/local/npm-python
- **WHEN** the core container is inspected
- **THEN** `pyright` is available from `/usr/local/npm-python/bin/`

#### Scenario: Claude CLI relocated to /usr/local/bin/claude
- **WHEN** the core container is inspected
- **THEN** `claude --version` is available from `/usr/local/bin/claude`

#### Scenario: rust-analyzer relocated to /usr/local/bin
- **WHEN** the core container is inspected
- **THEN** `rust-analyzer --version` is available from `/usr/local/bin/rust-analyzer`

### Requirement: Toolchain Write Protection
With `read_only: true` and toolchains under `/usr/local/`, user-space package installation commands that write to image-layer paths SHALL fail with EROFS.

#### Scenario: pip install --user fails on read-only image
- **WHEN** the core container executes `pip install --user <package>`
- **THEN** the command fails with an EROFS error (Read-only file system)

#### Scenario: npm install -g fails on read-only image
- **WHEN** the core container executes `npm install -g <package>`
- **THEN** the command fails with an EROFS error

#### Scenario: go install fails on read-only GOPATH
- **WHEN** the core container executes `go install <package>`
- **THEN** the command fails with an EROFS error

### Requirement: Multi-Stage Dockerfile Build
The core Dockerfile SHALL use a multi-stage build separating build-time tools from runtime packages. Build-time network tools (`curl`, `wget`, `unzip`) SHALL be confined to the build stage.

#### Scenario: Build stage has network tools
- **WHEN** the core Dockerfile's `base` (build) stage is inspected
- **THEN** it installs `curl`, `wget`, and `unzip`

#### Scenario: Runtime stage excludes network tools
- **WHEN** the core Dockerfile's `runtime` stage is inspected
- **THEN** it does NOT install `curl`, `wget`, `unzip`, or `socat`

#### Scenario: Final stage copies from build stages
- **WHEN** the core Dockerfile's final stage is inspected
- **THEN** it uses `COPY --from=` directives to copy built artifacts (NVM, toolchains) into `/usr/local/`

### Requirement: Core Writable Path tmpfs Mounts
The core service in `compose.yml` SHALL include tmpfs mounts for all runtime-writable paths not already covered by existing volume or bind mounts. Admin has no equivalent requirement because the reframed admin has no writable paths at all (zero tmpfs, zero volumes — strictly stronger than `tmpfs+noexec` per design.md D6).

#### Scenario: Core ~/.config tmpfs mount
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the `tmpfs` block includes `/home/agent/.config`

#### Scenario: Core /run tmpfs mount
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the `tmpfs` block includes `/run`

### Requirement: Parallel Builder Stage USER Context Correctness
The core Dockerfile's parallel builder stages (`branch-typescript`, `branch-python`, `branch-claude`) SHALL use `USER root` for filesystem creation operations (`mkdir`, `chown`) on root-owned paths, then switch to `USER ${USERNAME}` before any network-facing tool installation. This ensures builder stages execute filesystem writes with correct privileges.

#### Scenario: branch-typescript uses root for staging mkdir
- **WHEN** the `branch-typescript` stage in `Dockerfile.core.wolfi` is inspected
- **THEN** `USER root` precedes `RUN mkdir -p /staging/...` and `USER ${USERNAME}` precedes the `npm install` command

#### Scenario: branch-python uses root for staging mkdir
- **WHEN** the `branch-python` stage in `Dockerfile.core.wolfi` is inspected
- **THEN** `USER root` precedes `RUN mkdir -p /staging/...` and `USER ${USERNAME}` precedes the `curl` and `npm install` commands

#### Scenario: branch-claude uses root for staging mkdir
- **WHEN** the `branch-claude` stage in `Dockerfile.core.wolfi` is inspected
- **THEN** `USER root` precedes `RUN mkdir -p /staging/...` and `USER ${USERNAME}` precedes the `npm install` and `curl` commands

#### Scenario: No unprivileged writes to root-owned paths in builder stages
- **WHEN** the `Dockerfile.core.wolfi` template is inspected
- **THEN** no `RUN mkdir`, `RUN chmod`, or `RUN chown` targeting `/staging`, `/usr`, `/etc`, `/var`, `/run`, or `/opt` occurs under an unprivileged `USER` context

### Requirement: Claude CLI Installer Path
The core Dockerfile SHALL reference `${HOME_DIR}/.local/bin/claude` as the source path for copying the Claude CLI binary to `/staging/usr/local/bin/claude`. The legacy path `${HOME_DIR}/.claude/local/claude` SHALL NOT be used.

#### Scenario: Claude binary copied from .local/bin
- **WHEN** the `branch-claude` stage in `Dockerfile.core.wolfi` is inspected
- **THEN** the `cp` command sources from `${HOME_DIR}/.local/bin/claude`

#### Scenario: Legacy claude path is absent
- **WHEN** the `Dockerfile.core.wolfi` template source is inspected
- **THEN** it does NOT contain `${HOME_DIR}/.claude/local/claude`

### Requirement: Dockerfile USER Context Structural Lint
The system SHALL include a unit test that scans rendered Dockerfile templates for filesystem write operations (`mkdir`, `chmod`, `chown`, `touch`, `cp`) targeting root-owned paths while the active `USER` is unprivileged. The lint SHALL use a state machine that tracks `USER` and `FROM` directives.

#### Scenario: Lint passes on correctly-structured Dockerfiles
- **WHEN** the structural lint runs against the current `Dockerfile.core.wolfi` and `Dockerfile.admin`
- **THEN** zero violations are reported

#### Scenario: Lint detects unprivileged mkdir on root-owned path
- **WHEN** the lint encounters `RUN mkdir -p /staging/...` under `USER agent` (not root)
- **THEN** it reports a violation identifying the line, the active user, and the target path

#### Scenario: Lint handles FROM resets
- **WHEN** the lint encounters `FROM ... AS branch-typescript`
- **THEN** it resets the tracked `USER` to `root` (Dockerfile default)

#### Scenario: Lint handles line continuations
- **WHEN** a `RUN` command spans multiple lines with `\` continuations
- **THEN** the lint joins them into a single logical line before scanning for filesystem operations

### Requirement: Admin Container Image Profile

The admin container image SHALL be `FROM scratch` with a single static binary `/fwd` at `/`. The image SHALL set `USER 65534:65534` explicitly (numeric uid:gid for nobody:nogroup; required because `scratch` has no `/etc/passwd`). The image SHALL set `ENTRYPOINT ["/fwd"]`.

The image SHALL be built via a multi-stage Dockerfile: a `golang:1.23-alpine` (or pinned-digest equivalent) builder stage compiles `templates/docker/admin/fwd.go` with `CGO_ENABLED=0 go build -ldflags="-s -w"`, and a `FROM scratch` final stage `COPY`s the resulting binary as `/fwd`.

`fwd.go` SHALL ship in the wheel under `templates/docker/admin/`. Compilation happens at `sandbox start` build time — the wheel ships only Go source, never a precompiled binary, so the wheel stays pure Python and architecture-agnostic.

The `/fwd` binary SHALL implement two argv modes:

- **Zero args** → PID-1 idle. Blocks on a `signal.Notify` channel for `SIGTERM`/`SIGINT`. A bare `select{}` triggers Go's runtime deadlock detector and aborts with a fatal error; the signal-channel pattern defeats the detector and gives clean SIGTERM-driven exit.
- **One arg `<host:port>`** → byte-pipe forwarder. `net.Dial("tcp", arg)`; bidirectional `io.Copy` between stdin↔conn and conn↔stdout; exits when either direction EOFs.

#### Scenario: Admin image final stage is FROM scratch
- **WHEN** `templates/docker/admin/Dockerfile.admin` is inspected
- **THEN** the final stage directive is `FROM scratch` (no other base)

#### Scenario: Admin Dockerfile uses multi-stage Go build
- **WHEN** `templates/docker/admin/Dockerfile.admin` is inspected
- **THEN** it contains a builder stage `FROM golang:1.23-alpine AS build` (or equivalent pinned-digest tag) that runs `CGO_ENABLED=0 go build -ldflags="-s -w"` against `fwd.go`, and a final `FROM scratch` stage with `COPY --from=build /fwd /fwd`

#### Scenario: Admin image USER is numeric nobody:nogroup
- **WHEN** `templates/docker/admin/Dockerfile.admin` is inspected
- **THEN** the final stage contains `USER 65534:65534` (numeric — `scratch` has no `/etc/passwd` to resolve names)

#### Scenario: Admin image ENTRYPOINT is /fwd
- **WHEN** `templates/docker/admin/Dockerfile.admin` is inspected
- **THEN** the final stage contains `ENTRYPOINT ["/fwd"]`

#### Scenario: fwd.go ships in the wheel
- **WHEN** the installed wheel's `templates/docker/admin/` directory is inspected
- **THEN** it contains the file `fwd.go`

#### Scenario: Wheel ships no precompiled fwd binary
- **WHEN** the installed wheel's `templates/docker/admin/` directory is inspected
- **THEN** it does NOT contain a precompiled binary file named `fwd` (compilation happens at `sandbox start` build time)

#### Scenario: fwd.go idle mode uses signal channel, not bare select
- **WHEN** `templates/docker/admin/fwd.go` is inspected
- **THEN** the zero-args branch blocks on a `signal.Notify` channel receiving `SIGTERM` and `SIGINT`, and does NOT contain a bare `select{}` statement (a bare `select{}` triggers Go's deadlock detector); a source comment SHALL document the deadlock-detector reasoning so future "simplify" PRs do not regress the idiom

#### Scenario: fwd.go forwarder mode dials TCP and io.Copy bidirectionally
- **WHEN** `templates/docker/admin/fwd.go` is inspected
- **THEN** the one-arg branch calls `net.Dial("tcp", os.Args[1])` and runs two `io.Copy` calls (stdin↔conn and conn↔stdout) and exits when either direction EOFs

### Requirement: Admin Image Contains Only `/fwd`

The reframed admin image SHALL contain exactly one executable file (`/fwd`) and the irreducible filesystem entries any container needs (e.g., `/proc`, `/sys`, `/dev` mounted by the runtime). The image SHALL NOT contain any shell, coreutils, network client, package manager, SSH client, source-tree editor, or any other userland tool. This is the empirically-validated "what's absent" surface from the admin-reframe validation rounds.

#### Scenario: socat absent from admin runtime
- **WHEN** the admin container is inspected for `/usr/bin/socat`, `/usr/sbin/socat`, or any path containing `socat`
- **THEN** no such file exists (admin contains exactly `/fwd` by construction)

#### Scenario: No shell in admin
- **WHEN** the admin container is inspected for `/bin/sh`, `/bin/bash`, `/bin/ash`, `/bin/dash`, `/usr/bin/zsh`, or `/bin/busybox`
- **THEN** none of these paths exist

#### Scenario: No SSH client tooling in admin
- **WHEN** the admin container is inspected for `/usr/bin/ssh`, `/usr/bin/scp`, `/usr/bin/sftp`, `/usr/bin/ssh-keygen`, or `/usr/bin/ssh-add`
- **THEN** none of these paths exist

#### Scenario: No coreutils in admin
- **WHEN** the admin container is inspected for `/usr/bin/cat`, `/usr/bin/ls`, `/bin/ls`, `/usr/bin/cp`, `/usr/bin/mv`, `/usr/bin/rm`, or `/usr/bin/echo`
- **THEN** none of these paths exist

#### Scenario: No network clients in admin
- **WHEN** the admin container is inspected for `/usr/bin/nc`, `/usr/bin/curl`, `/usr/bin/wget`, or `/usr/bin/dig`
- **THEN** none of these paths exist

#### Scenario: No IPC SSH credentials inside admin
- **WHEN** the admin container is inspected for `/run/secrets/ipc_ssh_key` or `/run/secrets/ipc_known_hosts`
- **THEN** neither path exists (admin no longer mounts the IPC keypair; the host's ssh client owns these files dev:dev mode 0600)

#### Scenario: Admin image has exactly one executable
- **WHEN** the admin image's filesystem layers are enumerated
- **THEN** the only executable file added by the image is `/fwd`

