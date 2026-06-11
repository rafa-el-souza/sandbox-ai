# vmlease-validation Specification

## Purpose
Govern sandbox-ai's vmlease real-host validation batteries (`tests/vmlease/`): the reference-baseline
lifecycle (a living current-state reference, frozen only during an in-flight change's validation and
refreshed at durable milestones), the baseline validation matrix as the current-state verdict snapshot,
the zero-spend `vmlease lint` + `vmlease plan` change-time gate (real-host runs are billable and
out-of-band), and TOML-bundle authoring. This codifies the testing discipline that was previously
prose-only and got misread as "frozen forever" (F-067).

## Requirements
### Requirement: Reference-baseline lifecycle

The tracked vmlease batteries under `tests/vmlease/` SHALL be maintained as a **reference baseline** — a
current-state reference that reflects the last durable (shipped/archived) state of the product on the
supported distro × mode matrix. The reference baseline SHALL be treated as frozen **only for the duration
of an in-flight change's validation** — committed `HEAD` provides that freeze, so a change measures its
cell-flips against the committed baseline without editing it mid-flight. When a change reaches a durable
state (its work is archived), the reference baseline and its generating inputs SHALL be refreshed to the
new real state in the same change, so the tracked baseline never goes stale. The term "frozen pre-fix
yardstick" SHALL NOT be used; the baseline is a living reference, not a permanent freeze.

#### Scenario: A change validates against the committed baseline without mutating it

- **WHEN** a contributor is validating an in-flight product change against the reference baseline
- **THEN** they compare their working-tree results against the committed (`HEAD`) baseline matrix to read the cell-flips
- **AND** they do NOT edit the committed reference baseline during that volatile work

#### Scenario: Durable state refreshes the reference baseline

- **WHEN** a product change reaches a durable state (its validation is complete and the result is shipped)
- **THEN** the reference baseline and the baseline validation matrix are refreshed to the new real state as part of landing that work
- **AND** the prior state remains recoverable via git history and the archived run summaries

### Requirement: Baseline validation matrix is the current-state verdict snapshot

`tests/vmlease/baseline-matrix.md` SHALL be the **baseline validation matrix** — a publish-safe,
verdicts-only (PASS/FAIL) snapshot of the `sandbox` command × distro × mode results, regenerated from the
per-run `*.summary.json` companions. It SHALL carry no host IPs, no transcripts, and no other
run-environment data, and SHALL cite the source run id + date for each mode it reports.

#### Scenario: Matrix regenerated from run summaries

- **WHEN** the baseline validation matrix is refreshed after a durable change
- **THEN** its verdicts are derived from the run `*.summary.json` companions (not hand-asserted)
- **AND** each mode's section cites the run id and date it was generated from
- **AND** the file contains no host IPs or transcripts

### Requirement: Battery-change validation gate

A change that adds or modifies a vmlease battery SHALL gate on the zero-spend checks **`vmlease lint`**
(shellcheck, severity-gated) and **`vmlease plan`** (full bundle load/resolution) passing on every battery
it touches. A real-host `vmlease run` is billable and out-of-band: it SHALL NOT be required to land a
battery change, and when the refreshed verdicts are reused from an existing run, that run SHALL be
byte-identical to the battery being landed.

#### Scenario: A battery change lands on lint + plan alone

- **WHEN** a change modifies a tracked battery
- **THEN** `vmlease lint` and `vmlease plan` pass on every touched bundle
- **AND** no real-host `vmlease run` is required for the change to land

#### Scenario: Reused verdicts must come from the identical battery

- **WHEN** a refreshed matrix reuses verdicts from a prior real-host run instead of re-running
- **THEN** the battery that produced those verdicts is byte-identical to the battery being landed

### Requirement: Batteries authored as vmlease TOML bundles

Each tracked battery SHALL be a vmlease TOML bundle — a `battery.toml` manifest whose probes are authored
inline (`run = '''…'''`) unless a probe genuinely warrants a co-located `script` file. The reference
baseline SHALL be editable directly (no separate derive-a-copy "acceptance" builder is required to layer a
change's deltas onto a frozen baseline); pass/fail MAY be expressed via `success_when` tokens instead of
explicit `exit`-code plumbing.

#### Scenario: A battery is a TOML bundle with inline probes

- **WHEN** a tracked battery is loaded
- **THEN** it is a `battery.toml` manifest that `vmlease plan` resolves
- **AND** its probes are authored inline (or as a co-located contained script), with no JSON battery and no derive-a-copy acceptance builder

