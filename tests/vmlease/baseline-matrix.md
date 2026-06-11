# Baseline validation matrix — `sandbox` command × distro × mode

The **reference baseline**'s current-state verdict snapshot: `sandbox` command × distro × mode, PASS/FAIL.
This is a *living* current-state reference, refreshed at durable (shipped) milestones — not a permanent
freeze (it is held fixed only for the duration of an in-flight change's validation; git `HEAD` is that
freeze). Verdicts are generated from the per-run `*.summary.json` companions via `vmlease summarize`; each
mode cites its source run + date below. ✅ PASS · ❌ FAIL.

(Rows are per `sandbox` command; a battery may probe one command more than once — e.g. `status`,
`workspace list` — and the per-mode totals count every probe run, so they exceed the visible row count.)

## op-rootless

| command | ubuntu | debian | fedora | arch |
|---|:--:|:--:|:--:|:--:|
| `setup` | ✅ | ✅ | ✅ | ✅ |
| `doctor` | ✅ | ✅ | ✅ | ✅ |
| `init` | ✅ | ✅ | ✅ | ✅ |
| `status` | ✅ | ✅ | ✅ | ✅ |
| `workspace list` | ✅ | ✅ | ✅ | ✅ |
| `workspace add` | ✅ | ✅ | ✅ | ✅ |
| `workspace rename` | ✅ | ✅ | ✅ | ✅ |
| `start` | ✅ | ✅ | ✅ | ✅ |
| `attach` | ✅ | ✅ | ✅ | ✅ |
| `stop` | ✅ | ✅ | ✅ | ✅ |
| `workspace remove` | ✅ | ✅ | ✅ | ✅ |
| `workspace restore` | ✅ | ✅ | ✅ | ✅ |
| `destroy` | ✅ | ✅ | ✅ | ✅ |

## separate-user · SUDO

All green as of C-009 (`sudo systemd-run --pipe` crossing) + C-010 (`dispatch fwd` headless attach). The
`core-running` and `preflight-crossing-count` rows are the C-009/C-010 verification probes folded into the
reference baseline.

| command | ubuntu | debian | fedora | arch |
|---|:--:|:--:|:--:|:--:|
| `setup` | ✅ | ✅ | ✅ | ✅ |
| `doctor` | ✅ | ✅ | ✅ | ✅ |
| `init` | ✅ | ✅ | ✅ | ✅ |
| `status` | ✅ | ✅ | ✅ | ✅ |
| `workspace list` | ✅ | ✅ | ✅ | ✅ |
| `workspace add` | ✅ | ✅ | ✅ | ✅ |
| `workspace rename` | ✅ | ✅ | ✅ | ✅ |
| `start` | ✅ | ✅ | ✅ | ✅ |
| `core-running` | ✅ | ✅ | ✅ | ✅ |
| `preflight-crossing-count` | ✅ | ✅ | ✅ | ✅ |
| `attach` | ✅ | ✅ | ✅ | ✅ |
| `stop` | ✅ | ✅ | ✅ | ✅ |
| `workspace remove` | ✅ | ✅ | ✅ | ✅ |
| `workspace restore` | ✅ | ✅ | ✅ | ✅ |
| `destroy` | ✅ | ✅ | ✅ | ✅ |

## separate-user · POLKIT

| command | ubuntu | debian | fedora | arch |
|---|:--:|:--:|:--:|:--:|
| `setup` | ✅ | ❌ | ✅ | ✅ |
| `doctor` | ❌ | ❌ | ❌ | ❌ |
| `init` | ❌ | ❌ | ❌ | ❌ |
| `status` | ❌ | ❌ | ❌ | ❌ |
| `workspace list` | ❌ | ❌ | ❌ | ❌ |
| `workspace add` | ❌ | ❌ | ❌ | ❌ |
| `workspace rename` | ❌ | ❌ | ❌ | ❌ |
| `start` | ❌ | ❌ | ❌ | ❌ |
| `attach` | ❌ | ❌ | ❌ | ❌ |
| `stop` | ❌ | ❌ | ❌ | ❌ |
| `workspace remove` | ❌ | ❌ | ❌ | ❌ |
| `workspace restore` | ❌ | ❌ | ❌ | ❌ |
| `destroy` | ❌ | ❌ | ❌ | ❌ |

## Sources + totals

- **op-rootless** — `vmlease-e005-base-oprootless-v3-20260609T134818Z.summary.json` (2026-06-09) — {FAIL: 0, PASS: 64} — battery unchanged since (no C-009/C-010 delta touches the op-rootless path).
- **separate-user · SUDO** — `vmlease-c010acc1-20260610T190646Z.summary.json` (2026-06-10) — {FAIL: 0, PASS: 72} — the refreshed reference baseline (C-009 + C-010 deltas) run green across all 4 distros.
- **separate-user · POLKIT** — `vmlease-e005-base-su-polkit-20260609T144922Z.summary.json` (2026-06-09) — {FAIL: 57, PASS: 15} — battery unchanged; the reds are a durable truth (see below).

## Reading the matrix

- **op-rootless:** green on every command × distro — the no-privilege-crossing mode (apt/CI). No C-009/C-010 delta applies.
- **separate-user · SUDO:** **green across the board.** C-009 moved the SUDO-mode dispatcher-op crossing from the `machinectl` PTY (which empty-crossed on apt — F-063) to `sudo systemd-run --pipe`, flipping the apt `start`/`doctor` cells; C-010 routed `attach`'s `/fwd` through the dispatcher so the per-op sudoers `Cmnd_Spec` authorizes it headlessly (F-060), flipping `attach` on all 4. `core-running` (non-PTY `sudo -u` read) and `preflight-crossing-count` (8→2 burst-collapse) are the verification probes that pin those fixes.
- **separate-user · POLKIT:** red is the **durable** state, not a pending bug. `setup` correctly refuses to install a polkit rule (it runs as root and crosses via sudo); and even with a genuine `machine1.shell` rule + polkitd running, every crossing op needs interactive `manage-units` authorization, which is **headless-blocked** by design (F-060) — POLKIT-mode crossings require an interactive agent. (debian also lacks `polkit.service` on the minimal image, hence its `setup` ❌.)
