## Purpose

This specification defines runtime-aware configuration adjustments that reduce syscall amplification under gVisor's user-space sentry. When containers run with `runtime: runsc`, polling-heavy configurations that are imperceptible on bare metal can consume significant host CPU due to gVisor's 5–20× syscall translation overhead.

## Requirements

### Requirement: tmux Polling Interval for gVisor Compatibility
The admin container's `.tmux.conf` template SHALL set `status-interval` to 30 seconds or greater. Each status-line redraw triggers fork/execve for catppuccin status segments, and under gVisor's syscall translation the default 2-second interval produces observable host CPU load (~1 core idle).

#### Scenario: tmux status-interval is gVisor-compatible
- **WHEN** the source `templates/config/admin/.tmux.conf` template is inspected
- **THEN** it contains `set -g status-interval 30`

#### Scenario: tmux status-interval is not the bare-metal default
- **WHEN** the source `templates/config/admin/.tmux.conf` template is inspected
- **THEN** it does NOT contain `set -g status-interval 2`

### Requirement: tmux Activity Monitoring Disabled for gVisor Compatibility
The admin container's `.tmux.conf` template SHALL disable `monitor-activity` and `visual-activity`. These settings add every window's pty to the `poll()` set and run the terminal emulator on every byte received, even with zero clients attached. Under gVisor, this compounds with the status-interval syscall load.

#### Scenario: tmux monitor-activity disabled
- **WHEN** the source `templates/config/admin/.tmux.conf` template is inspected
- **THEN** it contains `setw -g monitor-activity off`

#### Scenario: tmux visual-activity disabled
- **WHEN** the source `templates/config/admin/.tmux.conf` template is inspected
- **THEN** it contains `set -g visual-activity off`

#### Scenario: tmux activity monitoring is not enabled
- **WHEN** the source `templates/config/admin/.tmux.conf` template is inspected
- **THEN** it does NOT contain `monitor-activity on` or `visual-activity on`

### Requirement: Admin Service runc Runtime Override
The admin service in `compose.yml` SHALL use `runtime: "runc"` (hardcoded), not `runtime: "{{ runtime }}"`. gVisor's PTY subsystem implements an intrinsic tight `poll(2)` loop when the PTY master has no attached physical terminal client. Under gVisor, this consumes ~2 CPU cores regardless of tmux configuration. The only resolution is runtime substitution — runc eliminates the overhead entirely.

#### Scenario: Admin uses runc runtime
- **WHEN** the `compose.yml` template source is inspected for the admin service
- **THEN** it contains `runtime: "runc"` (not `runtime: "{{ runtime }}"`)

#### Scenario: Admin runtime is not templated
- **WHEN** the `compose.yml` template source is inspected for the admin service
- **THEN** the `runtime:` value does NOT contain `{{ runtime }}` or any Jinja2 variable

#### Scenario: Core retains gVisor runtime
- **WHEN** the `compose.yml` template source is inspected for the core service
- **THEN** it contains `runtime: "{{ runtime }}"` (resolves to gVisor `runsc` at render time)

#### Scenario: Admin retains full security baseline
- **WHEN** the rendered `compose.yml` is inspected for the admin service
- **THEN** it contains `cap_drop: [ALL]`, `no-new-privileges:true`, `read_only: true`, and `ipc: private` (security posture unchanged by runtime switch)
