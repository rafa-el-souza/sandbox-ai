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
The rendered `compose.yml` SHALL mount SSH credentials into the core container via bind-mount volumes. Docker Compose `secrets:` syntax SHALL NOT be used — its `uid`, `gid`, `mode` directives are silently ignored under rootless Docker. A top-level `secrets:` block SHALL NOT exist in the compose template. The admin container SHALL NOT receive any SSH-credential bind-mounts (the host's ssh client reads `ipc_ssh_key` and `ipc_known_hosts` directly from the host filesystem; admin holds zero credentials post-reframe).

#### Scenario: Core receives host private key as bind-mount
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it includes a volume mount `{{ instance_dir }}/secrets/ipc_host_key:/run/secrets/ipc_host_key:ro`

#### Scenario: Core does not use Docker secrets for host key
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it does NOT include a `secrets:` entry for `ipc_host_key`

#### Scenario: Core receives authorized_keys
- **WHEN** the rendered `compose.yml` is inspected for the core service
- **THEN** it includes a volume mount `{{ instance_dir }}/secrets/authorized_keys:/run/secrets/authorized_keys:ro`

#### Scenario: No top-level secrets block
- **WHEN** the `compose.yml` template source is inspected
- **THEN** it does NOT contain a top-level `secrets:` block

### Requirement: IPC Client Credential Ownership

The orchestrator SHALL materialize `ipc_ssh_key` and `ipc_known_hosts` as **dev-owned** files (uid matches the current operator running the `sandbox` CLI) with mode `0600` and **no extended ACL entries**. This SHALL be achieved by **excluding** these two files from `cli.main.RO_FILE_RECIPES` so that the consumer-uid-0-chown helper-cp recipe does NOT touch them. The host's ssh client (invoked by `sandbox attach`) reads these files directly via `-i <inst>/secrets/ipc_ssh_key` and `-o UserKnownHostsFile=<inst>/secrets/ipc_known_hosts`.

Per design.md decision D3 (Fix B'), the **server-side** credentials (`authorized_keys` and `ipc_host_key`) SHALL keep their existing `consumer-uid-0-chown` lifecycle (mode 0600, owned by the host subuid that maps to in-container uid 0 — typically `166535:166535` for `claude-sandbox`); those are read by core's in-container sshd, not by the host.

#### Scenario: ipc_ssh_key dev-owned at mode 0600
- **WHEN** `_phase_credentials` has run and `<inst>/secrets/ipc_ssh_key` is inspected
- **THEN** the file is owned by the current operator's uid:gid (the dev user invoking the CLI) with mode `0600`

#### Scenario: ipc_known_hosts dev-owned at mode 0600
- **WHEN** `_phase_credentials` has run and `<inst>/secrets/ipc_known_hosts` is inspected
- **THEN** the file is owned by the current operator's uid:gid with mode `0600`

#### Scenario: No extended ACL entries on client-side credentials
- **WHEN** `getfacl <inst>/secrets/ipc_ssh_key` and `getfacl <inst>/secrets/ipc_known_hosts` are inspected
- **THEN** neither file has any named-user or named-group ACL entries beyond the standard POSIX `user::rw-`, `group::---`, `other::---` triple

#### Scenario: ipc_ssh_key excluded from RO_FILE_RECIPES
- **WHEN** `cli.main.RO_FILE_RECIPES` is inspected
- **THEN** it does NOT contain an entry whose source basename is `ipc_ssh_key`

#### Scenario: ipc_known_hosts excluded from RO_FILE_RECIPES
- **WHEN** `cli.main.RO_FILE_RECIPES` is inspected
- **THEN** it does NOT contain an entry whose source basename is `ipc_known_hosts`

#### Scenario: Server-side credentials retain consumer-uid-0-chown recipe
- **WHEN** `cli.main.RO_FILE_RECIPES` is inspected
- **THEN** it still contains entries for `authorized_keys` and `ipc_host_key`, both targeting the consumer's uid 0 mapping with mode 0600

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
