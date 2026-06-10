## Purpose

This specification defines the `sandbox attach` command lifecycle, governing warm state verification and PTY handover without re-hydration.
## Requirements
### Requirement: Optional Workspace Argument

`sandbox attach <inst> [<ws-name>]` SHALL accept an optional workspace name. When the instance has exactly one workspace (N=1), the argument MAY be omitted; the system defaults to that single workspace. When N>1 and the argument is omitted, the system SHALL exit with the list of available workspaces and exit code 1. When the argument is supplied, it MUST exist in `sandbox.toml [workspaces]`.

The default-when-N=1 is computed at attach time (no "default workspace" field stored in sandbox.toml or registry).

#### Scenario: N=1 omitted argument defaults
- **WHEN** `sandbox attach foo` is invoked and `foo` has exactly one workspace `main`
- **THEN** the system defaults to `main` and proceeds to PTY handover with cwd `/workspaces/main`

#### Scenario: N>1 omitted argument errors
- **WHEN** `sandbox attach foo` is invoked and `foo` has multiple workspaces
- **THEN** the CLI exits with: "Multiple workspaces in 'foo'. Pick one: <list>." and exit code 1

#### Scenario: Unknown workspace argument rejected
- **WHEN** `sandbox attach foo nonexistent` is invoked and `nonexistent` is not in `[workspaces]`
- **THEN** the CLI exits with a "workspace not found" error

#### Scenario: ssh remote command sets cwd to /workspaces/<ws>
- **WHEN** attach proceeds to PTY handover with workspace `<ws>` resolved
- **THEN** the ssh invocation includes a remote command of the form `'cd /workspaces/<ws> && exec bash -l'`, replacing the prior `docker exec -w /workspaces/<ws>` flag (per design D9)

### Requirement: Per-Instance Backup Lock Check

`sandbox attach <inst>` SHALL check `<inst>.backup.lock` and refuse fast if held. (Attach against a stopped-but-being-backed-up instance is rejected; the instance must complete the backup before attach can proceed — although in practice attach against a non-running instance fails the warm-state check first.)

#### Scenario: Concurrent backup blocks attach
- **WHEN** `sandbox attach <inst>` is invoked while `<inst>.backup.lock` is held
- **THEN** attach exits with a "Backup in progress" error

### Requirement: Warm State Verification Before Attach
The system SHALL verify that the sandbox's containers are running before attempting to hand the operator's terminal over to core via the ssh-through-admin path.

This check is the attach control-plane gate (C-010): in separate-user mode it crosses via the **framed** `compose-ps` dispatch op, and it MUST complete successfully **before** the streaming ProxyCommand crossing opens — the attach/no-attach decision rides a framed, nonce-bound verdict; the subsequent stream invocation carries no orchestrator-interpreted content (per `runtime-dispatcher`'s "Streaming Op Class").

#### Scenario: Running sandbox allows attach
- **WHEN** `sandbox attach <inst>` is invoked and `docker compose ps -q` returns non-empty output
- **THEN** the CLI proceeds to hand the terminal over to core via the canonical `tlog-rec → ssh → ProxyCommand → /fwd` invocation

#### Scenario: Stopped sandbox rejects attach
- **WHEN** `sandbox attach <inst>` is invoked and no containers are running for the instance
- **THEN** the CLI exits with: "Sandbox '<inst>' is not running. Use 'sandbox start <inst>' to launch."

### Requirement: PTY Handover Without Re-Hydration
The system SHALL hand the operator's terminal over to **core** (as `agent`) via a host-side ssh client wrapped in `tlog-rec`, using a `ProxyCommand` that **in separate-user mode** crosses the privilege boundary into the unprivileged docker user and runs the streaming dispatcher op `fwd` (per `runtime-dispatcher`'s "Streaming Op Class" / "fwd Op Wire Expansion"), which execs `docker exec -i <project>-admin-1 /fwd <core_ipc_ip>:9999` inside the sandbox-user session to forward stdio↔TCP to core's sshd. The handover SHALL NOT re-run hydration, credential generation, or IPAM allocation (the `fwd` wire expansion's IPAM read is the existing read-only ledger peek). In operator-rootless mode the handover uses the operator-local ProxyCommand path with no privilege-boundary crossing, per the "Attach ProxyCommand in Operator-Rootless Mode" requirement.

**In separate-user mode** the `ProxyCommand` SHALL be obtained from `core.dispatch`'s streaming entrypoint (the single sanctioned producer — per `runtime-dispatcher`'s "Streaming ProxyCommand Entrypoint"), with the crossing prefix selected by `machinectl_authentication`: **SUDO** → `sudo_pipe_cmd(docker_unprivileged_user)` (the privileged byte-pipe, authorized by the instance-agnostic per-op sudoers `Cmnd_Spec` — headless-capable, the F-060 fix); **POLKIT** → `pipe_cmd(docker_unprivileged_user)` (the unprivileged byte-pipe, polkit `manage-units`-authorized — requires an interactive polkit agent; headless POLKIT attach remains unsupported and documented as such). The crossed payload is identical in both modes: the bare `dispatch fwd <inst> --project <P> --ip <IP>`. A `machinectl_cmd` crossing is NEVER used for the ProxyCommand in either mode (the PTY's `onlcr` would corrupt the SSH binary stream).

The in-container working directory SHALL be selected via the ssh remote command suffix `'cd /workspaces/<ws> && exec bash -l'` (per design D9), not via a `docker exec -w` flag.

#### Scenario: Attach bypasses hydration
- **WHEN** `sandbox attach <inst>` completes its warm state check successfully
- **THEN** no Jinja2 templates are rendered, no `.htpasswd` is regenerated, and no IPAM ledger is mutated (the `fwd` wire expansion performs only the read-only IPAM peek)

#### Scenario: Terminal handed to core via ssh-through-admin (separate-user, SUDO mode)
- **WHEN** containers are confirmed running, `state.lock` is released, workspace `<ws>` is resolved, and `machinectl_authentication` is `"sudo"`
- **THEN** the system invokes `tlog-rec --writer=file --file-path=<host-side log path> -- ssh -F /dev/null -i <inst_dir>/secrets/ipc_ssh_key -o UserKnownHostsFile=<inst_dir>/secrets/ipc_known_hosts -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes -o IdentityAgent=none -o ForwardAgent=no -o ForwardX11=no -o ClearAllForwardings=yes -o PermitLocalCommand=no -o ProxyCommand="sudo systemd-run -q --pipe --uid=<docker_unprivileged_user> /bin/bash -c '/usr/local/libexec/sandbox-ai/dispatch fwd <inst> --project <project_name> --ip <core_ipc_ip>'" -p 9999 -t agent@<core_ipc_ip> 'cd /workspaces/<ws> && exec bash -l'` (the admin container `<project_name>-admin-1` is derived dispatcher-side from `--project`)

#### Scenario: Terminal handed to core via ssh-through-admin (separate-user, POLKIT mode)
- **WHEN** containers are confirmed running, `state.lock` is released, workspace `<ws>` is resolved, and `machinectl_authentication` is `"polkit"`
- **THEN** the system invokes the same argv as the SUDO-mode scenario except the `ProxyCommand` value carries no `sudo` prefix (`systemd-run -q --pipe --uid=… /bin/bash -c '…dispatch fwd …'`); the crossing is authorized by the polkit `manage-units` action and requires an interactive polkit agent

### Requirement: Per-User State Initialization Required
The `sandbox attach` command SHALL refuse to operate when the per-user state tree is not initialized. Initialization is signaled by the presence of `<sandbox_ai_user_home()>/state/instances.json`. On absence, the command SHALL exit with a clear error directing the operator to run `sandbox init`.

#### Scenario: Attach on uninitialized host
- **WHEN** `sandbox attach` is invoked and `<home>/state/instances.json` does not exist
- **THEN** the CLI exits with: "Error: per-user state not initialized at `<resolved-home>`. Run `sandbox init` first." and exit code 1

#### Scenario: Resolved home in error message
- **WHEN** the attach command above runs with `SANDBOX_AI_USER_HOME=/tmp/test-home` set
- **THEN** the error message contains `/tmp/test-home`

### Requirement: Host-Side `tlog-rec` Wrap

The `sandbox attach` invocation SHALL wrap the ssh client in `tlog-rec` (a host-side Red Hat tlog dependency) so that operator sessions are recorded to a structured JSON file. The recording is best-effort and operator-side: no orchestrator state depends on tlog being installed; if absent, `sandbox doctor` reports the missing dependency.

The tlog invocation form is `tlog-rec --writer=file --file-path=<path> -- ssh ...` where `<path>` lives under a host-side directory (e.g., `~/.sandbox-ai/sessions/<inst>/<UTC-timestamp>.log`).

#### Scenario: tlog-rec wraps the ssh invocation
- **WHEN** `sandbox attach <inst>` proceeds to PTY handover with workspace `<ws>` resolved
- **THEN** the executed argv begins with `tlog-rec --writer=file --file-path=<path> --` followed by the `ssh ...` command described in the "Terminal handed to core via ssh-through-admin" scenario; the ssh client is never invoked outside of a `tlog-rec` wrap

#### Scenario: Recording path lives under the host-side sessions directory
- **WHEN** `sandbox attach <inst>` constructs the `tlog-rec` argv
- **THEN** the `--file-path=<path>` value resolves to `~/.sandbox-ai/sessions/<inst>/<UTC-timestamp>.log` (or an equivalent host-side path under `<sandbox_ai_user_home()>/sessions/<inst>/`); the path is NOT inside any container, bind mount, or instance secrets directory

#### Scenario: sandbox doctor checks for tlog presence
- **WHEN** `sandbox doctor` runs on a host where `tlog-rec` is not installed
- **THEN** the doctor reports the missing `tlog` dependency with a remediation hint (e.g., `apt install tlog`, `dnf install tlog`, AUR `tlog`); `sandbox attach` continues to function only when `tlog-rec` is on `PATH`

### Requirement: Attach ProxyCommand in Operator-Rootless Mode

When `host_config.host.docker_execution_mode == operator-rootless`, `cli.attach` SHALL construct the SSH `ProxyCommand` with **no boundary-crossing prefix and no dispatcher indirection**: the ProxyCommand SHALL be the bare local `docker exec -i <project>-admin-1 /fwd <core_ipc_ip>:9999` — the streaming entrypoint's operator-rootless form (per `runtime-dispatcher`'s "Streaming ProxyCommand Entrypoint"). The surrounding handover structure (`tlog-rec -- ssh … -o ProxyCommand="…" -p 9999 -t agent@<core_ipc_ip>`) SHALL be unchanged; only the crossing differs by mode. In `separate-user` mode the ProxyCommand SHALL be the streaming entrypoint's crossed form — the `dispatch fwd <wire>` payload over `sudo_pipe_cmd` (SUDO) or `pipe_cmd` (POLKIT), per the "PTY Handover Without Re-Hydration" requirement.

#### Scenario: operator-rootless ProxyCommand has no crossing prefix

- **WHEN** `sandbox attach <inst>` runs with `docker_execution_mode == operator-rootless`
- **THEN** the `ProxyCommand` argument is `docker exec -i <project>-admin-1 /fwd <core_ipc_ip>:9999` with no `systemd-run`/`--uid=`/`sudo` prefix and no `/usr/local/libexec/sandbox-ai/dispatch` token

#### Scenario: separate-user ProxyCommand routes through the streaming entrypoint

- **WHEN** `sandbox attach <inst>` runs with `docker_execution_mode == separate-user`
- **THEN** the `ProxyCommand` argument is obtained from `core.dispatch`'s streaming entrypoint (never hand-assembled in `cli.attach`) and carries the bare `dispatch fwd <inst> --project <P> --ip <IP>` payload over the auth-mode-selected pipe prefix

### Requirement: Hardened ssh Client Invocation

The attach/handover ssh client SHALL be invoked with `-F /dev/null` — it MUST NOT read the operator's `~/.ssh/config` or the system `ssh_config`. Rationale: the session endpoint lives inside the untrusted sandbox plane; an operator config stanza such as `Host *` + `ForwardAgent yes` would hand the operator's ssh-agent to a compromised plane — a sandbox→operator privilege escalation, exactly the class the privilege boundary exists to prevent.

The invocation SHALL additionally pin, explicitly and in every execution mode (separate-user SUDO, separate-user POLKIT, operator-rootless):

- `-o ForwardAgent=no`, `-o ForwardX11=no`, `-o ClearAllForwardings=yes` — no forwarding channel of any kind into or out of the plane;
- `-o IdentitiesOnly=yes`, `-o IdentityAgent=none` — only the per-instance `-i <secrets>/ipc_ssh_key` identity is offered, and no agent is ever contacted;
- `-o PermitLocalCommand=no` — the remote end cannot trigger client-side command execution;
- the existing transport pins, now normative: `-o UserKnownHostsFile=<secrets>/ipc_known_hosts`, `-o StrictHostKeyChecking=yes` (pinned per-instance host key; no TOFU, no prompt).

These options are the **non-escalation guarantee** of the attach session: what attach promises is not a trustworthy view of the plane (unattainable — the endpoint is the plane's), but that the session cannot be used to reach back into the operator.

#### Scenario: ssh client ignores operator ssh_config
- **WHEN** any attach/handover ssh invocation is built (either execution mode, either auth mode)
- **THEN** the argv contains `-F /dev/null` before any `-o` option, so no `~/.ssh/config` or system `ssh_config` stanza (e.g. `Host *` + `ForwardAgent yes`) can alter the connection's security posture

#### Scenario: Forwarding and agent access pinned off
- **WHEN** the attach ssh argv is inspected
- **THEN** it contains `-o ForwardAgent=no`, `-o ForwardX11=no`, `-o ClearAllForwardings=yes`, `-o IdentitiesOnly=yes`, `-o IdentityAgent=none`, and `-o PermitLocalCommand=no`

#### Scenario: Host key remains pinned
- **WHEN** the attach ssh argv is inspected
- **THEN** it contains `-o UserKnownHostsFile=<inst_dir>/secrets/ipc_known_hosts` and `-o StrictHostKeyChecking=yes`, and the only identity source is `-i <inst_dir>/secrets/ipc_ssh_key`

### Requirement: Headless Separate-User Attach (SUDO mode)

In separate-user mode with `machinectl_authentication == "sudo"`, `sandbox attach` SHALL succeed on a host with **no polkit agent and no interactive TTY available to the crossing** (a headless host): the ProxyCommand's `sudo systemd-run` crossing is authorized non-interactively by the NOPASSWD per-op sudoers `Cmnd_Spec` (`sudo -n` semantics — no password prompt path). This closes F-060, whose root cause was the unprivileged `pipe_cmd` crossing's dependence on an interactive polkit `manage-units` authorization.

POLKIT-mode separate-user attach remains **interactive-only** (it requires a polkit agent to authorize `manage-units`); this asymmetry SHALL be stated in operator-facing docs. Operator-rootless attach is unaffected (no crossing).

If the `fwd` crossing is denied by sudo (typical cause: a host provisioned before this op existed — the sudoers drop-in lacks the `fwd` `Cmnd_Spec`), the attach failure surfaces as the ssh client's connection-closed; the `setup_invariants` doctor check flags the op-enum drift and the remedy is `sudo sandbox setup` (re-renders the rule from the current op enum).

#### Scenario: Headless SUDO attach succeeds
- **WHEN** `sandbox attach <inst> <ws>` runs over a non-interactive SSH session on a headless separate-user SUDO host whose sudoers drop-in includes the `fwd` `Cmnd_Spec`, and the instance is running
- **THEN** the ProxyCommand crossing is authorized without any polkit agent or password prompt, the SSH session reaches core's sshd, and the operator lands in `/workspaces/<ws>`

#### Scenario: Stale sudoers rule surfaces as doctor drift
- **WHEN** attach fails on a SUDO-mode host whose drop-in predates the `fwd` op
- **THEN** `sandbox doctor`'s `setup_invariants` reports the sudoers op-enum drift (the installed rule does not match the current `core.dispatch.Op` enum) with the `sudo sandbox setup` remedy

#### Scenario: POLKIT attach without an agent still fails (documented)
- **WHEN** `sandbox attach` runs on a headless separate-user POLKIT host (no polkit agent)
- **THEN** the `pipe_cmd` crossing is refused by polkit (`manage-units` has no agent to authorize it) — unchanged behavior, documented as interactive-only; the SUDO auth mode is the supported headless path

