# Baseline validation matrix — `sandbox` command × distro × mode

The **reference baseline**'s current-state verdict snapshot: `sandbox` command/probe × distro × mode,
PASS/FAIL. This is a *living* current-state reference, refreshed at durable (shipped) milestones — not a
permanent freeze (it is held fixed only for the duration of an in-flight change's validation; git `HEAD` is
that freeze). Verdicts are generated from the per-run `*.summary.json` companions via `vmlease summarize`;
each mode cites its source run(s) + date below. ✅ PASS · ❌ FAIL.

(Rows are per `sandbox` command (single-operator modes) or per operator-phase probe (multi-operator modes);
a battery may probe one command more than once — e.g. `status` stopped + running — and the per-mode totals
count every probe run, so they exceed the visible row count.)

**All four bundles are green across the full 4-distro matrix as of C-013 (2026-06-17)** — debian-13 is
included now that the Hetzner trixie-backports mirror recovered (it was excluded in G15.2; see *Sources*).

## op-rootless (`baseline-op-rootless`, 15 probes)

| command | ubuntu | debian | fedora | arch |
|---|:--:|:--:|:--:|:--:|
| `setup` | ✅ | ✅ | ✅ | ✅ |
| `init` | ✅ | ✅ | ✅ | ✅ |
| `doctor` | ✅ | ✅ | ✅ | ✅ |
| `status` (stopped + running) | ✅ | ✅ | ✅ | ✅ |
| `workspace list` (×2) | ✅ | ✅ | ✅ | ✅ |
| `workspace add` | ✅ | ✅ | ✅ | ✅ |
| `workspace rename` | ✅ | ✅ | ✅ | ✅ |
| `start` [^runtime] | ✅ | ✅ | ✅ | ✅ |
| `attach` [^attach] | ✅ | ✅ | ✅ | ✅ |
| `stop` | ✅ | ✅ | ✅ | ✅ |
| `workspace remove` | ✅ | ✅ | ✅ | ✅ |
| `workspace restore` | ✅ | ✅ | ✅ | ✅ |
| `destroy` | ✅ | ✅ | ✅ | ✅ |

## separate-user · SUDO (`baseline-separate-user-sudo`, 17 probes)

All green as of C-009 (`sudo systemd-run --pipe` crossing) + C-010 (`dispatch fwd` headless attach). The
`core-running` and `preflight-crossing-count` rows are the C-009/C-010 verification probes folded into the
reference baseline.

| command | ubuntu | debian | fedora | arch |
|---|:--:|:--:|:--:|:--:|
| `setup` | ✅ | ✅ | ✅ | ✅ |
| `init` | ✅ | ✅ | ✅ | ✅ |
| `doctor` | ✅ | ✅ | ✅ | ✅ |
| `status` (stopped + running) | ✅ | ✅ | ✅ | ✅ |
| `workspace list` (×2) | ✅ | ✅ | ✅ | ✅ |
| `workspace add` | ✅ | ✅ | ✅ | ✅ |
| `workspace rename` | ✅ | ✅ | ✅ | ✅ |
| `start` [^runtime] | ✅ | ✅ | ✅ | ✅ |
| `core-running` (non-PTY `sudo -u` read) | ✅ | ✅ | ✅ | ✅ |
| `preflight-crossing-count` (8→2 collapse) | ✅ | ✅ | ✅ | ✅ |
| `attach` [^attach] | ✅ | ✅ | ✅ | ✅ |
| `stop` | ✅ | ✅ | ✅ | ✅ |
| `workspace remove` | ✅ | ✅ | ✅ | ✅ |
| `workspace restore` | ✅ | ✅ | ✅ | ✅ |
| `destroy` | ✅ | ✅ | ✅ | ✅ |

## multi-operator · op-rootless first (`multi-operator-oprootless-first`, 9 probes)

C-013 F-070 / F-071 on a real multi-operator host, with operator-rootless provisioning first. Rows are the
operator-phase probes (not single `sandbox` commands).

| probe | ubuntu | debian | fedora | arch |
|---|:--:|:--:|:--:|:--:|
| `op1-setup` (op1 op-rootless setup; capture subid + sb-ws gid) | ✅ | ✅ | ✅ | ✅ |
| `op2-setup` (second operator setup) | ✅ | ✅ | ✅ | ✅ |
| `op2-bridge-gid-assert` — **F-070** (per-operator `sb-ws` gid in op2's subgid range) | ✅ | ✅ | ✅ | ✅ |
| `op2-doctor` (no bridge-gid-out-of-range violation) | ✅ | ✅ | ✅ | ✅ |
| `op2-start` (op2 core under op2's rootless docker) | ✅ | ✅ | ✅ | ✅ |
| `op3f-setup` (entry-less operator setup) | ✅ | ✅ | ✅ | ✅ |
| `op3f-loud-assert` (fails LOUD with `NoSubgidRangeError`) | ✅ | ✅ | ✅ | ✅ |
| `mixed-subid-overlap-and-guard` — **F-071** (op4 subid disjoint + L2 guard) | ✅ | ✅ | ✅ | ✅ |
| `teardown` (best-effort op2/op3f/op4 cleanup) | ✅ | ✅ | ✅ | ✅ |

## multi-operator · separate-user first (`multi-operator-separate-user-first`, 8 probes)

F-070 / F-071 with the separate-user operator provisioning first.

| probe | ubuntu | debian | fedora | arch |
|---|:--:|:--:|:--:|:--:|
| `op1-setup` (op1 separate-user setup) | ✅ | ✅ | ✅ | ✅ |
| `op2-setup` (second operator setup) | ✅ | ✅ | ✅ | ✅ |
| `op2-bridge-gid-assert` — **F-070** | ✅ | ✅ | ✅ | ✅ |
| `op2-doctor` | ✅ | ✅ | ✅ | ✅ |
| `op2-start` | ✅ | ✅ | ✅ | ✅ |
| `op3f-setup` (entry-less operator setup) | ✅ | ✅ | ✅ | ✅ |
| `op3f-loud-assert` — **F-071** loud-refusal | ✅ | ✅ | ✅ | ✅ |
| `teardown` | ✅ | ✅ | ✅ | ✅ |

## Sources + totals

Each bundle's ubuntu/fedora/arch and debian cells come from distinct runs (debian was re-added 2026-06-17
once the trixie-backports mirror recovered). All runs gated on `vmlease summarize` exit 0 with **per-host
probe count > 0** (guarding the F-075 hollow-green hole — a zero-probe ERROR host that `summarize` alone
would pass).

- **op-rootless** — `vmlease-c013r-base-oprootless-2026-06-16T19` (ubuntu/fedora/arch, 2026-06-16) +
  `vmlease-c013-debian-check-2026-06-17T1145` (debian, 2026-06-17) — {FAIL: 0, PASS: 60} over 15 probes × 4.
- **separate-user · SUDO** — `vmlease-c013r-base-sepuser-2026-06-16T19` (ubuntu/fedora/arch, 2026-06-16) +
  `vmlease-c013-debian-sepuser-2026-06-17T1206` (debian, 2026-06-17) — {FAIL: 0, PASS: 68} over 17 × 4.
- **multi-operator · op-rootless first** — `vmlease-c013f-moronly-2026-06-16T21` (fedora/arch, 2026-06-16) +
  `vmlease-c013-ubuntu-multi-oprootless-2026-06-17T1302` (ubuntu, 2026-06-17) +
  `vmlease-c013-debian-multi-oprootless-fixed-2026-06-17T1233` (debian, 2026-06-17) — {FAIL: 0, PASS: 24,
  PASS_NO_ASSERTIONS: 12} over 9 × 4. The ubuntu + debian runs were re-run on the vmlease decode fix
  (`fix/lenient-decode-ssh-stdout`); the prior ubuntu "green" was a hollow zero-probe artifact of that bug
  (F-075) masking a flaky op4 setup — re-validated real here.
- **multi-operator · separate-user first** — `vmlease-c013r-multi-sepuser-2026-06-16T19` (ubuntu/fedora/arch,
  2026-06-16) + `vmlease-c013-debian-multi-sepuser-2026-06-17T1206` (debian, 2026-06-17) — {FAIL: 0,
  PASS: 20, PASS_NO_ASSERTIONS: 12} over 8 × 4.

## Reading the matrix

- **op-rootless / separate-user · SUDO** — green on every command × distro. separate-user green as of C-009
  (`sudo systemd-run --pipe`, fixing the apt `start`/`doctor` F-063 empty-crossing) + C-010 (`dispatch fwd`
  headless `attach`, fixing F-060 on all 4).
- **multi-operator** — F-070 (`op2-bridge-gid-assert`) + F-071 (`op3f-loud-assert`,
  `mixed-subid-overlap-and-guard`) green on all 4 distros: a second operator's `sb-ws-<op>` bridge gid lands
  in *that operator's* subgid range, and a fourth operator's subid range does not silently overlap op1's.
  These validate the **allocation mechanisms**. A multi-operator **runtime** gap on a specific host (an Nth
  operator's rootless dockerd failing to come up — F-074 family) is a separate follow-up, not a C-013
  deliverable.

[^runtime]: A green `start` asserts the core container is **running** (`docker ps` shows `-smoke-core-1`
    up, no crossing error) — it does not re-assert the OCI runtime is `sandbox-ai-runsc`. gVisor isolation
    on rootless docker was separately root-caused + real-host-validated in E-005 (F-057 fixed `d6017d1`,
    F-059 fixed `435d423`); this matrix trusts that fix rather than re-probing the runtime each run.

[^attach]: The `attach` probe's **primary** gate is reaching the agent shell at `/workspaces/main`
    (RWHOAMI/RPWD are observational). It drives a PTY via `script(1)` (installed by `[prep.packages]`); a
    silently-absent `script(1)` would skip-with-reason rather than fail. On debian-13 the cell depends on
    the `tlog` prep step providing `tlog-rec` (not apt-packaged on trixie — the advisory-vs-hard-dep posture
    is an open backlog item). The core-image terminfo gap (full-screen TUIs under exotic `TERM`) is
    pre-existing and not gated here.
