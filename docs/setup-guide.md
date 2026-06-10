# sandbox-ai Setup Guide

Operator-facing walkthrough for `sudo sandbox setup` — the idempotent command that takes a fresh Linux host from "distro + uv-installed wheel" to "every `sandbox doctor` check green." This guide covers the first-time run, the `--update-runsc` lifecycle, the production integrity posture, and a manual uninstall recipe.

> Reference for the authoritative contract: `openspec/specs/sandbox-setup/spec.md`. Internal design rationale: `openspec/changes/sandbox-setup/design.md`. This guide is the operator-facing distillation.

## Prerequisites

- A supported Linux distro (see [Distro support tiers](#distro-support-tiers)).
- The `sandbox` CLI installed by the operator (`uv pip install sandbox-ai` or `uv tool install sandbox-ai`) — this lands on the operator's PATH.
- `sudo` access (setup runs as root throughout).
- Standard distro tooling on PATH: `sudo`, `machinectl`, `setfacl`, `getfacl`, `rsync`, `loginctl`, `useradd`, `usermod`, `groupadd`, `visudo`, `chattr`, `sysctl`, `tlog-rec`. L0 checks each and fails with a copy-pasteable, distro-conditional install command if any is missing (e.g. `tlog-rec`: `sudo apt install tlog` on Debian/Ubuntu, `sudo dnf install tlog` on Fedora/RHEL, `paru -S tlog` on Arch).
- **Rootless Docker host-prep (varies by distro; not yet auto-checked).** L5 runs `dockerd-rootless-setuptool.sh install` as the unprivileged sandbox user, which has prerequisites L0 does **not** yet verify (a forthcoming change promotes these to early L0 checks — until then a missing one surfaces only as a mid-apply L5 failure). They must be reachable on a **system** PATH the sandbox user inherits — the crossing's PATH is the systemd default `/usr/*bin`, **not** any user's `~/bin`:
  - **Debian/Ubuntu:** `sudo apt install docker-ce-rootless-extras uidmap` (`uidmap` supplies `newuidmap`/`newgidmap`; `docker-ce` brings the compose v2 plugin). On **Debian 13 (trixie)** `tlog` is not packaged — build it from source (`github.com/Scribery/tlog`: deps incl. `libutempter-dev`, then `autoreconf -i -f && ./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var && make && sudo make install`); the `apt install tlog` hint above applies only to Ubuntu and Debian ≤12.
  - **Fedora/RHEL:** `sudo dnf install docker-ce-rootless-extras` (`shadow-utils` supplies `newuidmap`/`newgidmap`).
  - **Arch:** the `docker` package ships no system-PATH `dockerd-rootless-setuptool.sh` — install the upstream **static** docker + rootless-extras bundle (`download.docker.com/linux/static/stable/<arch>/`) into `/usr/local/bin`, plus `sudo pacman -S docker-compose slirp4netns fuse-overlayfs shadow`, and load `nf_tables` (`sudo modprobe nf_tables`; persist via `/etc/modules-load.d/`).

The `sandbox` CLI is **operator territory** (operator-owned, on PATH, typed directly). The dispatcher and runsc binaries setup installs are **setup territory** (`/usr/local/libexec/sandbox-ai/{dispatch,runsc}`, root-owned, not on PATH, `chattr +i`). Do not move binaries between territories — the split is a load-bearing trust boundary.

## First-time setup

### 1. Preview the plan (no mutations)

```
sudo sandbox setup --dry-run
```

This runs the **plan pass** only: every phase probes the host without writing anything. Output is doctor-style:

- `✓ already correct` — phase needs no action.
- `⊙ missing → will mutate` — phase will act during the apply pass.
- `⚠ blocked → reason + remediation` — a dependency failed; the phase will be BLOCKED-BY.
- `✗ verify-only failure → refuse` — an unfixable precondition (e.g. cgroup v2 inactive, ACL FS unsupported, unrecognized distro); apply will not run until you remediate.

The plan ends with a one-line summary:

```
Summary: <A> already correct, <M> will mutate, <B> blocked, <R> refused
```

`A + M + B + R` equals the total phase count for this invocation (including any sticky-opt-in integration phases). The host is byte-identical after `--dry-run` as before; exit code 0.

### 2. Apply

```
sudo sandbox setup
```

Setup re-runs the plan pass, then gates the apply pass:

- **Zero mutations** (converged host): no prompt; emits `Nothing to apply. Setup is complete.`; exit 0.
- **≥1 refusal**: no prompt; emits the refusal list with remediation hints; exit non-zero; the apply pass never runs.
- **≥1 mutation, TTY, no `--yes`**: emits the plan, then `Proceed with apply? [y/N]: `. Type `y` / `Y` / `yes` / `YES` to proceed; anything else (or just Enter) aborts with `aborted by operator (n). No mutations applied.` and exit 0.
- **≥1 mutation, non-TTY (CI/automation)**: requires `--yes`, otherwise refuses with `non-interactive context requires --yes flag to apply mutations`.
- **Ctrl-C** at any point: `aborted by operator (SIGINT). No mutations applied.` to stderr, exit 130.

For non-interactive runs:

```
sudo sandbox setup --yes
```

`--yes` skips both the apply confirm prompt and the untested-distro prompt (warnings are still emitted).

### 3. The phased ceremony

The apply pass executes phases in this named order (the order is named, not counted — sub-phases L6a/L6.5 are stable identifiers; do not expect a fixed "N-phase" count):

| Phase | What it does |
|---|---|
| **L0** | Root assertion; operator resolution (`--operator` → `$SUDO_USER`+`$SUDO_UID` → `$PKEXEC_UID` → refuse); distro-tier classification; required-binary check; `MACHINECTL_PATH` uniqueness assertion on the sudoers `secure_path` basis. |
| **L1** | `/etc/sysctl.d/49-sandbox-ai.conf` (+ `sysctl -w`); `Delegate=yes` drop-in scoped to `user-<sandbox-uid>.service.d/`; verify-only ACL-FS support + cgroup-v2 hierarchy. |
| **L2** | systemd-machined enable+start; `useradd` for the sandbox user; `/etc/subuid`/`/etc/subgid` append-only-when-safe; `groupadd sb-ws` at an autodetected gid in the subuid range; `usermod -aG sb-ws <operator>`. (Does **not** install runsc.) |
| **L5** | `loginctl enable-linger <sandbox-user>`; rootless dockerd install via machinectl. |
| **L6** | Merge `runtimes["sandbox-ai-runsc"]` into `~<sandbox-user>/.config/docker/daemon.json` (preserving the operator's other runtimes); conditional `systemctl --user restart docker` + readiness poll. |
| **L6a** | runsc install (own phase): install the pinned binary if absent; on drift, mention it in the summary without auto-overwriting; `chattr +i`. |
| **L6.5** | Compile the dispatcher (offline, in a pinned `golang` container — `go test ./...` for Python↔Go parity runs first, so a fixture drift fails the compile) and install it; `chattr +i`; write the root-owned `0644` `/usr/local/libexec/sandbox-ai/dispatcher.manifest.json` (host plane, alongside the binary, so every operator's `sandbox doctor` can read it — F-021). |
| **L7** | Pre-pull the pinned helper image (`docker pull busybox:musl@<digest>`). |
| **L3** | Install the sudoers (or polkit) privilege-boundary drop-in; `visudo -cf` validation; **L3a** per-op probe of every dispatcher op. |
| **L8** | Fresh-session re-probe: verify the operator's group set now includes the `sb-ws` gid; verify machinectl is reachable through the new rule. |

**L3 is the last base-ceremony mutation phase and the only one that touches the privilege-boundary rule.** No sudoers/polkit grant exists on disk at any point before L3, so a crash anywhere in L0..L7 leaves the host with zero sandbox-ai passwordless boundary crossing (the deliberate "no permissive bootstrap rule" property). An L3 crash is handled by L3a's rollback (the just-installed drop-in is removed). L8 is verification, not mutation.

Apply continues past non-rollback failures: a failed phase is marked FAIL, its dependents BLOCKED-BY, and independent phases still run, so you see all reachable failures in one run. The finalization summary reports pass/fail/skipped counts plus remediation pointers.

### 4. Verify

```
sandbox doctor
```

On a freshly-converged host, `sandbox doctor` should return green. A converged-host re-run of `sudo sandbox setup` completes in under 5 seconds with every phase `already correct`.

## Distro support tiers

L0 classifies the host from `/etc/os-release` into one of three tiers:

- **Validated** — Debian, Ubuntu. Proceeds silently.
- **Untested** — Fedora, RHEL, CentOS, Arch, Manjaro. Same phase logic (branching only on distro-specific kernel knobs and package commands). L0 emits a non-blocking `⚠ Untested distro` warning. In a TTY it prompts `Press Enter to continue, Ctrl-C to abort`; in non-TTY or with `--yes` it logs the warning without prompting.
- **Unrecognized** — everything else (Alpine, NixOS, openSUSE, Gentoo, Void, …). L0 emits `✗ Unsupported distro` and refuses; no subsequent phase runs; no mutations occur. Use this guide's manual ceremony references to integrate by hand.

On Debian-family hosts L1 writes `kernel.unprivileged_userns_clone=1`; on other kernels that knob does not exist and is omitted (`user.max_user_namespaces` is written universally).

## Multi-operator hosts

Each operator runs `sudo sandbox setup` for themselves. Shared host state — the sandbox user, the `sb-ws` group, rootless dockerd, runsc, the dispatcher binary, `/etc/subuid` entries — is **convergent**: idempotent, written once, identical for everyone. Per-operator state **accumulates additively**:

- `alice` runs setup → `/etc/sudoers.d/sandbox-ai-machinectl-alice`, alice's `<sandbox_ai_home()>/`, alice in `sb-ws`.
- `bob` runs setup later → `/etc/sudoers.d/sandbox-ai-machinectl-bob`, bob's `<sandbox_ai_home()>/`, bob in `sb-ws` — alice's drop-in and state untouched.

Both can independently invoke the orchestrator without password prompts. Concurrent invocations under the same operator serialize on that operator's per-user `state.lock`; invocations under different operators do not inter-serialize.

## The `--update-runsc` lifecycle

Setup never silently changes the runsc version. L6a's behavior on each `sudo sandbox setup`:

- **Absent** → download from the pinned URL, verify sha512 against `BINARY_REGISTRY["runsc"]`, install at `/usr/local/libexec/sandbox-ai/runsc` mode 0755 root:root, `chattr +i`.
- **Present, sha matches the pin** → skip.
- **Present, sha differs from the pin** → do **not** overwrite. The finalization summary reports:

  ```
  runsc version drift: installed sha <X>, pinned sha <Y>. To update: sudo sandbox setup --update-runsc
  ```

  `sandbox doctor`'s `runsc_pinned_match` check reports the same drift.

To apply the pinned version after acknowledging the drift:

```
sudo sandbox setup --update-runsc
```

This runs **only** the L6a phase with `force=True`: `chattr -i` the existing binary → atomic install of the pinned version → `chattr +i`. All other phases are skipped. The immutable attribute is observable both before and after the update.

A future config setting (`[setup] auto_update_runsc = true`) for silent auto-update is out of scope for the current change.

## Production integrity posture

The dispatcher/runsc tamper model layers from cheapest to strongest. The **F-003** constraint is load-bearing: the sudoers `Digest_Spec` crypto pin is silently a no-op on Debian-family hosts, so the rendered sudoers rule deliberately contains no `Digest_Spec`. The compensating controls:

- **`chattr +i`** (automatic, L6a/L6.5) — defense-in-depth against casual/automated tampering plus an audit signal. Root can clear the bit; this is not crypto tamper resistance.
- **Doctor checks** — `dispatcher_sha_drift` (on-disk binary sha vs. the manifest's `compiled_sha512` + source-bundle sha; WARN on tamper or wheel-upgrade drift), `runsc_pinned_match` (on-disk runsc sha vs. the pin; WARN on drift), `setup_invariants` (owned-path/mode/ownership audit + machinectl-path stability + sudoers-rule content audit + sudo-version floor). All WARN, never FAIL.
- **fapolicyd** (opt-in) — `sudo sandbox setup --enable-fapolicyd-integration` writes `/etc/fapolicyd/trust.d/sandbox-ai.trust` (one `<path> <size> <sha256>` line per managed binary, `# sandbox-ai managed` header) and runs `fapolicyd-cli --update`. Refuses with a distro install hint if fapolicyd or `/etc/fapolicyd/trust.d/` is absent; warns if fapolicyd is installed but not running.
- **AIDE** (opt-in) — `sudo sandbox setup --enable-aide-integration` writes `/etc/aide/aide.conf.d/sandbox-ai.conf` (the `… dispatch NORMAL` / `… runsc NORMAL` snippet). Setup never runs `aide --init` (a 10+ minute filesystem walk); on first install with `/var/lib/aide/aide.db` absent it appends an `aide --init` prompt to the finalization summary. Schedule periodic `aide --check` runs yourself (e.g. a daily cron at off-peak hours).
- **dm-verity / IMA-appraise** — operator-configured (kernel cmdline + boot config), beyond setup's reach. Setup does not bootstrap these.

`sandbox doctor`'s `binary_integrity_posture` check probes dm-verity (`/proc/cmdline` + `dmsetup status`), IMA (`/sys/kernel/security/ima/policy`), fapolicyd (`systemctl is-active` + `fapolicyd-cli --check-status`), and AIDE (`which aide` + `/var/lib/aide/aide.db`), and reports structured state. It always PASSes (informational) — it detects and reports, it does not enforce or bootstrap.

**Sticky opt-in.** Once you enable an integration, every subsequent `sudo sandbox setup` auto-includes that phase (its flag is no longer needed — the phase auto-includes when its owned drop-in exists on disk). So `--update-runsc` (or any dispatcher re-compile) on a host with sticky integration cascades a trust-file refresh with the new sha — no window of inconsistency. An integration-phase failure does not roll back the L0..L8 base ceremony; fix the underlying issue and re-run.

**Auth mode is an explicit setup input — and POLKIT is fenced in this version.** `sudo sandbox setup` takes `--machinectl-auth {sudo|polkit}`; the effective mode is the flag if given, else the operator toml's `machinectl_authentication` if a toml exists, else the SUDO default. **Setup currently supports SUDO only.** If POLKIT is selected — via the flag, or an operator toml that already requests it — setup **refuses before touching the host** (no plan pass, no mutation) with a pointer to this section and the follow-on work. The reason is concrete: setup's per-op verification phases (L3a/L8) probe SUDO-only, so a polkit rule setup wrote could not be verified and would be rolled back — shipping a half-wired polkit path is worse than fencing it. An explicit `--machinectl-auth sudo` overrides a stale polkit toml (and `sandbox doctor`'s `setup_invariants` then WARNs on the lingering toml/rule disagreement). To run polkit today, configure the rule manually (step 2 of the uninstall recipe shows its path) and seed your toml with `machinectl_authentication = "polkit"`; full setup-driven polkit support (auth-aware L3a/L8 + live `V9d-polkit-e2e` validation) is a tracked follow-on change.

**SUDO vs POLKIT asymmetry — a different security model, not just a different mechanism.** In SUDO auth mode the sudoers rule enumerates each dispatcher op individually (per-op `Cmnd_Spec` narrowing on top of the application-layer discipline) — **the dispatcher *is* a privilege boundary**: the operator's NOPASSWD grant is exactly those twelve ops with their arg shapes, nothing else. In POLKIT auth mode the `org.freedesktop.machine1.shell` action cannot inspect the invoked argv, so the polkit rule is an **action-level** grant ("operator may `machinectl shell` into the sandbox user *for any command*") and per-op narrowing lives **only** at the application layer (the orchestrator only calls `core.dispatch`, enforced by a convention meta-test). In POLKIT mode the dispatcher is therefore a **convenience + integrity layer over a fully-trusted operator→sandbox shell grant, not a privilege boundary** — a legitimate posture for a single trusted operator on their own box, but a *different security model*. Choose it deliberately; it is not a drop-in alternative to SUDO. This is a documented property of the polkit action surface, not a defect.

## Manual uninstall recipe

There is no `sandbox setup --uninstall` yet (a future change will automate this). Setup writes ONLY to the namespaces it owns, so removing exactly the enumerable owned list **per the spec's "Reserved Namespace File Ownership" requirement** fully uninstalls sandbox-ai. Walk it manually:

1. `rm /etc/sudoers.d/sandbox-ai-machinectl-<operator>` — sudoers drop-in (SUDO mode), one per operator.
2. `rm /etc/polkit-1/rules.d/49-sandbox-ai-machinectl.rules` — polkit drop-in (POLKIT mode).
3. `rm /etc/sysctl.d/49-sandbox-ai.conf` — kernel sysctl drop-in.
4. `rm /etc/systemd/system/user-<sandbox-uid>.service.d/sandbox-ai-delegate.conf` — systemd `Delegate=yes` drop-in; then `systemctl daemon-reload`.
5. `chattr -i /usr/local/libexec/sandbox-ai/dispatch /usr/local/libexec/sandbox-ai/runsc` then `rm -rf /usr/local/libexec/sandbox-ai/` — the immutable binary directory (dispatcher + runsc) plus the root-owned `dispatcher.manifest.json` that lives alongside them.
6. Remove the `runtimes["sandbox-ai-runsc"]` key from `~<sandbox-user>/.config/docker/daemon.json` — reserved key only; leave the operator's other `runtimes["..."]` keys and the rest of the file intact.
7. `rm -rf <sandbox_ai_home()>/` — per-operator state (`{config,state,instances,workspaces}`), one tree per operator. (This tree is created by `sandbox init` as the operator, not by setup; the dispatcher manifest moved to the libexec dir in step 5.)
8. Remove the append-only `<sandbox-user>` lines from `/etc/subuid` + `/etc/subgid` if no longer needed (shared flat-file territory — remove only the sandbox-user entries by hand).
9. `rm /etc/fapolicyd/trust.d/sandbox-ai.trust` then `fapolicyd-cli --update` — **only if** fapolicyd integration was enabled (optional owned path).
10. `rm /etc/aide/aide.conf.d/sandbox-ai.conf` — **only if** AIDE integration was enabled (optional owned path); the operator's next `aide --check`/`aide --update` reflects the removal.
11. Optionally `userdel <sandbox-user>`, `groupdel sb-ws`, and drop the operator's `sb-ws` membership if the sandbox user / bridge group are no longer wanted — these are shared host state, not removed automatically since other operators may still depend on them.

This list is faithful to and complete against the spec's "Reserved Namespace File Ownership" enumerable list (including the optional fapolicyd/AIDE drop-ins and the dispatcher manifest). Setup writes nothing outside it, so this recipe is exhaustive.
