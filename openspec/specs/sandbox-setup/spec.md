# sandbox-setup Specification

## Purpose
TBD - created by archiving change sandbox-setup. Update Purpose after archive.
## Requirements
### Requirement: `sandbox setup` Command Entry Point

The system SHALL provide a `sandbox setup` command invoked as `sudo sandbox setup`. The command SHALL run as root throughout (`os.geteuid() == 0` at entry; refuse with diagnostic otherwise). The command SHALL operate idempotently: re-running on a converged host produces no mutations and reports `already correct` for each phase.

The command builds a `SetupContext` (the `host_config` + the resolved operator) BEFORE any phase runs. On a fresh host the per-operator `<sandbox_ai_home()>/config/sandbox-ai.toml` does not yet exist — and setup does **not** create it: the per-operator state tree + `sandbox-ai.toml` are the artifact of `sandbox init` running **as the operator** (F-021; setup runs as root, where `sandbox_ai_home()` resolves to `/root/.sandbox-ai`, invisible to the operator — so setup MUST NOT seed there). The entry point therefore SHALL bootstrap `host_config` from built-in defaults when the toml is absent: it loads the toml when present, and on its absence falls back to a defaults-only `HostConfig` whose `[host]` values (`docker_unprivileged_user`, `machinectl_authentication`, `workspace_bridge_group`) are the documented defaults (`docker_unprivileged_user="sandbox"`, `workspace_bridge_group="sb-ws"`; the auth mode is supplied explicitly per the "Machinectl Auth Mode Selection" requirement). These same defaults are what `sandbox init` seeds when it later creates the operator toml, so the values setup's phases observe and the values the operator's runtime commands read are consistent. An operator overrides `docker_unprivileged_user` by hand-editing the toml `init` seeds (then re-running setup is unnecessary for that value — it is read at runtime by the operator commands).

#### Scenario: Setup invoked without sudo
- **WHEN** the operator runs `sandbox setup` without `sudo` (i.e., effective uid != 0)
- **THEN** the command exits non-zero with the message `sandbox setup must be run as root. Re-invoke as: sudo sandbox setup`

#### Scenario: Setup invoked on a converged host
- **WHEN** an operator runs `sudo sandbox setup` on a host where every phase's probe would return "already correct"
- **THEN** the apply pass completes in under 5 seconds, the finalization summary shows every phase as `skip (already correct)`, and the operator's `sandbox doctor` continues to return green

#### Scenario: Fresh-host host_config bootstrap (sandbox-ai.toml absent)
- **WHEN** `sudo sandbox setup` runs on a fresh host where `<sandbox_ai_home()>/config/sandbox-ai.toml` does not exist, and the entry point must construct the `SetupContext.host_config` before any phase runs
- **THEN** the entry point builds `host_config` from defaults equivalent to `minimal_host_config` with `docker_unprivileged_user="sandbox"` and the explicitly-selected auth mode, proceeds with the ceremony, and does NOT create the per-operator tree or seed `sandbox-ai.toml` (that is `sandbox init`'s job, run as the operator); on any subsequent run an operator-seeded toml, if present, is loaded instead of the defaults

### Requirement: Machinectl Auth Mode Selection

`sandbox setup` SHALL accept the auth mode as an **explicit input** via a `--machinectl-auth {sudo|polkit}` flag, NOT infer it from a config file that does not exist yet on a fresh host. The effective auth mode SHALL be resolved with this precedence: the `--machinectl-auth` flag value if given; else the operator toml's `machinectl_authentication` if a toml is present; else the SUDO default.

**POLKIT is fenced in this version (D2 / F-022).** If the resolved effective auth mode is POLKIT — via the flag OR a present operator toml requesting it — setup SHALL refuse **before any phase runs** (no host mutation, no plan pass, no apply pass), exiting non-zero with a message that names the unsupported mode, points at the SUDO alternative and the manual polkit-config docs, and references the follow-on work (the POLKIT auth-mode change + validation track V9d-polkit-e2e). The refusal reason is load-bearing: setup's per-op verification phases (L3a/L8) probe SUDO-only, so a polkit rule setup installed could not be verified and would be rolled back — a half-wired polkit path is worse than an explicitly-unsupported one. An out-of-domain `--machinectl-auth` value (not `sudo`/`polkit`) is likewise refused before any phase runs.

An explicit `--machinectl-auth sudo` is the affirmative SUDO selection and overrides a stale operator toml that says polkit; the constructed `host_config` carries the resolved (SUDO) auth mode regardless of the toml, so L3 renders the sudoers rule the verification phases can check. The `setup_invariants` doctor check then WARNs on the residual toml/rule disagreement (see the `cli-doctor` capability's "Setup Invariants Check"). `sandbox init` and `sandbox doctor` retain their own `--machinectl-auth` handling unchanged — they operate at runtime in both modes; only *setup*'s rule-writing is fenced.

#### Scenario: --machinectl-auth sudo proceeds
- **WHEN** `sudo sandbox setup --machinectl-auth sudo` runs (toml absent or present)
- **THEN** setup resolves SUDO, builds a `host_config` whose `machinectl_authentication` is `sudo`, and proceeds with the ceremony

#### Scenario: --machinectl-auth polkit is refused before any mutation
- **WHEN** `sudo sandbox setup --machinectl-auth polkit` runs
- **THEN** setup exits non-zero with the polkit-unsupported refusal (naming the SUDO alternative + manual-config docs + the follow-on track); no phase probe or act runs; no host state is mutated

#### Scenario: operator toml requesting polkit is refused without a flag
- **WHEN** `sudo sandbox setup` runs with no `--machinectl-auth` flag and a present `sandbox-ai.toml` whose `[host].machinectl_authentication = "polkit"`
- **THEN** setup refuses with the same polkit-unsupported message (it does NOT silently downgrade to sudo); no host state is mutated

#### Scenario: explicit sudo flag overrides a stale polkit toml
- **WHEN** `sudo sandbox setup --machinectl-auth sudo` runs with a present toml whose `[host].machinectl_authentication = "polkit"`
- **THEN** setup proceeds in SUDO mode, the constructed `host_config.machinectl_authentication` is `sudo` (so L3 renders a sudoers rule), and the other `[host]` fields from the toml are preserved

### Requirement: Operator Resolution Precedence

When running as root, the system SHALL resolve the operator user via the following explicit precedence; on no resolution, refuse with a diagnostic:

1. The `--operator <name>` flag value (if provided); MUST match an existing user via `pwd.getpwnam(<name>)`.
2. `$SUDO_USER` from the environment; MUST be consistent with `$SUDO_UID` via `pwd.getpwnam($SUDO_USER).pw_uid == int($SUDO_UID)`.
3. `$PKEXEC_UID` from the environment; resolved via `pwd.getpwuid(int($PKEXEC_UID))`.
4. **No fallback.** Refuse with: `cannot resolve operator user. Re-invoke as: sudo sandbox setup, or pass --operator <name>.`

The system SHALL NOT use TTY-based heuristics (e.g., owner of `/dev/tty`, `os.getlogin()`, `who -m`) to infer the operator user.

#### Scenario: Operator resolved via $SUDO_USER
- **WHEN** the operator runs `sudo sandbox setup` from a normal user shell and `$SUDO_USER=alice`, `$SUDO_UID=1000`, `pwd.getpwnam("alice").pw_uid == 1000`
- **THEN** setup resolves the operator as `alice` and proceeds

#### Scenario: Operator resolved via --operator flag overrides $SUDO_USER
- **WHEN** the operator runs `sudo sandbox setup --operator bob` and `$SUDO_USER=alice`
- **THEN** setup resolves the operator as `bob` (the flag takes precedence)

#### Scenario: Refuse when no operator identifiable
- **WHEN** the operator runs `sandbox setup` as a direct root login (no `sudo` from a user shell; `$SUDO_USER`, `$PKEXEC_UID` both unset; no `--operator` flag)
- **THEN** setup exits non-zero with `cannot resolve operator user. Re-invoke as: sudo sandbox setup, or pass --operator <name>.`

#### Scenario: Refuse on SUDO_USER/SUDO_UID inconsistency
- **WHEN** `$SUDO_USER=alice` but `$SUDO_UID=999` and `pwd.getpwnam("alice").pw_uid == 1000`
- **THEN** setup refuses with a diagnostic naming the inconsistency

### Requirement: Plan/Apply Two-Pass UX

The system SHALL execute setup in two passes:

1. **Plan pass**: every phase invokes its probe; no mutations occur. Output is doctor-style with severity markers (`✓ already correct`, `⊙ missing → will mutate`, `⚠ blocked → reason + remediation`, `✗ verify-only failure → refuse + remediation`). The plan output SHALL end with a one-line summary in the form `Summary: <N> already correct, <N> will mutate, <N> blocked, <N> refused` covering every phase exactly once.
2. **Apply pass**: gated by `--yes` (non-interactive) OR interactive confirm-prompt (TTY only) OR auto-skip (zero mutations). Each phase re-probes; on `already correct`, skips. On `missing` / `drift`, runs the act; then reverifies. On failure, logs FAIL, marks dependents BLOCKED-BY, continues with independent phases. The apply pass SHALL emit per-phase progress including the action taken (e.g., `→ write /etc/sysctl.d/49-sandbox-ai.conf`) and the reverify outcome.

The `--dry-run` flag SHALL run only the plan pass; no apply pass executes. The plan pass output SHALL be byte-identical (modulo timestamps) whether reached via `--dry-run` or via the normal two-pass flow's first pass.

The interactive confirm prompt SHALL use the exact text `Proceed with apply? [y/N]:` with a trailing space. The default (Enter, or any input other than `y`/`yes` case-insensitive) SHALL be N (abort). Only `y` / `Y` / `yes` / `YES` (case-insensitive) SHALL proceed.

#### Scenario: Dry-run shows mutations without applying them
- **WHEN** the operator runs `sudo sandbox setup --dry-run` on a fresh host
- **THEN** the plan output shows `⊙ missing → will mutate` for every uninitialized phase; no mutations occur; the host is byte-identical after the command as before; exit code 0

#### Scenario: Apply pass refused in non-TTY without --yes
- **WHEN** `sudo sandbox setup` runs in a non-TTY context (no controlling terminal) without `--yes`
- **THEN** setup refuses with `non-interactive context requires --yes flag to apply mutations` and exits non-zero

#### Scenario: TTY apply pass prompts after plan when mutations are pending
- **WHEN** `sudo sandbox setup` runs in a TTY without `--yes` AND the plan shows ≥1 mutation AND zero refusals
- **THEN** stdout emits the plan output (including the summary line), then the prompt `Proceed with apply? [y/N]: `; setup blocks reading from stdin for the operator's response

#### Scenario: Operator types y to proceed
- **WHEN** the operator responds with `y` (or `Y` / `yes` / `YES`) followed by Enter
- **THEN** setup enters the apply pass and executes phases in order; exit code reflects apply-pass outcome

#### Scenario: Operator types n or presses Enter to abort
- **WHEN** the operator responds with `n` / `no` / any other input / just presses Enter
- **THEN** setup emits `aborted by operator (n). No mutations applied.` to stdout and exits 0 (intentional abort, not failure); no further phases execute

#### Scenario: Operator Ctrl-C aborts
- **WHEN** the operator presses Ctrl-C while setup is blocked on the prompt (or at any earlier point in the plan pass)
- **THEN** setup catches SIGINT, emits `aborted by operator (SIGINT). No mutations applied.` to stderr, and exits with code 130 (standard SIGINT convention: 128 + signal number)

#### Scenario: Plan shows zero mutations — no prompt, exit 0
- **WHEN** `sudo sandbox setup` runs on a host where every phase's probe returns `already correct` (idempotent re-run on a converged host)
- **THEN** the plan output ends with `Summary: <N> already correct, 0 will mutate, 0 blocked, 0 refused`; setup emits `Nothing to apply. Setup is complete.`; exit code 0; **no confirm prompt is shown**

#### Scenario: Plan shows ≥1 refusal — no prompt, exit non-zero
- **WHEN** `sudo sandbox setup` runs and the plan pass reports ≥1 phase with the `✗ verify-only failure` marker (e.g., cgroup v2 hierarchy inactive, ACL FS support missing, unrecognized distro at L0)
- **THEN** the plan output ends with the summary line showing `<N> refused`; stdout enumerates each refusal's remediation hint; setup emits `Setup will not enter the apply pass.`; exit non-zero; **no confirm prompt is shown**; the apply pass never runs (refusals block setup unconditionally)

#### Scenario: --yes skips the prompt and proceeds
- **WHEN** `sudo sandbox setup --yes` runs in a TTY context with mutations pending
- **THEN** the plan output is emitted; the prompt is NOT shown; setup proceeds directly into the apply pass

#### Scenario: Apply pass continues past non-rollback failures
- **WHEN** the apply pass encounters a phase failure that is NOT L3a (sudoers/polkit install + probe)
- **THEN** that phase is marked FAIL, its dependents are marked BLOCKED-BY, and the apply pass continues with independent phases; final exit code is non-zero; finalization summary names each FAIL and BLOCKED-BY phase with remediation pointers

#### Scenario: Plan summary line format
- **WHEN** the plan pass completes
- **THEN** the final line before the prompt (or before any operator-facing prompt/refusal text) is `Summary: <A> already correct, <M> will mutate, <B> blocked, <R> refused` where `A + M + B + R` equals the total phase count in this invocation (including any sticky-opt-in integration phases)

### Requirement: Phase Execution Order

The system SHALL execute the following phases in the named order during the apply pass. (The order is named, not counted: the historical layer numbers L0..L8 plus the sub-phases L6a [runsc install] and L6.5 [dispatcher install] are stable identifiers — do not re-introduce a brittle "N-phase" integer that drifts when a sub-phase is added; phase-3 review R1.)

1. **L0** identity + env resolution (root assertion; operator resolution per precedence rule; distro detection via `/etc/os-release`; binary check for required tools — a missing required tool (incl. `tlog-rec`) is a **`CONFLICT` refusal** surfaced identically in the plan and apply passes (`✗ verify-only failure → refuse`), NOT a convergeable `DRIFT`: setup never installs distro packages (Pattern A — detect early, refuse with an actionable per-distro install hint; on Debian 13+/trixie the `tlog` hint points at the from-source build since it is unpackaged there). L0 mutates nothing — it is verify/refuse, so `MISSING`/`DRIFT` never arise and the content-aware-DRIFT contract (D10) does not apply to it; **machinectl-path resolution + uniqueness assertion** — enumerate EVERY executable `machinectl` across the sudoers `secure_path` basis [`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`, the compiled default; or the host's configured `secure_path` if it differs]. The found paths SHALL be **deduped by resolved file identity** (`os.stat` `(st_dev, st_ino)`) before the count rule — multiple `secure_path` entries that are the *same file* (the usrmerge case: `/usr/bin`↔`/usr/sbin`↔`/sbin`↔`/bin` are symlinked to one directory, so one `machinectl` inode appears under several path strings) count as **one** machinectl, NOT several. An unstattable path is keyed by its path string (never silently merged into a real binary's identity group). Then: **exactly one distinct file, with a canonical alias (`/usr/bin` or `/usr/sbin`)** → carry the canonical `/usr/bin/machinectl` form as `MACHINECTL_PATH` for the L3 rule renderer. **Zero** → refuse (the orchestrator's `sudo machinectl …` could never be granted by any rule). **≥2 *genuinely distinct* files (different `(st_dev, st_ino)`)** → refuse with a diagnostic naming every path found and instructing the operator to remove the unexpected copy. **One distinct file but only outside a canonical location (e.g. solely `/usr/local/*`)** → refuse. Rationale: V9e/V9e-2 established that sudo's relative-command Cmnd resolution is version-divergent when ≥2 *distinct* `machinectl` binaries exist on secure_path (1.9.5p2 runs the Cmnd_Spec-permitted one; ≥1.9.15 resolves first-on-path then matches) — security-benign on both [neither runs an attacker shadow as root], but to keep behavior uniform and unambiguous across the supported sudo range, L0 refuses the genuinely-multi-binary state outright. The inode dedupe **preserves this anti-shadow property**: an attacker shadow at e.g. `/usr/local/bin/machinectl` is a *different inode* → still a second distinct file → still refused; only usrmerge symlink-aliases of one byte-identical file are collapsed (otherwise L0 would false-refuse on essentially every modern usrmerged distro — Fedora, Debian-trixie, Arch — empirically caught by the Milestone-J fresh-Fedora smoke; F-014). A successfully-installed rule therefore still can never coexist with a *distinct* shadowing `machinectl`.)
2. **L1** sysctl drop-in + verify-only (sysctl drop-in + `sysctl -w` for `kernel.unprivileged_userns_clone` [Debian-family only] + `user.max_user_namespaces`; verify-only for ACL FS support and cgroup v2 hierarchy). **L1 resolves no OS user.** The uid-scoped `Delegate=yes` drop-in was split out into L2a (below) — its path is keyed to the sandbox uid that L2 creates, so it cannot run before L2.
3. **L2** host-side prerequisites (systemd-machined enable + start; useradd for sandbox user; `/etc/subuid` + `/etc/subgid` append-only-when-safe; groupadd sb-ws at autodetected gid in subuid range; usermod -aG sb-ws operator). **L2 does NOT install runsc** — runsc is its own phase L6a (below); R1.
4. **L2a** systemd `Delegate=yes` drop-in (write `/etc/systemd/system/user-<sandbox-uid>.service.d/sandbox-ai-delegate.conf` narrow-scoped to the sandbox user's manager — NOT template-wide `user@.service.d/` — + `systemctl daemon-reload`; content-aware probe). `depends_on=("l2",)` and ordered before L5: the drop-in path is **uid-scoped to the sandbox user L2 creates**, so on a fresh host it cannot be resolved or written until L2 has run, and L5's rootless dockerd needs the cgroup delegation in place. (Empirically motivated — Milestone-J fresh-Fedora smoke: L1's pre-L2 `pwd.getpwnam('sandbox')` crashed the plan pass; the probe-exception class is now also backstopped systemically in `run_plan_pass`/`run_apply_pass`. See finding F-014.)
5. **L5** linger + rootless dockerd install (loginctl enable-linger sandbox-user; dockerd-rootless-setuptool.sh install via machinectl)
6. **L6** daemon.json reserved-key + restart-cliff + reverify (merge `runtimes["sandbox-ai-runsc"]` preserving operator's other runtimes; inode-stable write via `cat > file` per D9; **StartLimit-safe restart** = `systemctl --user reset-failed docker.service` then `systemctl --user restart --no-block docker`, followed by a **runtime-aware readiness poll** that waits until `docker info` lists the reserved runtime — not merely until the daemon answers; **runtime-aware reverify** = the reserved runtime is loaded by the daemon, not merely present in the file. The `reset-failed`/`--no-block`/runtime-aware shape is the F-023 fix: a single restart that cannot trip systemd's start-rate-limit and whose success is observed out-of-band so a session teardown during the restart cannot mask it, and a probe/reverify that detects a write-success/restart-fail end state.)
7. **L6a** runsc install (shape #3 — install-if-absent at `/usr/local/libexec/sandbox-ai/runsc`; on a re-run, mention drift in the finalization summary but do NOT auto-overwrite; `--update-runsc` re-runs ONLY this phase with `force=True`; `chattr +i` after install. Detailed contract in the "gVisor Runsc Drift and Update Offering (Shape #3)" requirement. Distinct from L2 — runsc has an upstream-pinned lifecycle unlike the host-prereq mutations; R1.)
8. **L6.5** dispatcher install (compile via `core.dispatch.compile_dispatcher` per `runtime-dispatcher`'s recipe — which runs `go test ./...` for Python↔Go target-argv fixture parity *before* `go build` in the same `--network none` container, so a drift fails the compile and therefore L6.5 [C-e]; install to `/usr/local/libexec/sandbox-ai/dispatch` mode 0755 root:root; apply `chattr +i`)
9. **L7** helper image pre-pull (`docker pull busybox:musl@<pinned-digest>` via machinectl)
10. **L3** sudoers/polkit drop-in install + probe (write `/etc/sudoers.d/sandbox-ai-machinectl-<operator>` OR `/etc/polkit-1/rules.d/49-sandbox-ai-machinectl.rules`; visudo -cf on staged file before install for sudoers mode; **L3a** post-install per-op probe via `sudo_as_operator(<operator>)` (= `sudo -u <operator>`) → `sudo -n machinectl shell <sandbox-user>@.host /bin/bash -c '/usr/local/libexec/sandbox-ai/dispatch <op> --check'` for every op in `SANDBOX_OPS` — note the probe invokes **relative `machinectl`**, byte-identical to what `core.host_config.machinectl_cmd()` emits at runtime, so the sudoers `secure_path`→Cmnd_Spec resolution bridge is verified per-host; `visudo -cf` is necessary but not sufficient per F-004; ROLLBACK on probe failure. The operator drop is `sudo_as_operator` — a **normal-process** `sudo -u` drop — NOT `pipe_cmd`: the command run as the operator is the setuid binary `sudo`, and execing a setuid binary from inside a `pipe_cmd`/`systemd-run --uid` transient unit fails with `EXIT_EXEC` on a real host, so the probe got empty output → "sentinel not found" → FAIL → rolled back a correct rule. See finding F-016. `pipe_cmd` remains correct for the plain-binary operator probes/L8.)
11. **L8** fresh-session re-probe via `pipe_cmd(operator)` (verify operator's group set includes sb-ws gid; verify machinectl reachable through the new rule)

L3 SHALL be the last mutation phase of the **base ceremony (L0..L8)** and SHALL be the only phase that installs or mutates the privilege-boundary rule (the sudoers/polkit drop-in). L8 is verification, not mutation. The security property this ordering buys is precise: **no privilege-boundary rule exists on disk at any point before L3, and none is mutated after it** — so a crash anywhere in L0..L7 leaves zero sudoers/polkit grant (no permissive-bootstrap-rule window), and a crash during L3 is handled by L3a's rollback. Setup-as-root invokes machinectl directly per V8's empirical validation; no permissive bootstrap rule is installed at any point.

The optional fapolicyd/AIDE integration phases (see their requirements below) run AFTER L8 and DO perform mutations — but ONLY within their own `/etc/fapolicyd/trust.d/` and `/etc/aide/aide.conf.d/` owned namespaces; they never create, mutate, or delete the sudoers/polkit privilege-boundary rule. They therefore cannot reopen a permissive-rule window, and they fail without rolling back the base ceremony (L0..L8 stand). The "L3 is last" property is thus scoped to the base ceremony and to the privilege-boundary rule specifically; it is NOT a claim that no file on the host is mutated after L3. (Phase-3 review C-b: the earlier unqualified "L3 SHALL be the LAST mutation phase" was false in the presence of the post-L8 integration phases; the security argument is preserved by this precise restatement.)

#### Scenario: L3 follows L6.5 (dispatcher binary exists before sudoers references it)
- **WHEN** the apply pass executes phase order
- **THEN** L6.5 (dispatcher install) completes successfully before L3 (sudoers/polkit install) begins; if L6.5 fails, L3 is marked BLOCKED-BY L6.5 and never executes

#### Scenario: L3 sudoers probe failure triggers rollback
- **WHEN** L3 installs `/etc/sudoers.d/sandbox-ai-machinectl-<operator>` successfully (visudo -cf passes) and any iteration of L3a's per-op probe (`sudo_as_operator(<operator>)` → `sudo -n machinectl shell <user>@.host /bin/bash -c '/usr/local/libexec/sandbox-ai/dispatch <op> --check'`, relative `machinectl`) yields a **recovered inner exit** (via the sentinel mechanism — NOT the raw outer exit, which `machinectl shell` masks) that is non-zero
- **THEN** setup removes the just-installed drop-in file (`rm /etc/sudoers.d/sandbox-ai-machinectl-<operator>`), marks L3 as FAIL with the probe's diagnostic output (failing op + recovered inner exit), and continues with L8 marked BLOCKED-BY L3

#### Scenario: Setup-as-root does not require sudoers rule pre-installation
- **WHEN** setup begins on a fresh host with no existing `/etc/sudoers.d/sandbox-ai-*` files
- **THEN** all phases up to and including L7 execute successfully via root → machinectl direct invocation (no sudoers rule needed); L3 then writes the final dispatcher-shaped rule

#### Scenario: Post-L8 integration phases never touch the privilege-boundary rule
- **WHEN** an optional fapolicyd or AIDE integration phase runs after L8 (via flag or sticky opt-in) and performs its mutation (`/etc/fapolicyd/trust.d/sandbox-ai.trust` + `fapolicyd-cli --update`, or `/etc/aide/aide.conf.d/sandbox-ai.conf`)
- **THEN** the mutation is confined to that integration's own owned namespace; the sudoers/polkit privilege-boundary drop-in installed by L3 is neither read-for-write, mutated, nor removed by any integration phase; no permissive-rule window is opened; and if the integration phase fails it does not roll back or alter the L0..L8 base-ceremony results (including the L3 rule)

#### Scenario: A crash before L3 leaves no privilege-boundary rule
- **WHEN** setup is interrupted (crash, SIGKILL, power loss) at any point during L0..L7
- **THEN** no `/etc/sudoers.d/sandbox-ai-machinectl-*` or `/etc/polkit-1/rules.d/49-sandbox-ai-machinectl.rules` file exists on the host (the privilege-boundary rule is only ever written by L3, which has not run); the host has no sandbox-ai-granted passwordless boundary crossing — the deliberate "no permissive bootstrap rule" property

### Requirement: Reserved Namespace File Ownership

The system SHALL write only to namespaces it owns. The complete enumerable list of owned paths/keys:

- `/etc/sudoers.d/sandbox-ai-machinectl-<operator>` (mode 0440, root:root) — sudoers drop-in, SUDO auth mode
- `/etc/polkit-1/rules.d/49-sandbox-ai-machinectl.rules` (mode 0644, root:root) — polkit drop-in, POLKIT auth mode
- `/etc/sysctl.d/49-sandbox-ai.conf` (mode 0644, root:root) — kernel sysctl drop-in
- `/etc/systemd/system/user-<sandbox-uid>.service.d/sandbox-ai-delegate.conf` (mode 0644, root:root) — systemd Delegate=yes drop-in narrow-scoped to the sandbox user's manager (NOT template-wide `user@.service.d/`)
- `/usr/local/libexec/sandbox-ai/runsc` (mode 0755, root:root, `chattr +i`) — runsc binary
- `/usr/local/libexec/sandbox-ai/dispatch` (mode 0755, root:root, `chattr +i`) — dispatcher binary
- `runtimes["sandbox-ai-runsc"]` key in `~<sandbox-user>/.config/docker/daemon.json` — reserved key only
- Append-only entries for `<sandbox-user>` in `/etc/subuid` + `/etc/subgid` — flat-file shared territory
- `/etc/fapolicyd/trust.d/sandbox-ai.trust` (mode 0644, root:root) — fapolicyd trust drop-in, OPTIONAL (present only when fapolicyd integration is enabled; see "Optional Fapolicyd Integration Phase" requirement)
- `/etc/aide/aide.conf.d/sandbox-ai.conf` (mode 0644, root:root) — AIDE config drop-in, OPTIONAL (present only when AIDE integration is enabled; see "Optional AIDE Integration Phase" requirement)
- `/usr/local/libexec/sandbox-ai/dispatcher.manifest.json` (mode 0644, root:root) — dispatcher install manifest containing the compiled binary's sha512 + source-bundle sha512 + compile timestamp; see "Dispatcher Manifest Schema" requirement

The per-operator state tree `<sandbox_ai_home()>/{config,state,instances,workspaces}` and the `sandbox-ai.toml` seed are **NOT** in this list: they are created and owned by `sandbox init` running **as the operator** (`ensure_per_user_state` + `_seed_host_config_if_absent`), not by setup. Setup runs as root, where `sandbox_ai_home()` resolves to `/root/.sandbox-ai` (an artifact invisible to the operator), so setup MUST NOT create the per-user tree (F-021); it is the operator-plane artifact of `init`. The dispatcher manifest, by contrast, is shared host state (the binary is convergent across operators) and therefore lives on the host plane alongside the binary, root-owned and world-readable so every operator's `sandbox doctor` can read it.

Setup SHALL NOT edit, append to, or overwrite any file or key outside this enumerated list during normal operation. Each drop-in file SHALL carry a leading `# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'` comment (for sudoers `#` is the comment syntax; for polkit `//` is used).

#### Scenario: Setup never edits /etc/sudoers
- **WHEN** setup executes any phase
- **THEN** no write or modification occurs against `/etc/sudoers` (the main file); all sudoers changes route through `/etc/sudoers.d/sandbox-ai-machinectl-<operator>` exclusively

#### Scenario: Owned drop-ins carry the managed-comment header
- **WHEN** setup writes `/etc/sudoers.d/sandbox-ai-machinectl-<operator>` or any other drop-in in its enumerated list
- **THEN** the first non-empty line of the file is the literal comment `# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'` (or `//` variant for polkit syntax)

#### Scenario: /etc/subuid uses append-only semantics
- **WHEN** L2 mutates `/etc/subuid` (or `/etc/subgid`) and the sandbox user has an existing entry meeting the minimum range size
- **THEN** setup leaves the existing entry untouched (idempotent skip); does NOT shrink or replace

#### Scenario: /etc/subuid refuses to shrink inadequate existing range
- **WHEN** L2 finds an existing `/etc/subuid` entry for the sandbox user with a range smaller than the minimum acceptable size
- **THEN** setup refuses with `existing /etc/subuid entry for <sandbox-user> has range <X>; minimum required is <Y>. Refusing to shrink existing range. Manually update /etc/subuid and re-run setup.`

### Requirement: Sudoers Rule Shape

In SUDO auth mode, the system SHALL write `/etc/sudoers.d/sandbox-ai-machinectl-<operator>` with the V9-validated template below. The rule shape was determined empirically by validation track V9 (see `openspec/explorations/ongoing/sandbox-setup/validation.md`) under the constraints of finding F-004 (sudoers `Cmnd_Spec` args use literal double-quotes, not shell-quoting — backslash-escape is required for embedded whitespace) and finding F-003 (sudoers `Digest_Spec` silently no-op on Debian-family hosts):

In the body below, `/usr/bin/machinectl` is shown as the *typical* resolved value (it is what `machinectl` resolves to on all 11 distro images validated in V9c). It is NOT hardcoded: the renderer substitutes `MACHINECTL_PATH` — the absolute path L0 resolved for `machinectl` under the sudoers `secure_path` basis (see "Phase Execution Order" L0). Pinning the L0-detected path (rather than a literal `/usr/bin/machinectl`) is what makes the rule's Cmnd_Spec equal to whatever `sudo` resolves the orchestrator's relative `machinectl` to; L3a then proves that equality per-host (relative-form probe), and `setup_invariants` re-checks it for drift.

```
# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'
Defaults fast_glob
<operator> <HOSTNAME>=(root) NOPASSWD: NOSETENV: \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ auth-probe, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ auth-probe\ --check, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ compose-up\ *, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ compose-down\ *, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ compose-ps\ *, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ compose-ls, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ compose-ls\ --check, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ docker-version, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ docker-version\ --check, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ docker-info\ *, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ docker-manifest-inspect\ *, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ helper-chown-files\ *, \
    /usr/bin/machinectl shell <sandbox-user>@.host /bin/bash -c /usr/local/libexec/sandbox-ai/dispatch\ helper-mkdir-chown-dirs\ *
```

(No-arg ops — `auth-probe`, `compose-ls`, `docker-version` — render **two exact Cmnd_Specs**: the bare runtime form AND the `\ --check` probe form. See property 5.)

Six properties of this shape are load-bearing:

1. **Full invocation prefix in every `Cmnd_Spec`.** Sudo matches the `Cmnd_Spec` path against argv[0] AFTER `sudo` — which for the orchestrator's invocation is the `machinectl` command, not `/usr/local/libexec/sandbox-ai/dispatch`. A shorter `Cmnd_Spec` of just `/usr/local/libexec/sandbox-ai/dispatch <op> *` would silently fail to match anything the orchestrator emits at runtime, while still passing `visudo -cf`.

2. **Cmnd_Spec path = the L0-resolved `MACHINECTL_PATH`, not a hardcoded literal.** `core.host_config.machinectl_cmd()` emits **relative** `["sudo", "machinectl", "shell", …]` (verified against source). Sudoers `Cmnd_Spec` entries MUST be absolute paths; sudo bridges the two by resolving the relative `machinectl` against the sudoers `secure_path` and matching the *resolved absolute path* against the Cmnd_Spec. The rule is therefore only correct if its Cmnd_Spec path equals that resolution. The renderer pins `MACHINECTL_PATH` (L0-resolved on the same `secure_path` basis sudo uses) precisely so the two coincide. A hardcoded `/usr/bin/machinectl` would silently fail on any host where `machinectl` resolves elsewhere (non-usrmerged layout, a non-default `secure_path`). L0's uniqueness assertion (above) guarantees exactly one *distinct* (inode-deduped — usrmerge symlink-aliases collapsed, genuinely-distinct binaries still refused) canonical `machinectl` on secure_path, so the pinned path is unambiguous. The relative-form L3a probe (below) proves the clean-case bridge per-host at setup time; `setup_invariants` re-checks it. (Closes phase-3 review finding B-3: prior drafts pinned + probed an absolute path that is *not* the shape the orchestrator emits, leaving the bridge asserted but unverified.)

   **Security property (empirically established by V9e + V9e-2 across sudo 1.9.5p2 → 1.9.17p2).** Under this rule shape, NO supported sudo version executes a secure_path-shadowing `machinectl` as root. sudo 1.9.5p2 (RHEL 8.10/9, Rocky 8/9, Alma 9) resolves a relative command to the secure_path entry permitted by a `Cmnd_Spec` and runs *that* (the pinned binary — robust against shadowing). sudo ≥1.9.15 (Ubuntu 24.04, Debian trixie, Fedora, Arch) resolves to first-on-secure_path then matches, so a shadow → NOMATCH → grant denied (fail-safe; the shadow is never run as root). Secure_path-shadowing privilege escalation is therefore not possible. The *shadow-detection* property of the relative-form L3a probe and `setup_invariants` is sudo-version-dependent (≥1.9.15: a post-setup shadow → NOMATCH, genuinely caught as an availability break; 1.9.5p2: a post-setup shadow is harmless and L3a still MATCHes, correctly — there is no failure to catch). This version-dependence is moot in practice because L0's multi-machinectl refusal prevents a shadow from coexisting with a successfully-installed rule in the first place; `setup_invariants` still WARNs on a detected second machinectl as hygiene/availability advisory. (Constraint recorded as finding F-005.)

3. **Backslash-escape on embedded whitespace; NEVER `"…"`.** Per F-004, sudoers `Cmnd_Spec` args are whitespace-tokenized at parse time; double-quote characters are literal pattern bytes that match nothing real. The backslash-escape form (`dispatch\ auth-probe`, `dispatch\ compose-up\ *`) expresses "match a single argv element — the bash command — containing whitespace and slashes" via sudo's fnmatch.

4. **`Defaults fast_glob`** is included as defense in depth. V9 confirmed that sudo 1.9.15p5 (Ubuntu 24.04) does not apply `FNM_PATHNAME` to argv-arg matching whether or not `fast_glob` is set; the directive is a no-op on this version. Older sudo versions MAY apply `FNM_PATHNAME` and would block `*` from spanning `/` in arguments like `helper-chown-files /srv/parent 0644 1000 1000 a.log`; the directive defends against that case. Keep it for sudo versions where it matters; it has no negative effect where it doesn't.

5. **No-arg ops render BOTH the bare and the `\ --check` exact Cmnd_Specs.** No-arg ops (`auth-probe`, `compose-ls`, `docker-version`) carry no `\ *` wildcard (arg-smuggling denied — V9 B7), so an arg-bearing invocation does not match. But L3a probes *every* op with a trailing `--check` (the no-op-success isolation shape), so the rule MUST also grant the exact `<dispatch>\ <op>\ --check` form for no-arg ops, or the probe's `<op> --check` cannot match the bare `<op>` grant and L3a false-negatives ("sudo: a password is required") for a password-requiring operator — observed round-5 on Fedora once the L3a transport (F-016) was fixed and the probe finally reached the rule. Granting the literal `\ --check` (exact, no wildcard) preserves anti-arg-smuggling — only that single extra exact string is allowed, and it is a no-op-success the orchestrator never emits at runtime. Arg-bearing ops need no separate `--check` entry: their `\ *` already covers it. (G2 — F-016 sibling.)

6. **Cmnd specs are INLINED into the operator's user-spec — no shared `Cmnd_Alias`.** `Cmnd_Alias` (like all sudoers alias kinds) shares a single GLOBAL namespace across every file sudo loads from `/etc/sudoers.d/`. A per-operator drop-in that declared `Cmnd_Alias SANDBOX_OPS = …` therefore COLLIDED with every other operator's drop-in once two coexisted (the multi-operator-by-accumulation case) — sudo reports `duplicate Cmnd_Alias "SANDBOX_OPS"` on every parse, polluting each operator's `sudo` stderr and (were two operators ever to resolve different sandbox users / machinectl paths) silently letting one inherit the other's specs. The fix inlines the Cmnd list directly into the operator's user-spec (`<operator> <HOSTNAME>=(root) NOPASSWD: NOSETENV: <spec1>, \ <spec2>, …`), so each drop-in is wholly independent and two coexisting drop-ins parse cleanly under `visudo -c`. (`Defaults fast_glob` repeated across drop-ins is harmless — duplicate `Defaults` is not an error, unlike duplicate `Cmnd_Alias`.) Round-6's 12.4 multi-op test missed this because only one per-operator rule existed at a time then; round-7 exposed it once the password-operator's rule (F-018) finally installed alongside another operator's. (F-020.)

The rule SHALL bind to the resolved `<HOSTNAME>` (output of `hostname` at setup time), not `ALL`. The rule SHALL include the `NOSETENV:` tag. The rule SHALL NOT contain a `Digest_Spec` (`sha512:<hash>` prefix). The dispatcher op enumeration SHALL be rendered from `core.dispatch.Op` enum values at template-write time (single source of truth). The `<sandbox-user>` placeholder SHALL be resolved from `[host].docker_unprivileged_user`. Setup SHALL refuse to render any rule body containing `"` (double-quote) characters inside a `Cmnd_Spec` — the renderer's golden-file unit test asserts the rendered bytes are quote-free.

Setup SHALL validate the staged rule via `visudo -cf <staged-path>` before installing to the final path; a syntax error from visudo SHALL refuse the install with the visudo output. visudo's acceptance is a NECESSARY but NOT SUFFICIENT correctness signal per F-004 — the L3a post-install per-op probe (below) is the sufficient signal.

**Sudo-version compatibility.** This rule shape is empirically validated on sudo **1.9.5p2 → 1.9.17p2** across 11 distro images (V9c) — including the enterprise floor RHEL 8.10 / Rocky 8.9 / AlmaLinux 9 (all sudo 1.9.5p2) — and the relative→absolute `secure_path` bridge is validated across that same range (V9e/V9e-2). `Defaults fast_glob` is a no-op across this entire range (V9c) and is retained only as defense-in-depth for the (out-of-support) case of sudo older than the validated floor. Sudo versions **older than 1.9.5p2** occur only on EOL distros (RHEL 7 = 1.8.23, Debian 10 = 1.8.27); the rule shape is unverified there and `fast_glob` may be load-bearing-and-unconfirmed. Both setup L0 and the steady-state `cli-doctor` `setup_invariants` check WARN (do not FAIL) when the host's sudo predates 1.9.5p2 — out-of-support EOL territory, surfaced for operator visibility, not gated.

#### Scenario: Rule binds to current hostname
- **WHEN** setup runs on host `myhost.local` and writes the sudoers drop-in
- **THEN** the rule's host clause is `myhost.local=(root)` (the hostname is captured via `hostname` at setup time and embedded verbatim)

#### Scenario: Cmnd_Spec path is the L0-resolved machinectl path, not a hardcoded literal
- **WHEN** L0 resolves `machinectl` on the sudoers `secure_path` basis to `MACHINECTL_PATH` (e.g. `/usr/bin/machinectl` on a usrmerged host, or `/usr/local/sbin/machinectl` if a shadowing copy exists earlier on secure_path) and L3 renders the rule
- **THEN** every `Cmnd_Spec` inlined into the operator's user-spec begins with that exact `MACHINECTL_PATH` value; the renderer never emits a hardcoded `/usr/bin/machinectl` independent of L0's resolution

#### Scenario: Two operators' drop-ins coexist without a duplicate-alias collision
- **WHEN** two operators (`alice`, `bob`) each run setup so both `/etc/sudoers.d/sandbox-ai-machinectl-alice` and `…-bob` are present, and sudo loads both
- **THEN** neither drop-in declares a `Cmnd_Alias` (the Cmnd specs are inlined into each operator's user-spec); `visudo -c` over the combined `/etc/sudoers.d/` reports no `duplicate Cmnd_Alias` and exits 0; each operator's grant is independent (F-020)

#### Scenario: L0 refuses when machinectl does not resolve on the secure_path basis
- **WHEN** L0 runs and NO executable `machinectl` is found on the sudoers `secure_path` (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`, or the host's configured `secure_path`)
- **THEN** L0 fails with a diagnostic explaining that the orchestrator's `sudo machinectl …` invocation could never be granted by any sudoers rule on this host; setup does not proceed to subsequent phases

#### Scenario: L0 accepts a usrmerged host (one inode under several symlinked path strings)
- **WHEN** L0 enumerates `machinectl` on the secure_path basis of a usrmerged host and finds it under several path strings (`/usr/bin/machinectl`, `/usr/sbin/machinectl`, `/sbin/machinectl`, `/bin/machinectl`) that all `os.stat` to the **same `(st_dev, st_ino)`** (one byte-identical file via the merged-`/usr` symlinked directories)
- **THEN** L0 treats them as **one** distinct `machinectl`, does NOT refuse, and carries the canonical `/usr/bin/machinectl` form as `MACHINECTL_PATH`. (Counting path strings here would false-refuse on essentially every modern distro — Fedora, Debian-trixie, Arch; empirically caught by the Milestone-J fresh-Fedora smoke, F-014. Dedupe is by `os.stat` `(st_dev, st_ino)`.)

#### Scenario: L0 refuses ≥2 genuinely-distinct machinectl, or a sole non-canonical one
- **WHEN** L0 enumerates `machinectl` and, after deduping by `(st_dev, st_ino)`, finds **≥2 genuinely distinct files** (different inodes — e.g. a real attacker/extra binary at `/usr/local/bin/machinectl` whose inode differs from `/usr/bin/machinectl`; an unstattable path is keyed by its path string so it is never merged into a real binary's identity group), OR finds exactly one distinct file but only in a non-canonical location (e.g. solely `/usr/local/bin/machinectl`, nothing canonical)
- **THEN** L0 fails with a diagnostic listing every `machinectl` path found and instructing the operator to remove the unexpected copy (the orchestrator expects the single systemd-provided `/usr/bin/machinectl`); setup does not proceed. (Inode dedupe preserves the F-005/V9e anti-shadow property: a distinct shadow binary is a distinct inode → still refused; only usrmerge symlink-aliases of one byte-identical file are collapsed. This refusal makes the V9e/V9e-2 sudo-version resolution divergence unreachable on any host where setup succeeds — a *distinct* shadow can never coexist with an installed rule.)

#### Scenario: L3a relative-form probe behavior under a shadowing machinectl is sudo-version-dependent (V9e/V9e-2)
- **WHEN** a shadowing `machinectl` somehow exists earlier on secure_path at L3a time despite L0's uniqueness assertion (e.g. a concurrent process dropped `/usr/local/bin/machinectl` between L0 and L3a)
- **THEN** on sudo ≥1.9.15 the relative-form probe returns `password is required` (sudo resolves to the shadow, ≠ pinned Cmnd_Spec → NOMATCH); setup rolls back the drop-in and reports the path-drift cause. On sudo 1.9.5p2 the probe MATCHes (sudo resolves the relative command to the Cmnd_Spec-permitted path and runs the pinned binary; the shadow is ignored) — this is *correct*, not a missed failure: V9e-2 established the pinned binary executes, so there is no exploit and no availability break to catch. In neither case is the attacker-plantable shadow executed as root. (The absolute-form probe used in pre-B-3 drafts would have spuriously MATCHed on *both* versions, masking the ≥1.9.15 availability break entirely — that is the F-004-class blind spot the relative-form probe removes on modern sudo. L0's multi-machinectl refusal makes this whole scenario unreachable on a host where setup succeeds; it is specified for defense-in-depth against post-L0 races.)

#### Scenario: Rule excludes Digest_Spec per F-003
- **WHEN** setup writes the sudoers drop-in
- **THEN** the rule does NOT contain any `sha512:`, `sha256:`, or other Digest_Spec prefix (F-003 confirmed silently no-op on Debian-family hosts; not relied on)

#### Scenario: Rule body excludes double-quote characters in any Cmnd_Spec (F-004)
- **WHEN** setup renders the sudoers rule body via the Jinja2 template
- **THEN** the rendered bytes contain ZERO `"` characters within any `Cmnd_Spec` segment of the inlined operator user-spec; embedded whitespace within a single argv pattern is encoded via backslash-escape (`\ `)

#### Scenario: Op names rendered into the rule body MUST match [a-z0-9-]+
- **WHEN** setup renders the sudoers rule body
- **THEN** every op-name segment (the bytes between the dispatcher path and the trailing `\ *` or end-of-Cmnd_Spec) MUST match the regex `[a-z0-9-]+`; rendering an op name containing any other character (e.g., `*`, `'`, `"`, backslash, whitespace) MUST raise a render-time `ValueError` and abort installation BEFORE `visudo -cf` is invoked.
- **(empirically motivated by V9c-C1C2.C1.1 / C1.2 — `visudo -cf` is a syntactic gate, not a content gate; it silently accepts op-name patterns containing `*` or `'`, producing a rule that grants more than intended.)**

#### Scenario: visudo validation rejects malformed staged rule
- **WHEN** setup stages the rule to `/tmp/sandbox-ai-machinectl-test` and `visudo -cf` returns non-zero
- **THEN** setup refuses the install with the visudo output and does NOT proceed to copy the file to `/etc/sudoers.d/`

#### Scenario: L3a per-op probe confirms each enumerated op resolves to MATCH (F-004 silent-footgun defense)
- **WHEN** setup completes L3 (sudoers drop-in installed; `visudo -cf` passed) and proceeds to L3a
- **THEN** for each op in `SANDBOX_OPS`, setup invokes `pipe_cmd(<operator>) → sudo -n machinectl shell <sandbox-user>@.host /bin/bash -c '/usr/local/libexec/sandbox-ai/dispatch <op> --check'` (where `--check` is the runtime-dispatcher no-op-success flag). The probe SHALL invoke **relative `machinectl`** — byte-identical to `core.host_config.machinectl_cmd()`'s runtime output — NOT an absolute `/usr/bin/machinectl`. This makes L3a verify the actual sudoers `secure_path`→Cmnd_Spec resolution bridge the orchestrator depends on; an absolute-path probe would spuriously MATCH even on a host where the relative-form orchestrator call fails (the exact F-004-class silent footgun this probe exists to defeat). Setup SHALL recover the **inner** `/bin/bash -c '<dispatch> <op> --check'` exit via the sentinel mechanism (`core.executor.Executor(...).run(..., sentinel=True)` or equivalent) and SHALL branch on that recovered inner exit, NOT on the raw `pipe_cmd` exit: `pipe_cmd` propagates only sudo/machinectl's exit, while `machinectl shell` masks the inner exit, so a dispatcher reject (unknown/absent/mis-pathed op → inner exit 2) without inner-exit recovery is masked as a sudoers MATCH — a misconfigured or absent rule reported healthy, the exact F-004/Finding-J class C-001's archived `runtime-dispatcher` spec mandates L3a defend against. The dispatcher's journald `check=1` record is audit-only and SHALL NOT be used as the control-flow signal. Setup asserts a recovered inner exit of 0 (MATCH) for each op. If any op's recovered inner exit is non-zero with `password is required` on stderr, the rule does not grant that op (a missed backslash-escape per F-004, OR `MACHINECTL_PATH` resolution drift between L0 and the live secure_path); any other non-zero recovered inner exit (incl. dispatcher reject exit 2) likewise indicates a broken/absent/mis-pathed rule or binary. On any non-MATCH outcome setup rolls back the drop-in atomically (`rm` the file) and reports the failing op, its recovered inner exit, and remediation.

### Requirement: Polkit Rule Shape (POLKIT Auth Mode)

This requirement defines the *intended shape* of the polkit rule. **In this version, `sandbox setup` does NOT write it: POLKIT is fenced at the entry point** (see "Machinectl Auth Mode Selection"). The rule shape is retained here for the manual-config path (operators who configure polkit by hand per `docs/setup-guide.md`) and for the follow-on change that will wire auth-aware L3a/L8 verification and un-fence setup's polkit path. When that rule *is* written (manually today, by setup in the follow-on), it SHALL take this shape:

In POLKIT auth mode, the system SHALL write `/etc/polkit-1/rules.d/49-sandbox-ai-machinectl.rules` granting the operator the `org.freedesktop.machine1.shell` action for the sandbox user without password prompt, scoped via JavaScript subject/action checks per polkit's rule syntax.

**Narrowing asymmetry (known, not merely unvalidated).** POLKIT-mode narrowing is inherently *coarser* than SUDO-mode. The sudoers `Cmnd_Spec` can match on the full argv (the V9 per-op `Cmnd_Spec`s, inlined into the operator user-spec, enumerate each dispatcher op + its arg shape). polkit's `org.freedesktop.machine1.shell` action does NOT expose the invoked command/argv as a matchable attribute — its inspectable subject/action fields are essentially the requesting user and the target machine/user. The polkit rule therefore grants "operator may `machinectl shell` into the sandbox user" at the *action* level; it canNOT enumerate or restrict to the ten dispatcher ops the way the sudoers rule does. **In POLKIT mode the per-op narrowing lives ONLY at the application layer**: the orchestrator only ever invokes `core.dispatch`, and the convention meta-test (`runtime-dispatcher` host-config capability) enforces that no other `src/` code crosses the boundary — exactly the same application-layer enforcement that backstops the SUDO legacy-rule case. Operator docs MUST state this asymmetry plainly (POLKIT mode = action-level grant + application-layer op discipline; SUDO mode = sudoers-layer per-op enumeration on top of the same application-layer discipline).

**Empirical status.** End-to-end confirmation that this polkit rule actually grants (the JS predicate returns `polkit.Result.YES` through a live `polkitd` + `systemd-machined`) is **deferred to validation track V9d-polkit-e2e** and is NOT yet established. V9c route-1 confirmed only that `pkaction` is present and the rule file parses syntactically across distros; it did NOT exercise the predicate against a running machined (containers have no machined). The SHALL above is the *intended* rule shape; the grant behavior is expected-but-unproven pending V9d-polkit-e2e (filed in `next.md`).

#### Scenario: POLKIT rule shape is written with the action-level predicate (syntactic)
- **WHEN** setup runs in POLKIT auth mode and writes the polkit drop-in
- **THEN** the rendered rule's JavaScript predicate references `action.id == "org.freedesktop.machine1.shell"` AND `subject.user == "<operator>"` AND resolves the action's `target_user` lookup to the configured sandbox-user; the file parses (no JS syntax error) and `pkaction --action-id org.freedesktop.machine1.shell` confirms the action exists in the catalog

#### Scenario: POLKIT grant is action-level, not per-op (documented asymmetry)
- **WHEN** the polkit rule is in effect and the operator invokes any `machinectl shell <sandbox-user>@.host …` command
- **THEN** polkit authorizes it at the `machine1.shell` action level regardless of the bash command (polkit cannot inspect the dispatcher op); per-op restriction is NOT provided by the polkit layer — it is provided solely by the application-layer invariant that the orchestrator only calls `core.dispatch` (convention-meta-test-enforced). This is the deliberate, documented POLKIT/SUDO asymmetry, not a defect

#### Scenario: End-to-end polkit grant is pending V9d-polkit-e2e
- **WHEN** the C-002 change is reviewed/archived
- **THEN** the requirement does NOT assert that the live polkit predicate returns `polkit.Result.YES` as an established fact; that grant is validated only when V9d-polkit-e2e runs against a real `polkitd`+`systemd-machined` (or a systemd-in-container harness); until then the POLKIT path is "intended shape, grant expected-but-unproven"

### Requirement: gVisor Runsc Drift and Update Offering (Shape #3)

L6a SHALL implement the following shape:
- Probe `/usr/local/libexec/sandbox-ai/runsc` for presence and sha512.
- If absent → download from `BINARY_REGISTRY["runsc"].url_template` (resolved with `$(uname -m)` substitution), verify sha512 against `BINARY_REGISTRY["runsc"].sha512`, install at the reserved path with `chattr +i`.
- If present + sha matches pinned → skip.
- If present + sha differs from pinned → record drift; mention in the finalization summary as: `runsc version drift: installed sha <X>, pinned sha <Y>. To update: sudo sandbox setup --update-runsc`.

The system SHALL provide a `--update-runsc` flag that runs ONLY the L6a phase (download + verify + install), ignoring the "already installed, sha matches" skip; intended for operators who acknowledged the drift mention and want to apply the pinned version.

The system SHALL NOT auto-update runsc on a re-run of `sudo sandbox setup` without the `--update-runsc` flag. Operators who want shape #2 (silent auto-update) configure it via a future change's `[setup] auto_update_runsc = true` config setting (out of scope for this change).

#### Scenario: Fresh host installs pinned runsc on first run
- **WHEN** L6a runs on a host where `/usr/local/libexec/sandbox-ai/runsc` is absent
- **THEN** setup downloads from `BINARY_REGISTRY["runsc"].url_template`, verifies sha512, installs to `/usr/local/libexec/sandbox-ai/runsc` mode 0755 root:root, applies `chattr +i`; the apply pass records the install

#### Scenario: Subsequent re-run with matching sha skips install
- **WHEN** L6a runs on a host where `/usr/local/libexec/sandbox-ai/runsc` exists with sha512 == `BINARY_REGISTRY["runsc"].sha512`
- **THEN** setup probes, finds matching sha, skips the install (apply pass records `skip (already correct)`)

#### Scenario: Drift detected on re-run without --update-runsc
- **WHEN** L6a runs on a host where `/usr/local/libexec/sandbox-ai/runsc` exists with sha512 != `BINARY_REGISTRY["runsc"].sha512`
- **THEN** setup skips the install (does NOT overwrite); records drift; finalization summary contains `runsc version drift: installed sha <X>, pinned sha <Y>. To update: sudo sandbox setup --update-runsc`

#### Scenario: --update-runsc applies the pinned version on drift
- **WHEN** `sudo sandbox setup --update-runsc` runs on a host with sha drift
- **THEN** setup `chattr -i`'s the existing binary, downloads + verifies + installs the pinned version, re-applies `chattr +i`; finalization summary records the update

### Requirement: chattr +i on Installed Binaries

After installing the dispatcher binary at `/usr/local/libexec/sandbox-ai/dispatch` (L6.5) OR runsc at `/usr/local/libexec/sandbox-ai/runsc` (L6a), the system SHALL set the immutable file attribute via `chattr +i <path>`. The attribute SHALL be cleared (`chattr -i`) before any replace operation (e.g., on `--update-runsc` or future dispatcher upgrades) and re-applied after the replace completes.

The `chattr +i` step is documented as a defense-in-depth measure per F-003's compensating-controls discussion. It does not provide crypto tamper resistance (root can clear the bit) but raises the bar for casual/automated tampering and adds an audit signal (chattr operations are unusual).

#### Scenario: Dispatcher binary carries immutable attribute after install
- **WHEN** L6.5 completes successfully
- **THEN** `lsattr /usr/local/libexec/sandbox-ai/dispatch` output line begins with characters including `i` (immutable attribute set)

#### Scenario: --update-runsc toggles the immutable bit through the replace
- **WHEN** `--update-runsc` applies a new pinned runsc to an existing immutable binary
- **THEN** the sequence is: `chattr -i /usr/local/libexec/sandbox-ai/runsc` → atomic install of new binary → `chattr +i /usr/local/libexec/sandbox-ai/runsc`; the immutable attribute is observable both before and after the update

### Requirement: Content-Aware Phase Probes

Every phase's probe SHALL inspect content, not just file presence, for any phase whose mutation can drift across sandbox-ai version upgrades. The probe SHALL render or compute the **expected state** from current sources (config inputs, `core.dispatch.Op` enum, `BINARY_REGISTRY` pins, `IMAGE_REGISTRY` pins, dispatcher source bundle, etc.) and compare against the **observed state** on disk. The phase SHALL skip its act only when expected and observed match exactly.

The content-aware probe semantic applies to:

- **L3 sudoers/polkit drop-in**: probe renders the expected rule from current `core.dispatch.Op` enum + operator name + hostname, compares byte-by-byte against the existing drop-in file. Mismatch → re-render and re-install (and re-run the L3a probe). Same applies to the polkit rule in POLKIT mode.
- **L6 daemon.json reserved key**: probe parses the existing daemon.json (if present) and deep-equals our `runtimes["sandbox-ai-runsc"]` value against the expected (path = `/usr/local/libexec/sandbox-ai/runsc`, runtimeArgs = `["--oci-seccomp"]`, or as defined by `runtime-dispatcher`). **The probe is runtime-aware (F-023): a file deep-equal is necessary but NOT sufficient — the probe additionally confirms the daemon has *loaded* the runtime (the reserved key appears in `docker info` runtimes). File-correct AND loaded → skip; file-correct but NOT loaded → DRIFT (restart needed); mismatch or absence → merge.** Act always (re)starts when it runs (StartLimit-safe per the phase-order description), since a file-only deep-equal that skipped the restart left the runtime unregistered — the masking gap F-023 captured. The merge never touches the operator's other `runtimes["..."]` keys.
- **L6.5 dispatcher install**: probe verifies both (a) on-disk binary's sha512 matches the manifest's recorded `compiled_sha512` AND (b) the manifest's recorded `source_bundle_sha512` matches the current source bundle's sha (computed by hashing, in sorted path order, the **full C-001 compile-input file set derived from `core.dispatch._DISPATCH_SOURCE_ENTRIES`** — currently `main.go, main_test.go, go.mod, go.sum, vendor, fixtures` under `src/templates/dispatch/`; NOT the narrower `{main.go, go.mod, go.sum, vendor/**}` subset, which omits `main_test.go`/`fixtures/` and would let a Python↔Go target-argv parity-fixture change move compile success without tripping the drift hash). Both match → skip. Either differs → recompile + reinstall + update manifest. The two-field manifest avoids stale dispatcher binaries on wheel upgrades.

#### Scenario: L6 daemon.json probe deep-equals reserved key only
- **WHEN** the existing daemon.json contains `runtimes["sandbox-ai-runsc"]` matching the expected value AND `runtimes["runsc-debug"]` (operator's own runtime), AND the daemon has loaded the reserved runtime (it appears in `docker info` runtimes)
- **THEN** the L6 probe reports already-correct; the operator's `runsc-debug` entry is untouched

#### Scenario: L6 probe detects a write-success/restart-fail end state (F-023)
- **WHEN** the daemon.json carries the correct `runtimes["sandbox-ai-runsc"]` value but the running daemon has NOT loaded it (the reserved runtime is absent from `docker info` runtimes — e.g. a prior restart failed and a file-only re-run skipped it)
- **THEN** the L6 probe reports DRIFT (not already-correct), and the act performs a StartLimit-safe restart (`reset-failed` then `restart --no-block`) and polls `docker info` until the reserved runtime is loaded; reverify confirms the loaded runtime, not merely the file

#### Scenario: L6.5 dispatcher probe skips on no-drift wheel re-run
- **WHEN** the manifest at `/usr/local/libexec/sandbox-ai/dispatcher.manifest.json` records `compiled_sha512=X` + `source_bundle_sha512=Y`, the on-disk binary's sha is `X`, AND the current source bundle's sha is `Y`
- **THEN** L6.5 skips compile + install; reports already-correct

#### Scenario: L6.5 dispatcher probe forces recompile on wheel upgrade
- **WHEN** the manifest records `compiled_sha512=X` + `source_bundle_sha512=Y` but the current source bundle's sha is `Y'` (different — wheel upgrade changed dispatcher source)
- **THEN** L6.5 recompiles, installs the new binary, updates the manifest with the new `compiled_sha512` + `source_bundle_sha512`

#### Scenario: L3 sudoers probe re-renders on Op enum change
- **WHEN** the existing sudoers drop-in's inlined per-op `Cmnd_Spec` enumeration differs from what `core.dispatch.Op` now contains (e.g., wheel upgrade added a new op)
- **THEN** the L3 probe detects the mismatch; L3 act re-renders from the current `core.dispatch.Op` enum, runs visudo -cf on the new content, and atomically replaces the drop-in; the L3a per-op probe re-verifies

### Requirement: Dispatcher Manifest Schema

Setup's L6.5 phase SHALL write the dispatcher's installation manifest at `/usr/local/libexec/sandbox-ai/dispatcher.manifest.json` (root-owned, mode 0644) — the host plane, alongside the binary, NOT under `<sandbox_ai_home()>/state/`. The dispatcher binary is shared host state convergent across operators, so its manifest is host-level; placing it under `sandbox_ai_home()` would resolve to `/root/.sandbox-ai/state/` under a root-running `sudo sandbox setup` (where the operator's `sandbox doctor` could never read it — F-021). Root-owned so only setup writes it; world-readable (0644) so every operator's doctor can read it. The schema is:

```json
{
  "compiled_sha512": "<128-hex-char sha512 of the installed binary>",
  "source_bundle_sha512": "<128-hex-char sha512 of concatenated source files>",
  "compile_timestamp": "<ISO 8601 UTC>"
}
```

The `source_bundle_sha512` SHALL be computed by hashing the concatenation, in deterministic alphabetical path-relative order, of the file set **derived from `core.dispatch._DISPATCH_SOURCE_ENTRIES`** (the single source of truth for C-001's compile inputs — currently the entries `main.go`, `main_test.go`, `go.mod`, `go.sum`, `vendor`, `fixtures` under `src/templates/dispatch/`, with directory entries expanded to every file beneath them in sorted path order). Setup SHALL derive this list from `core.dispatch._DISPATCH_SOURCE_ENTRIES` rather than hardcoding a literal file list, so the manifest's drift coverage tracks C-001's compile inputs automatically if that set ever changes (a hardcoded `{go.mod, go.sum, main.go, vendor/**}` subset omits `main_test.go`/`fixtures/`, which gate compile success via the in-container `go test ./...` Python↔Go parity run — a change there must trip the drift hash). The hash SHALL include file content only (not metadata such as mtime or mode).

The `compile_timestamp` field SHALL be the timestamp at which the L6.5 act last produced this manifest; it has no semantic role in the probe (informational for forensics).

#### Scenario: Manifest schema present after first L6.5 act
- **WHEN** L6.5 completes its act (compile + install + manifest write) for the first time
- **THEN** `/usr/local/libexec/sandbox-ai/dispatcher.manifest.json` is a valid JSON file containing exactly the three keys `compiled_sha512`, `source_bundle_sha512`, `compile_timestamp`; the binary at `/usr/local/libexec/sandbox-ai/dispatch` has the recorded `compiled_sha512`

#### Scenario: Source bundle sha is deterministic across hosts
- **WHEN** two operators on different hosts compute `source_bundle_sha512` against the same wheel-installed `src/templates/dispatch/` content
- **THEN** the resulting shas are byte-identical (content-only hash; no metadata)

### Requirement: Optional Fapolicyd Integration Phase

The system SHALL provide an optional fapolicyd integration phase that runs after L8 when **either** condition holds:

1. `--enable-fapolicyd-integration` is passed on the current invocation, OR
2. `/etc/fapolicyd/trust.d/sandbox-ai.trust` already exists (sticky opt-in: a previous setup invocation enabled the integration; subsequent setups auto-include it).

The phase SHALL:
- **Probe**: verify `fapolicyd` is installed (`which fapolicyd`); if absent, refuse with `fapolicyd not installed. Run: sudo apt install fapolicyd (or sudo dnf install fapolicyd; Arch: paru -S fapolicyd), then re-run.`; verify `/etc/fapolicyd/trust.d/` directory exists (if absent, refuse with `fapolicyd installed but trust.d directory missing; check fapolicyd installation`); compute expected trust-file content (the canonical content described below); compare against existing `/etc/fapolicyd/trust.d/sandbox-ai.trust` content. Match → skip. Mismatch or absence → run act.
- **Act**: render the trust file with one line per managed binary in the format `<absolute-path> <size-bytes> <sha256-hex>`:
  - `/usr/local/libexec/sandbox-ai/dispatch <size> <sha256>`
  - `/usr/local/libexec/sandbox-ai/runsc <size> <sha256>`
  Write the file at `/etc/fapolicyd/trust.d/sandbox-ai.trust` mode 0644 root:root with a leading `# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'` comment. Invoke `fapolicyd-cli --update` to reload the trust DB. If `systemctl is-active fapolicyd` returns "inactive," warn (don't fail) `fapolicyd installed but not running. Run: sudo systemctl enable --now fapolicyd to start enforcement.`
- **Reverify**: `fapolicyd-cli --check-trust file=/usr/local/libexec/sandbox-ai/dispatch` returns `trusted: yes`.

The phase SHALL fail without rolling back the base ceremony (L0..L8 already succeeded). On failure, the operator can re-run with the same flag(s) once the underlying issue is fixed.

The `/etc/fapolicyd/trust.d/sandbox-ai.trust` path SHALL be added to the enumerable owned-namespace list per the "Reserved Namespace File Ownership" requirement.

#### Scenario: Integration enabled via flag on first run
- **WHEN** the operator runs `sudo sandbox setup --enable-fapolicyd-integration` on a host where `/etc/fapolicyd/trust.d/sandbox-ai.trust` does not exist and fapolicyd is installed
- **THEN** the integration phase writes `/etc/fapolicyd/trust.d/sandbox-ai.trust` with the canonical content; runs `fapolicyd-cli --update`; reverifies trust; reports PASS

#### Scenario: Integration sticky on subsequent run without flag
- **WHEN** the operator ran `--enable-fapolicyd-integration` previously (so `/etc/fapolicyd/trust.d/sandbox-ai.trust` exists), then runs plain `sudo sandbox setup` without any integration flag
- **THEN** the fapolicyd phase auto-includes itself (probe detects the drop-in's presence); the probe finds matching content; skip the act; reports already-correct

#### Scenario: Integration drop-in refresh on dispatcher sha change
- **WHEN** L6.5 has just installed a new dispatcher binary (different sha than previous); the fapolicyd phase is auto-included via the sticky-opt-in semantic
- **THEN** the fapolicyd probe detects the trust file's recorded sha differs from the new binary's sha; the act re-renders the trust file with the new sha + size; `fapolicyd-cli --update` reloads

#### Scenario: --update-runsc cascades to fapolicyd trust refresh
- **WHEN** `sudo sandbox setup --update-runsc` runs on a host with sticky fapolicyd integration enabled
- **THEN** L6a re-installs runsc with the new pinned sha; the fapolicyd phase (auto-included via sticky opt-in) detects the trust file's recorded runsc sha is now stale; re-renders the trust file with the new runsc sha; reloads fapolicyd; no window of inconsistency

#### Scenario: Integration refuses when fapolicyd not installed
- **WHEN** the operator passes `--enable-fapolicyd-integration` on a host where `which fapolicyd` returns non-zero
- **THEN** the integration phase refuses with the distro-conditional install command; the base ceremony's L0..L8 results stand; integration phase reports FAIL with remediation

### Requirement: Optional AIDE Integration Phase

The system SHALL provide an optional AIDE integration phase that runs after L8 when **either** condition holds:

1. `--enable-aide-integration` is passed on the current invocation, OR
2. `/etc/aide/aide.conf.d/sandbox-ai.conf` already exists (sticky opt-in: a previous setup invocation enabled the integration).

The phase SHALL:
- **Probe**: verify `aide` is installed (`which aide`); if absent, refuse with `aide not installed. Run: sudo apt install aide (or sudo dnf install aide; Arch: sudo pacman -S aide), then re-run.`; verify `/etc/aide/aide.conf.d/` directory exists (if absent on operator's AIDE version that lacks drop-in support, refuse with `AIDE on this host does not support /etc/aide/aide.conf.d/ drop-ins; manually integrate per docs/setup-guide.md`); compute expected conf.d content; compare against existing `/etc/aide/aide.conf.d/sandbox-ai.conf`. Match → skip. Mismatch or absence → run act.
- **Act**: render the conf.d file with the canonical content:
  ```
  # sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'
  /usr/local/libexec/sandbox-ai/dispatch NORMAL
  /usr/local/libexec/sandbox-ai/runsc NORMAL
  ```
  Write at `/etc/aide/aide.conf.d/sandbox-ai.conf` mode 0644 root:root. Validate via `aide --config-check` if AIDE's version supports the flag; otherwise skip validation.
- **Reverify**: file is present at the expected path with the expected content.
- **DB initialization**: setup SHALL NOT automatically run `aide --init` (the operation walks the entire filesystem; 10+ minutes typical). On first install, the phase's reverify SHALL append a finalization-summary mention: `AIDE conf.d snippet installed. To begin monitoring, run: sudo aide --init (warning: walks the entire filesystem; can take 10+ minutes), then schedule periodic checks via cron (e.g., daily aide --check at off-peak hours).`

The phase SHALL fail without rolling back the base ceremony. The `/etc/aide/aide.conf.d/sandbox-ai.conf` path SHALL be added to the enumerable owned-namespace list per the "Reserved Namespace File Ownership" requirement.

#### Scenario: Integration enabled on first run, AIDE DB present
- **WHEN** the operator runs `sudo sandbox setup --enable-aide-integration` on a host where AIDE is installed and `/var/lib/aide/aide.db` exists (operator has an active AIDE workflow)
- **THEN** the integration phase writes `/etc/aide/aide.conf.d/sandbox-ai.conf`; no DB initialization is triggered; the operator's next scheduled `aide --check` picks up the new file paths

#### Scenario: Integration enabled on first run, AIDE DB absent
- **WHEN** the operator runs `sudo sandbox setup --enable-aide-integration` on a host where AIDE is installed but `/var/lib/aide/aide.db` does NOT exist
- **THEN** the integration phase writes the conf.d file; the finalization summary contains the `aide --init` operator-prompt; setup does NOT auto-initialize the DB

#### Scenario: Integration sticky on subsequent run
- **WHEN** the operator ran `--enable-aide-integration` previously, then runs plain `sudo sandbox setup` without the flag
- **THEN** the AIDE phase auto-includes itself; probe finds the conf.d file with matching content; skip the act

#### Scenario: Integration refuses on old AIDE without conf.d support
- **WHEN** `aide` is installed but `/etc/aide/aide.conf.d/` directory does not exist (older AIDE version)
- **THEN** the integration phase refuses with the documented guidance; the operator can either upgrade AIDE or manually integrate; the base ceremony's L0..L8 results stand

### Requirement: Distro Support Tiers

The system SHALL classify the detected distro at L0 into one of three tiers and behave accordingly:

- **Validated**: Debian, Ubuntu. Setup proceeds without any distro-related warning.
- **Untested**: Fedora, RHEL, CentOS, Arch, Manjaro. Setup proceeds with the same phase logic, branching only on distro-specific kernel knobs and package-manager commands. L0 SHALL emit a non-blocking warning. In TTY contexts, the warning includes a confirmation prompt (`Press Enter to continue, Ctrl-C to abort`); in non-TTY contexts, the warning is logged without prompting; `--yes` skips the prompt (the warning is still emitted).
- **Unrecognized**: everything else (Alpine, NixOS, openSUSE, Gentoo, Void, etc.). Setup SHALL refuse at L0 with a refusal message naming the supported distros and pointing operators at the setup guide; setup does NOT proceed to any subsequent phase.

The distro classification SHALL be derived from `/etc/os-release` `ID` and `ID_LIKE` fields via `core.doctor.detect_distro()` (existing helper). The classification SHALL be enumerable in code so contributors adding distro support have a single point of edit.

The canonical operator-facing warning text on untested distros SHALL be:

```
⚠ Untested distro

  Detected: <distro> <version>

  Debian and Ubuntu are sandbox-ai's reference distros. On other distros
  it uses the same logic with distro-specific package commands, but has
  not been end-to-end validated.

  To preview the steps without applying any, re-run with --dry-run.
  Manual remediation steps for each phase are documented in sandbox-ai's
  setup guide.

  Press Enter to continue, Ctrl-C to abort.
```

(The "Press Enter to continue" line is omitted in non-TTY contexts and when `--yes` is passed.)

The canonical operator-facing refusal text on unrecognized distros SHALL be:

```
✗ Unsupported distro

  Detected: <distro> <version>

  sandbox-ai's setup currently supports: Debian, Ubuntu, Fedora, RHEL,
  CentOS, Arch, Manjaro.

  If you'd like to use sandbox-ai on a different distro, the setup guide
  documents the manual ceremony steps so you can perform them by hand.

  Setup refuses to proceed on unrecognized distros to avoid making
  unpredictable system changes.
```

#### Scenario: Validated distro (Ubuntu) proceeds silently
- **WHEN** setup runs on Ubuntu 24.04
- **THEN** L0 detects the distro as validated; no warning emitted; setup proceeds to L1 without any distro-related output in plan or apply passes

#### Scenario: Untested distro (Fedora) emits warning with TTY prompt
- **WHEN** setup runs on Fedora 40 in a TTY context without `--yes`
- **THEN** L0 emits the canonical untested-distro warning including the `Press Enter to continue, Ctrl-C to abort` line; setup blocks waiting for operator input; on Enter, proceeds to L1; on Ctrl-C, exits non-zero without further mutations

#### Scenario: Untested distro in non-TTY context logs warning without prompt
- **WHEN** setup runs on Fedora 40 in a non-TTY context (e.g., CI) with `--yes`
- **THEN** L0 emits the canonical untested-distro warning WITHOUT the `Press Enter to continue` line; setup proceeds to L1 without waiting; warning content is identical otherwise

#### Scenario: Untested distro with --yes skips prompt
- **WHEN** setup runs on Fedora 40 in a TTY context WITH `--yes`
- **THEN** L0 emits the canonical untested-distro warning WITHOUT the `Press Enter to continue` line; setup proceeds to L1 immediately; the warning is logged for operator visibility

#### Scenario: Unrecognized distro (Alpine) refuses
- **WHEN** setup runs on Alpine 3.20 (`detect_distro()` returns `None` because Alpine is not in the supported-distros map)
- **THEN** L0 emits the canonical unsupported-distro refusal text; setup exits non-zero; no L1 or subsequent phase runs; no mutations occur

### Requirement: Lock Acquisition

Setup SHALL acquire `state.lock` (per CLAUDE.md's per-user lock topology) for the duration of mutation phases (L1 through L8). The lock SHALL NOT be held during plan-pass probe execution (the plan pass is read-only). Concurrent `sudo sandbox setup` invocations under the same operator SHALL serialize on the lock; concurrent invocations under different operators on the same host SHALL each acquire their own per-user `state.lock` (no inter-operator serialization).

#### Scenario: Concurrent setup invocations under the same operator serialize
- **WHEN** operator `alice` runs `sudo sandbox setup` twice concurrently (same operator's `state.lock` file)
- **THEN** the second invocation blocks on `state.lock` until the first completes (or fails); the locks ARE NOT inter-operator (concurrent invocations under different operators run independently)

