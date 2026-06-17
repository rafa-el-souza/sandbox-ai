# Baseline validation matrix — `sandbox` command × distro × mode

The **reference baseline**'s current-state verdict snapshot: `sandbox` command/probe × distro × mode,
PASS/FAIL. This is a *living* current-state reference, refreshed at durable (shipped) milestones — not a
permanent freeze (it is held fixed only for the duration of an in-flight change's validation; git `HEAD` is
that freeze). Verdicts are generated from the per-run `*.summary.json` companions via `vmlease summarize`;
each mode cites its source run(s) + date below. **✅ PASS · ❌ FAIL · ⊘ known-fail** (a real failure that is
tracked, accepted, and deliberately out of the current change's scope — an `xfail`; *not* green, *not* a
regression; see the cited finding).

(Rows are per `sandbox` command (single-operator modes) or per operator-phase outcome (multi-operator modes);
a battery may probe one command more than once — e.g. `status` stopped + running — and a single probe may
contribute more than one row when it gates one thing and observes another. Per-mode totals count every probe
run, so they differ from the visible row count.)

**Every gated probe passes across the full 4-distro matrix as of C-013 (2026-06-17)** — debian-13 included
now that the Hetzner trixie-backports mirror recovered (excluded in G15.2; see *Sources*). The **one**
non-pass is a known, tracked runtime defect outside C-013's scope: a **second operator's core container
fails to start** (exit 126) on every distro — **F-074**, shown as `⊘` below. C-013's deliverables (toml
retirement, F-070 per-operator bridge gid, F-071 per-operator subid ranges + overlap detection) all pass;
simultaneous multi-operator *runtime* was never a claimed deliverable.

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
| `op2-start` — **F-070** gate: no `SubgidOutOfRangeError` | ✅ | ✅ | ✅ | ✅ |
| op2 **core container running** [^f074] | ⊘ | ⊘ | ⊘ | ⊘ |
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
| `op2-start` — **F-070** gate: no `SubgidOutOfRangeError` | ✅ | ✅ | ✅ | ✅ |
| op2 **core container running** [^f074] | ⊘ | ⊘ | ⊘ | ⊘ |
| `op3f-setup` (entry-less operator setup) | ✅ | ✅ | ✅ | ✅ |
| `op3f-loud-assert` — **F-071** loud-refusal | ✅ | ✅ | ✅ | ✅ |
| `teardown` | ✅ | ✅ | ✅ | ✅ |

## Sources + totals

Each bundle's ubuntu/fedora/arch and debian cells come from distinct runs (debian was re-added 2026-06-17
once the trixie-backports mirror recovered). All runs gated on `vmlease summarize` exit 0 with **per-host
probe count > 0** (guarding the F-075 hollow-green hole — a zero-probe ERROR host that `summarize` alone
would pass).

> **The `⊘` rows do not appear in the `summarize` totals.** `summarize` reports `{FAIL: 0}` for the
> multi-operator bundles because the `op2-start` probe gates only on the F-070 assertion; op2's core
> exit-126 is emitted as an **observational `_review` token** (`OP2_CORE_NOT_RUNNING_review`), not a `FAIL`
> — vmlease has no `xfail`/known-fail verdict yet (F-073), so a tracked-but-real failure can only be encoded
> as observational. This matrix surfaces it as `⊘` so the green totals don't read as "op2 runs." Until
> vmlease grows an `xfail` verdict, **do not** infer "second operator works" from a green `op2-start` cell.

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
- **multi-operator** — the C-013 **allocation mechanisms** are green on all 4 distros: F-070
  (`op2-bridge-gid-assert` — a second operator's `sb-ws-<op>` bridge gid lands in *that operator's* subgid
  range) and F-071 (`op3f-loud-assert` + `mixed-subid-overlap-and-guard` — an entry-less operator fails loud,
  and a fourth operator's subid range does not silently overlap op1's). The `⊘` row is the multi-operator
  **runtime** defect (**F-074**): a second operator's core container exits 126 (bind-mounted entrypoint
  exec-denied, subuid-base-dependent) on **both** multi-operator bundles, **all 4 distros** — deterministic,
  not environmental. It is a tracked follow-up, **not** a claimed C-013 deliverable; C-013 only has to (and
  does) prove the allocation mechanisms.

[^f074]: **`⊘` known-fail — F-074.** The second operator's core container exits **126** immediately
    (`op2-mu2-core-1 Exited (126)`: `exec /usr/local/bin/entrypoint.sh: Permission denied`). The entrypoint
    is bind-mounted from the host owned by the operator's "consumer subuid" (base+999, mode `0500`); op1
    (base 100000) maps to an exec-permitted in-container identity, op2 (base 165536, the distinct high base
    C-013's F-071 allocator hands a second operator) does not — so it is **subuid-base-dependent**, the same
    on every distro and both multi-operator bundles. The `op2-start` probe gates only on the **F-070**
    assertion and records this exit as an observational `_review` token (vmlease has no `xfail` verdict yet,
    F-073), so `summarize` still exits 0. Whether C-013 *caused* this (a new per-operator-mapping defect) or
    merely *exposed* a pre-existing multi-operator-runtime limit is unresolved — tracked as F-074 + a
    sandbox-ai `next` backlog item; simultaneous multi-operator runtime is not a C-013 deliverable.

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
