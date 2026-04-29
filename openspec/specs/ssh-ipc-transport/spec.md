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
The system SHALL provide a Jinja2-rendered `sshd_config` template with security-hardened defaults. The config SHALL bind to the IPC-specific IP address, not `0.0.0.0`.

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

### Requirement: SSH Keypair Generation
The orchestrator SHALL generate two Ed25519 keypairs per instance during credential provisioning using the `cryptography` Python library.

#### Scenario: Auth keypair generated
- **WHEN** `_phase_credentials()` runs for a new instance
- **THEN** the system generates an Ed25519 auth keypair and writes: the private key to `secrets/ipc_ssh_key` (PEM, mode 0600) and the public key to `secrets/authorized_keys` (OpenSSH format)

#### Scenario: Host keypair generated
- **WHEN** `_phase_credentials()` runs for a new instance
- **THEN** the system generates an Ed25519 host keypair and writes: the private key to `secrets/ipc_host_key` (PEM, mode 0600) and a `known_hosts` entry to `secrets/ipc_known_hosts` (OpenSSH format with `{{ core_ipc_ip }}` as the hostname)

#### Scenario: Keypair generation is idempotent
- **WHEN** `_phase_credentials()` runs and SSH key files already exist
- **THEN** existing keys are NOT overwritten (same idempotency pattern as proxy credentials)

#### Scenario: cryptography library used for key generation
- **WHEN** the SSH keypair generation code is inspected
- **THEN** it uses `cryptography.hazmat.primitives.asymmetric.ed25519` (not `ssh-keygen` subprocess)

#### Scenario: Secrets directory in INSTANCE_SUBDIRS
- **WHEN** `INSTANCE_SUBDIRS` in `scaffold.py` is inspected
- **THEN** it contains `"secrets"` so that `create_instance_dirs()` creates `sandboxes/<id>/secrets/` during `sandbox init`

### Requirement: SSH Credential Mounts
The rendered `compose.yml` SHALL mount SSH credentials into core and admin containers with appropriate permissions.

#### Scenario: Core receives host private key as Docker secret
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it includes a `secrets` entry mounting `ipc_host_key` at `/run/secrets/ipc_host_key` with `uid: '0'`, `gid: '0'`, `mode: 0600`

#### Scenario: Core receives authorized_keys
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it includes a volume mount `{{ instance_dir }}/secrets/authorized_keys:/run/secrets/authorized_keys:ro`

#### Scenario: Admin receives auth private key as Docker secret
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it includes a `secrets` entry mounting `ipc_ssh_key` at `/run/secrets/ipc_ssh_key` with `uid: '1000'`, `gid: '1000'`, `mode: 0600`

#### Scenario: Admin receives known_hosts
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it includes a volume mount `{{ instance_dir }}/secrets/ipc_known_hosts:/run/secrets/ipc_known_hosts:ro`

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

### Requirement: Core Dockerfile USER root
The core container Dockerfile SHALL set `USER root` for the final stage entrypoint. sshd requires root to perform L1 UID separation (`setuid(agent)` per session).

#### Scenario: Dockerfile final USER is root
- **WHEN** the core Dockerfile's final stage is inspected
- **THEN** the last `USER` directive before `ENTRYPOINT` is `USER root`

#### Scenario: Agent-owned files retain correct ownership
- **WHEN** the core container starts as root
- **THEN** files created in earlier Dockerfile layers as `USER agent` retain `agent:agent` ownership

### Requirement: Core Container sshd Runtime Directory
The core container SHALL have a tmpfs mount at `/run` for the sshd PID file. The entrypoint SHALL create `/run/sshd` before executing sshd.

#### Scenario: /run tmpfs mount present
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** the `tmpfs` block includes `/run`

#### Scenario: Entrypoint creates sshd directory
- **WHEN** `entrypoint.sh` is inspected
- **THEN** it includes `mkdir -p /run/sshd` before `exec /usr/sbin/sshd -D -e`
