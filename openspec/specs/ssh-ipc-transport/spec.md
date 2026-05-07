## Purpose

This specification defines the SSH-based IPC transport between the admin (human) and core (agent) containers, replacing the legacy socat+UDS mechanism with stock OpenSSH over a dedicated TCP subnet.

## Requirements

### Requirement: SSH Server in Core Container
The core container SHALL run `sshd -D -e` as its primary entrypoint process. The `-D` flag SHALL prevent daemonization (container entrypoint pattern). The `-e` flag SHALL send logs to stderr (captured by Docker logging driver).

#### Scenario: sshd is the core entrypoint
- **WHEN** the core container starts
- **THEN** the entrypoint executes `/usr/sbin/sshd -D -e` as PID 1 (via `exec`)

#### Scenario: socat removed from core entrypoint
- **WHEN** the core container's `entrypoint.sh` is inspected
- **THEN** it does NOT contain any `socat` invocation

#### Scenario: openssh-server installed in core image
- **WHEN** the core container is inspected
- **THEN** `sshd -V` (or `/usr/sbin/sshd`) is available and `socat` is NOT installed

#### Scenario: Core service has no command override
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the core service does NOT contain a `command:` directive (the legacy `command: ["sleep", "infinity"]` is removed — sshd entrypoint replaces it)

### Requirement: Hardened sshd_config Template
The system SHALL provide a Jinja2-rendered `sshd_config` template with security-hardened defaults. The config SHALL bind to the IPC-specific IP address, not `0.0.0.0`. The config SHALL disable PID file creation for non-root operation.

#### Scenario: sshd binds to IPC IP only
- **WHEN** the rendered `sshd_config` is inspected
- **THEN** `ListenAddress` is set to the core container's `ipc_net` IP address (not `0.0.0.0`)

#### Scenario: sshd listens on port 9999
- **WHEN** the rendered `sshd_config` is inspected
- **THEN** `Port` is set to `9999`

#### Scenario: Password authentication disabled
- **WHEN** the rendered `sshd_config` is inspected
- **THEN** `PasswordAuthentication no` and `KbdInteractiveAuthentication no` are set

#### Scenario: Only agent user allowed
- **WHEN** the rendered `sshd_config` is inspected
- **THEN** `AllowUsers agent` is set and `PermitRootLogin no` is set

#### Scenario: Forwarding disabled
- **WHEN** the rendered `sshd_config` is inspected
- **THEN** `X11Forwarding no`, `AllowAgentForwarding no`, `AllowTcpForwarding no`, and `PermitTunnel no` are all set

#### Scenario: Host key from orchestrator-managed secret
- **WHEN** the rendered `sshd_config` is inspected
- **THEN** `HostKey` is set to `/run/secrets/ipc_host_key`

#### Scenario: Authorized keys from orchestrator-managed file
- **WHEN** the rendered `sshd_config` is inspected
- **THEN** `AuthorizedKeysFile` is set to `/run/secrets/authorized_keys`

#### Scenario: Warmup environment variable accepted
- **WHEN** the rendered `sshd_config` is inspected
- **THEN** `AcceptEnv SANDBOX_WARMUP_PROMPT` is set

#### Scenario: Session limits configured
- **WHEN** the rendered `sshd_config` is inspected
- **THEN** `MaxSessions 10` and `ClientAliveInterval 300` and `ClientAliveCountMax 2` are set

#### Scenario: PID file disabled
- **WHEN** the rendered `sshd_config` is inspected
- **THEN** `PidFile none` is set

### Requirement: SSH Keypair Generation
The orchestrator SHALL generate two Ed25519 keypairs per instance during credential provisioning using the `cryptography` Python library. Keys land under `<sandbox_ai_home()>/instances/<inst>/secrets/`.

#### Scenario: Auth keypair generated
- **WHEN** `_phase_credentials()` runs for a new instance
- **THEN** the system generates an Ed25519 auth keypair and writes: the private key to `<sandbox_ai_home()>/instances/<inst>/secrets/ipc_ssh_key` (PEM, mode 0600) and the public key to `<sandbox_ai_home()>/instances/<inst>/secrets/authorized_keys` (OpenSSH format)

#### Scenario: Host keypair generated
- **WHEN** `_phase_credentials()` runs for a new instance
- **THEN** the system generates an Ed25519 host keypair and writes: the private key to `<sandbox_ai_home()>/instances/<inst>/secrets/ipc_host_key` (PEM, mode 0600) and a `known_hosts` entry to `<sandbox_ai_home()>/instances/<inst>/secrets/ipc_known_hosts` (OpenSSH format with `{{ core_ipc_ip }}` as the hostname)

#### Scenario: Keypair generation is idempotent
- **WHEN** `_phase_credentials()` runs and SSH key files already exist
- **THEN** existing keys are NOT overwritten (same idempotency pattern as proxy credentials)

#### Scenario: cryptography library used for key generation
- **WHEN** the SSH keypair generation code is inspected
- **THEN** it uses `cryptography.hazmat.primitives.asymmetric.ed25519` (not `ssh-keygen` subprocess)

#### Scenario: Secrets directory in INSTANCE_SUBDIRS
- **WHEN** `INSTANCE_SUBDIRS` in `scaffold.py` is inspected
- **THEN** it contains `"secrets"` so that `create_instance_dirs()` creates `<sandbox_ai_home()>/instances/<inst>/secrets/` during `sandbox init`

### Requirement: SSH Credential Mounts
The rendered `compose.yml` SHALL mount SSH credentials into core and admin containers via bind-mount volumes. Docker Compose `secrets:` syntax SHALL NOT be used — its `uid`, `gid`, `mode` directives are silently ignored under rootless Docker. A top-level `secrets:` block SHALL NOT exist in the compose template.

#### Scenario: Core receives host private key as bind-mount
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it includes a volume mount `{{ instance_dir }}/secrets/ipc_host_key:/run/secrets/ipc_host_key:ro`

#### Scenario: Core does not use Docker secrets for host key
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it does NOT include a `secrets:` entry for `ipc_host_key`

#### Scenario: Core receives authorized_keys
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it includes a volume mount `{{ instance_dir }}/secrets/authorized_keys:/run/secrets/authorized_keys:ro`

#### Scenario: Admin receives auth private key as bind-mount
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it includes a volume mount `{{ instance_dir }}/secrets/ipc_ssh_key:/run/secrets/ipc_ssh_key:ro`

#### Scenario: Admin does not use Docker secrets for SSH key
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it does NOT include a `secrets:` entry for `ipc_ssh_key`

#### Scenario: Admin receives known_hosts
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it includes a volume mount `{{ instance_dir }}/secrets/ipc_known_hosts:/run/secrets/ipc_known_hosts:ro`

#### Scenario: No top-level secrets block
- **WHEN** the `compose.yml` template source is inspected
- **THEN** it does NOT contain a top-level `secrets:` block

### Requirement: Admin SSH Client Configuration
The admin container SHALL connect to core via SSH with host key pinning and public key authentication. The admin container SHALL have `openssh-client` installed.

#### Scenario: Admin connects with strict host key checking
- **WHEN** the admin SSH client invocation is inspected
- **THEN** it includes `-o StrictHostKeyChecking=yes` and `-o UserKnownHostsFile=/run/secrets/ipc_known_hosts`

#### Scenario: Admin authenticates with private key
- **WHEN** the admin SSH client invocation is inspected
- **THEN** it includes `-i /run/secrets/ipc_ssh_key`

#### Scenario: Admin requests PTY allocation
- **WHEN** the admin SSH client invocation is inspected
- **THEN** it includes `-t` for PTY allocation

#### Scenario: openssh-client installed in admin image
- **WHEN** the admin container is inspected
- **THEN** `ssh -V` returns an OpenSSH version string

### Requirement: Warmup Prompt via SSH SendEnv
The admin container's warmup flow SHALL use SSH `SendEnv` to transport the warmup prompt to core, eliminating shell interpolation boundaries.

#### Scenario: Warmup prompt transported via SSH environment
- **WHEN** `SANDBOX_WARMUP_PROMPT` is set and the admin `.zshrc` warmup block executes
- **THEN** the SSH command includes `-o SendEnv=SANDBOX_WARMUP_PROMPT`

#### Scenario: Warmup invokes interactive claude session
- **WHEN** the warmup SSH command is inspected
- **THEN** the remote command is `claude --dangerously-skip-permissions "$SANDBOX_WARMUP_PROMPT"` (interactive mode, not `claude -p` one-shot mode)

#### Scenario: Warmup variable unset after use
- **WHEN** the warmup SSH session completes
- **THEN** `unset SANDBOX_WARMUP_PROMPT` is executed in the admin shell

### Requirement: Core Dockerfile USER for Non-Root sshd
The core container Dockerfile SHALL set `USER ${USERNAME}` for the final stage entrypoint. Non-root sshd (euid ≠ 0) skips privilege separation entirely — no `chroot()`, no `setuid()`, no `setgid()`. This eliminates the need for `CAP_SYS_CHROOT`, `CAP_SETUID`, and `CAP_SETGID`.

#### Scenario: Dockerfile final USER is agent
- **WHEN** the core Dockerfile's final stage is inspected
- **THEN** the last `USER` directive before `ENTRYPOINT` is `USER ${USERNAME}`

#### Scenario: Dockerfile does not end with USER root
- **WHEN** the core Dockerfile's final stage is inspected
- **THEN** it does NOT contain `USER root` as the last `USER` directive

#### Scenario: Agent-owned files retain correct ownership
- **WHEN** the core container starts as agent
- **THEN** files created in earlier Dockerfile layers as `USER agent` retain `agent:agent` ownership

### Requirement: sshd-session File Capability for PTY Allocation
The core Dockerfile SHALL grant `cap_chown+ep` (effective + permitted) on `/usr/lib/ssh/sshd-session` via `setcap`. This enables `pty_setowner()` to call `chown(2)` on allocated PTY devices without running sshd as root.

#### Scenario: setcap layer in Dockerfile
- **WHEN** the core Dockerfile is inspected
- **THEN** it contains a `RUN` directive that executes `setcap cap_chown+ep /usr/lib/ssh/sshd-session`

#### Scenario: libcap-utils installed and purged
- **WHEN** the core Dockerfile is inspected
- **THEN** the `setcap` RUN layer installs `libcap-utils` (or equivalent), runs `setcap`, and removes the package in the same layer to minimize image size

#### Scenario: File capability targets sshd-session not sshd
- **WHEN** the core Dockerfile is inspected
- **THEN** the `setcap` command targets `/usr/lib/ssh/sshd-session` (not `/usr/sbin/sshd`)

### Requirement: Core Container sshd Runtime Directory
The core container SHALL have a tmpfs mount at `/run` with mode `0755`. The entrypoint SHALL NOT create `/run/sshd` — with `PidFile none`, no PID file directory is needed.

#### Scenario: /run tmpfs mount present with mode
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the `tmpfs` block includes `/run` with `mode=0755`

#### Scenario: Entrypoint does not create sshd directory
- **WHEN** `entrypoint.sh` is inspected
- **THEN** it does NOT contain `mkdir -p /run/sshd`
