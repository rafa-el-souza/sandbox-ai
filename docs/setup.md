# Setup

`sudo sandbox setup` internals and rationale — the design behind the idempotent host-bootstrap command. This doc sits at the *internals / rationale* altitude; the operator-facing walkthrough (first-time run, the `--update-runsc` lifecycle, the production integrity posture in operator terms, and a manual uninstall recipe) lives in [setup-guide.md](setup-guide.md).

`sudo sandbox setup` takes a fresh Linux host from "distro + uv-installed wheel" to "every `sandbox doctor` check green," and is safe to re-run any time the host drifts. It runs as root throughout (`os.geteuid() == 0`; refuses non-root with `sandbox setup must be run as root. Re-invoke as: sudo sandbox setup`). Operator-side state crosses via `pipe_cmd(<operator>)`; sandbox-user state crosses via `machinectl_cmd(...)` — setup's `src/core/setup/*.py` modules are the third allowlisted `machinectl_cmd` caller category (see [privilege-boundary.md](privilege-boundary.md)).

## Contents

- [Operator resolution](#operator-resolution)
- [Plan/apply two-pass UX](#planapply-two-pass-ux)
- [Phased ceremony](#phased-ceremony)
- [Content-aware probes](#content-aware-probes)
- [Reserved-namespace principle](#reserved-namespace-principle)
- [Binary-location split + Digest_Spec constraint](#binary-location-split--digest_spec-constraint)
- [runsc drift/update](#runsc-driftupdate)
- [Multi-operator by accumulation](#multi-operator-by-accumulation)
- [Inline sudoers user-spec vs. Cmnd_Alias](#inline-sudoers-user-spec-vs-cmnd_alias)
- [Production integrity posture](#production-integrity-posture)
- [Manual uninstall recipe](#manual-uninstall-recipe)

## Operator resolution

The operator is resolved by explicit precedence (no TTY heuristics):

1. `--operator <name>` — explicit override.
2. `$SUDO_USER`+`$SUDO_UID` consistency.
3. `$PKEXEC_UID`.
4. refuse.

## Plan/apply two-pass UX

The plan pass probes every phase (no mutations) with doctor-style markers (`✓ already correct`, `⊙ missing → will mutate`, `⚠ blocked`, `✗ verify-only failure → refuse`) and a `Summary: <A> already correct, <M> will mutate, <B> blocked, <R> refused` line. `--dry-run` runs only the plan pass. The apply pass is gated on the plan outcome:

- **Zero mutations** → no prompt, `Nothing to apply.`, exit 0.
- **≥1 refusal** → no prompt, refusal list, exit non-zero (apply never runs).
- **≥1 mutation, TTY, no `--yes`** → `Proceed with apply? [y/N]: ` (only `y`/`Y`/`yes`/`YES` proceeds).
- **≥1 mutation, non-TTY, no `--yes`** → refuse.
- **SIGINT anywhere** → `aborted by operator (SIGINT). No mutations applied.`, exit 130.

Apply continues past non-rollback failures (dependents marked BLOCKED-BY); L3a failure rolls back the sudoers drop-in.

## Phased ceremony

Phases are **named, not counted** — never re-introduce a brittle "N-phase" integer. They run in the named order:

| Phase | What it does |
|---|---|
| **L0** | identity/env (root + operator resolution; distro tier; required-binary check; `MACHINECTL_PATH` uniqueness assertion on the sudoers `secure_path` basis — inode-deduped so usrmerge symlink-aliases are one, genuinely-distinct binaries still refused). |
| **L1** | sysctl drop-in + ACL-FS/cgroup-v2 verify (L1 resolves no OS user). |
| **L2** | systemd-machined + sandbox useradd + `/etc/subuid`/`/etc/subgid` append-only + `sb-ws` groupadd + operator `usermod` (L2 does **not** install runsc). |
| **L2a** | `Delegate=yes` drop-in (split out of L1: its `user-<sandbox-uid>.service.d/` path is uid-scoped to the sandbox user L2 creates, so `depends_on=("l2",)`, ordered before L5). |
| **L5** | linger + rootless dockerd. |
| **L6** | `daemon.json` reserved-key merge + StartLimit-safe restart (`systemctl --user reset-failed docker.service` then `restart --no-block`, then a **runtime-aware** readiness poll until `docker info` lists the reserved runtime; the probe/reverify confirm the daemon's *loaded* runtime, not just the `daemon.json` file — do NOT "fix" a flaky restart with a blind retry: rapid restarts trip systemd's start-rate-limit and leave docker down). |
| **L6a** | runsc install (own phase). |
| **L6.5** | dispatcher compile+install. |
| **L7** | helper-image pre-pull. |
| **L3** | sudoers drop-in install + **L3a** per-op probe. |
| **L8** | fresh-session re-probe. |

**L3 is the last base-ceremony mutation phase and the only one that touches the privilege-boundary rule** — there is no permissive bootstrap rule before it, so a crash anywhere in L0..L7 leaves zero sudoers grant on disk, and an L3 crash is handled by L3a's rollback. Setup-as-root invokes `machinectl` directly; no permissive bootstrap rule is ever installed. L8 is verification, not mutation. The optional fapolicyd/AIDE integration phases run **after L8** with sticky opt-in (see [Production integrity posture](#production-integrity-posture)) and mutate only their own `/etc/fapolicyd/trust.d/` and `/etc/aide/aide.conf.d/` namespaces — they never touch the L3 rule, so the no-permissive-window property is scoped to the base ceremony's privilege-boundary rule.

> **Removed-phase history — the old L4 "operator state" phase.** An earlier L4 "operator state" phase was **removed**: setup runs as root, where `sandbox_ai_home()` resolves to `/root/.sandbox-ai` — invisible to the operator — so the per-operator `{config,state,instances,workspaces}` tree + `sandbox-ai.toml` are created **by `sandbox init` as the operator**, never by setup. Setup's only operator-readable artifact, the dispatcher manifest, therefore lives on the host plane alongside the binary, not under `sandbox_ai_home()`.

## Content-aware probes

Every phase whose mutation can drift across wheel upgrades renders expected state from current sources (the `core.dispatch.Op` enum, `BINARY_REGISTRY`/`IMAGE_REGISTRY` pins, the dispatcher source bundle derived from `core.dispatch.DISPATCH_SOURCE_ENTRIES`, the toml/daemon.json contents) and compares it to observed on-disk state; the act is skipped only on an exact match. This is why an idempotent re-run on a converged host completes in <5s with every phase `already correct`.

## Reserved-namespace principle

Setup writes ONLY to namespaces it owns and never edits, appends to, or overwrites any file or key outside the enumerable list (see [Manual uninstall recipe](#manual-uninstall-recipe) for the full list). Each drop-in carries a leading `# sandbox-ai managed — do not edit; rerun 'sudo sandbox setup'` comment. Hand-edits *outside* the owned namespace are never touched; hand-edits *inside* it are authoritatively overwritten by setup. `/etc/subuid`/`/etc/subgid` are append-only (flat-file shared territory; refuse to shrink an inadequate existing range).

## Binary-location split + Digest_Spec constraint

The dispatcher (`/usr/local/libexec/sandbox-ai/dispatch`, L6.5) and runsc (`/usr/local/libexec/sandbox-ai/runsc`, L6a) install root-owned mode `0755`, not on PATH (FHS § 4.7 `libexec/`), with `chattr +i` applied after install (cleared with `chattr -i` before any replace, re-applied after — see [runsc drift/update](#runsc-driftupdate)). This is the cheap compensating control for the unavailable `Digest_Spec`: the sudoers `Digest_Spec` (`sha512:<hash>`) crypto pin is **silently no-op on Debian-family hosts**, so the rendered rule deliberately contains no `Digest_Spec`. `chattr +i` does not provide crypto tamper resistance (root can clear the bit) but raises the bar for casual/automated tampering and adds an audit signal.

The remaining integrity story is doctor-visibility, not enforcement. The four doctor checks:

- `dispatcher_sha_drift` — on-disk binary sha vs. the `/usr/local/libexec/sandbox-ai/dispatcher.manifest.json` `compiled_sha512` + source-bundle sha (host-plane path alongside the binary); WARN on tamper or wheel-upgrade drift.
- `runsc_pinned_match` — on-disk runsc sha vs. `BINARY_REGISTRY["runsc"].sha512`; WARN on drift.
- `binary_integrity_posture` — informational dm-verity / IMA / fapolicyd / AIDE state.
- `setup_invariants` — owned-path/mode/ownership audit + machinectl-path stability + sudoers-rule content audit + sudo-version floor.

All four are WARN-not-FAIL by policy.

## runsc drift/update

L6a installs the pinned runsc if absent; on a re-run with a sha mismatch it does **not** auto-overwrite — it records `runsc version drift: installed sha <X>, pinned sha <Y>. To update: sudo sandbox setup --update-runsc` in the finalization summary. `sudo sandbox setup --update-runsc` re-runs **only** the L6a phase with `force=True` (`chattr -i` → atomic install of the pinned binary → `chattr +i`), bypassing the drift-skip. Setup never silently auto-updates runsc.

## Multi-operator by accumulation

Each operator runs `sudo sandbox setup` for themselves. Shared host state (sandbox user, `sb-ws` group, rootless dockerd, runsc, dispatcher binary, `/etc/subuid` entries) is **convergent** across operators — idempotent, written once, the same for everyone. Per-operator state (the `/etc/sudoers.d/sandbox-ai-machinectl-<operator>` drop-in, `<sandbox_ai_home()>/`, `sb-ws` group membership) **accumulates additively**: `alice` running setup installs `…-alice`; `bob` running setup later installs `…-bob` without disturbing alice's drop-in or the shared state. Concurrent invocations under the same operator serialize on that operator's per-user `state.lock`; invocations under different operators do not inter-serialize.

## Inline sudoers user-spec vs. Cmnd_Alias

The SUDO sudoers rule enumerates each dispatcher op as a `Cmnd_Spec` **inlined directly into the operator's user-spec** (`<operator> <hostname>=(root) NOPASSWD: NOSETENV: <spec>, \ …`) — NOT behind a shared `Cmnd_Alias`. Each spec is the full `MACHINECTL_PATH shell <user>@.host /bin/bash -c <dispatch>\ <op>[\ *]` prefix (backslash-escaped whitespace, zero `"` chars, no `Digest_Spec`, rendered from `core.dispatch.Op`). The inline form is load-bearing for multi-operator-by-accumulation: `Cmnd_Alias` names share a single global `/etc/sudoers.d/` namespace, so a per-operator `Cmnd_Alias SANDBOX_OPS` collided with every other operator's drop-in (`duplicate Cmnd_Alias`); inlining keeps each operator's user-spec independent.

## Production integrity posture

The integration phases write fixed-namespace artifacts at mode `0644` root:root: the fapolicyd trust file `/etc/fapolicyd/trust.d/sandbox-ai.trust` (one `<path> <size> <sha256>` line per managed binary, `# sandbox-ai managed` header) and the AIDE conf `/etc/aide/aide.conf.d/sandbox-ai.conf` (the two-line `… dispatch NORMAL` / `… runsc NORMAL` snippet + `# sandbox-ai managed` header). They run after L8 with sticky opt-in and mutate only their own namespaces — they never touch the L3 rule, and an integration-phase failure never rolls back the L0..L8 base ceremony, so the no-permissive-window property stays scoped to the base ceremony's privilege-boundary rule.

Operator-facing integrity posture (the cheapest→strongest tier layering, the doctor checks, and the fapolicyd/AIDE/dm-verity/IMA detection): see [setup-guide.md](setup-guide.md#production-integrity-posture).

## Manual uninstall recipe

Operator uninstall steps: see [setup-guide.md](setup-guide.md#manual-uninstall-recipe). Setup writes ONLY to the namespaces it owns (the spec's "Reserved Namespace File Ownership" enumerable list), so removing exactly that list fully uninstalls; the recipe walks it manually.
