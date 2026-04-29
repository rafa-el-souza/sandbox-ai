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

### Requirement: Admin Binary Pruning
The admin container SHALL NOT contain `socat` in its final image.

#### Scenario: socat absent from admin runtime
- **WHEN** the admin container executes `which socat`
- **THEN** the command returns "not found"

### Requirement: Firecrawl Binary Pruning
The firecrawl container SHALL NOT contain `socat` in its final image.

#### Scenario: socat absent from firecrawl runtime
- **WHEN** the firecrawl container executes `which socat`
- **THEN** the command returns "not found"

### Requirement: Toolchain Relocation to /usr/local/
All image-layer toolchains SHALL be installed under `/usr/local/` instead of `$HOME`. After relocation, `$HOME` SHALL contain zero image-layer data, eliminating tmpfs shadowing risk.

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

#### Scenario: Admin NVM relocated
- **WHEN** the admin container is inspected
- **THEN** `NVM_DIR` is set to `/usr/local/nvm` and `node --version` returns the expected version

#### Scenario: Admin tmux plugins relocated
- **WHEN** the admin container is inspected
- **THEN** tmux plugins are installed under `/usr/local/tmux-plugins/`

#### Scenario: Admin zsh-completions relocated
- **WHEN** the admin container is inspected
- **THEN** zsh completions are under `/usr/local/share/zsh-completions/`

#### Scenario: Admin entrypoint relocated
- **WHEN** the admin Dockerfile is inspected
- **THEN** the entrypoint script is at `/usr/local/bin/entrypoint.sh`

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
The core service in `compose.yml` SHALL include tmpfs mounts for all runtime-writable paths not already covered by existing volume or bind mounts.

#### Scenario: Core ~/.config tmpfs mount
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the `tmpfs` block includes `/home/agent/.config`

#### Scenario: Core /run tmpfs mount
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the `tmpfs` block includes `/run`

### Requirement: Admin Writable Path tmpfs Mounts
The admin service in `compose.yml` SHALL include tmpfs mounts for all runtime-writable paths not already covered by existing volume or bind mounts.

#### Scenario: Admin ~/.cache tmpfs mount
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** the `tmpfs` block includes `/home/human/.cache`

#### Scenario: Admin ~/.config tmpfs mount
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** the `tmpfs` block includes `/home/human/.config`

#### Scenario: Admin ~/.zsh_sessions tmpfs mount
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** the `tmpfs` block includes `/home/human/.zsh_sessions`

#### Scenario: Starship config bind-mount layers over tmpfs
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** the `starship.toml` bind-mount at `/home/human/.config/starship.toml:ro` coexists with the `/home/human/.config` tmpfs mount (Docker processes tmpfs before bind-mounts)

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

### Requirement: Admin Dockerfile Entrypoint Permission Context
The admin Dockerfile SHALL use `USER root` before the `COPY --chown=root:root entrypoint.sh` and `chmod a+x` operations, then restore `USER ${USERNAME}` before the `ENTRYPOINT` directive.

#### Scenario: Entrypoint copy runs as root
- **WHEN** the `Dockerfile.admin.debian` is inspected
- **THEN** `USER root` appears before `COPY --chown=root:root entrypoint.sh /usr/local/bin/entrypoint.sh` and `RUN chmod a+x /usr/local/bin/entrypoint.sh`

#### Scenario: Final USER is unprivileged before ENTRYPOINT
- **WHEN** the `Dockerfile.admin.debian` is inspected
- **THEN** `USER ${USERNAME}` appears between the `chmod` and the `ENTRYPOINT` directive

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
- **WHEN** the structural lint runs against the current `Dockerfile.core.wolfi` and `Dockerfile.admin.debian`
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

