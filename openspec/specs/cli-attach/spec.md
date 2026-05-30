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

#### Scenario: Running sandbox allows attach
- **WHEN** `sandbox attach <inst>` is invoked and `docker compose ps -q` returns non-empty output
- **THEN** the CLI proceeds to hand the terminal over to core via the canonical `tlog-rec → ssh → ProxyCommand → /fwd` invocation

#### Scenario: Stopped sandbox rejects attach
- **WHEN** `sandbox attach <inst>` is invoked and no containers are running for the instance
- **THEN** the CLI exits with: "Sandbox '<inst>' is not running. Use 'sandbox start <inst>' to launch."

### Requirement: PTY Handover Without Re-Hydration
The system SHALL hand the operator's terminal over to **core** (as `agent`) via a host-side ssh client wrapped in `tlog-rec`, using a `ProxyCommand` that crosses the privilege boundary into the unprivileged docker user via `pipe_cmd` (`systemd-run -q --pipe --uid=<docker_unprivileged_user>`) and execs `/fwd` inside admin to forward stdio↔TCP to `core_ipc_ip:9999`. The handover SHALL NOT re-run hydration, credential generation, or IPAM allocation.

The `ProxyCommand` SHALL use `pipe_cmd` (polkit-authenticated via the `manage-units` action) regardless of the `machinectl_authentication` mode configured in host config; the `sudo`/`polkit` setting in host config governs `machinectl_cmd` (used by other handover paths) and does NOT prefix the `ProxyCommand` with `sudo` in either mode.

The in-container working directory SHALL be selected via the ssh remote command suffix `'cd /workspaces/<ws> && exec bash -l'` (per design D9), not via a `docker exec -w` flag.

#### Scenario: Attach bypasses hydration
- **WHEN** `sandbox attach <inst>` completes its warm state check successfully
- **THEN** no Jinja2 templates are rendered, no `.htpasswd` is regenerated, and no IPAM ledger is read or modified

#### Scenario: Terminal handed to core via ssh-through-admin
- **WHEN** containers are confirmed running, `state.lock` is released, and workspace `<ws>` is resolved (this scenario applies regardless of whether `machinectl_authentication` is `"sudo"` or `"polkit"` — the `ProxyCommand` uses `pipe_cmd` in both modes and is never prefixed with `sudo`)
- **THEN** the system invokes `tlog-rec --writer=file --file-path=<host-side log path> -- ssh -i <inst_dir>/secrets/ipc_ssh_key -o UserKnownHostsFile=<inst_dir>/secrets/ipc_known_hosts -o StrictHostKeyChecking=yes -o ProxyCommand="systemd-run -q --pipe --uid=<docker_unprivileged_user> /usr/bin/docker exec -i <inst>-admin-1 /fwd <core_ipc_ip>:9999" -p 9999 -t agent@<core_ipc_ip> 'cd /workspaces/<ws> && exec bash -l'`

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

When `host_config.host.docker_execution_mode == operator-rootless`, `cli.attach` SHALL construct the SSH `ProxyCommand` **without** the `pipe_cmd(...)` (`systemd-run -q --pipe --uid=…`) byte-pipe prefix: the ProxyCommand SHALL be the bare local `docker exec -i <project>-admin-1 /fwd <core_ipc_ip>:9999`. The surrounding handover structure (`tlog-rec -- ssh -i … -o ProxyCommand="…" -p 9999 -t agent@<core_ipc_ip>`) SHALL be unchanged; only the inner crossing prefix is removed. In `separate-user` mode the ProxyCommand SHALL continue to compose `pipe_cmd(docker_unprivileged_user)` exactly as before.

#### Scenario: operator-rootless ProxyCommand has no pipe_cmd prefix

- **WHEN** `sandbox attach <inst>` runs with `docker_execution_mode == operator-rootless`
- **THEN** the `ProxyCommand` argument is `docker exec -i <project>-admin-1 /fwd <core_ipc_ip>:9999` with no `systemd-run`/`--uid=` prefix

#### Scenario: separate-user ProxyCommand unchanged

- **WHEN** `sandbox attach <inst>` runs with `docker_execution_mode == separate-user`
- **THEN** the `ProxyCommand` argument is composed via `pipe_cmd(host_config.host.docker_unprivileged_user)` exactly as before this change

